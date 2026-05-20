"""
etl_secop.py
============
Pipeline ETL de alto rendimiento para SECOP II (~8 GB, 8.63M filas).
Stack: Polars (transformaciones) + DuckDB (agregaciones) + Parquet/zstd (salida).

Ejecución:
    python etl_secop.py
"""

import os
import sys
import time
import polars as pl
import duckdb

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CSV_PATH   = os.path.join(BASE_DIR, "SECOP_II_-_Procesos_de_Contrataci_n.csv")
OUT_DIR    = os.path.join(BASE_DIR, "data")
OUT_FILE   = os.path.join(OUT_DIR, "secop.parquet")

COLUMNAS_MAP = {
    "Departamento Entidad":            "departamento_entidad",
    "Ciudad Entidad":                  "ciudad_entidad",
    "Entidad":                         "nombre_entidad",
    "Nit Entidad":                     "nit_entidad",
    "Tipo de Contrato":                "tipo_contrato",
    "Nombre del Proveedor Adjudicado": "proveedor_adjudicado",
    "Modalidad de Contratacion":       "modalidad_de_contratacion",
    "Valor Total Adjudicacion":        "valor_del_contrato",
    "Proveedores Unicos con Respuestas": "numero_de_oferentes",
    "Fecha Adjudicacion":              "fecha_de_firma_del_contrato",
}
COLUMNAS_CSV = list(COLUMNAS_MAP.keys())

# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def crear_directorio() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    log(f"Directorio de salida listo: {OUT_DIR}")


# ---------------------------------------------------------------------------
# FASE 1 — LECTURA LAZY (Polars no carga todo en RAM de golpe)
# ---------------------------------------------------------------------------

def leer_csv_lazy() -> pl.LazyFrame:
    """
    Lee el CSV con LazyFrame: Polars planifica el query y solo materializa
    lo que se necesita. Ideal para archivos de varios GB.
    """
    log("Iniciando lectura lazy del CSV (esto puede tardar unos segundos)...")

    lf = pl.scan_csv(
        CSV_PATH,
        separator=",",
        encoding="utf8-lossy",          # tolera caracteres raros
        infer_schema_length=50_000,     # muestra más filas para inferir tipos
        null_values=["", "NULL", "null", "N/A", "n/a", "#N/A"],
        try_parse_dates=False,          # lo hacemos manual para control total
        low_memory=False,               # Polars usa columnar streaming interno
    )

    # Verificar que las columnas existen (tolerante a mayúsculas/minúsculas)
    schema_cols = [c.lower() for c in lf.collect_schema().names()]
    cols_validas = [
        c for c in COLUMNAS_CSV
        if c.lower() in schema_cols
    ]
    cols_faltantes = set(COLUMNAS_CSV) - set(cols_validas)
    if cols_faltantes:
        log(f"AVISO: Columnas no encontradas (se omiten): {cols_faltantes}")

    # Selección de columnas — reducción dramática de I/O
    lf = lf.select(cols_validas)
    
    # Renombrar a nombres internos
    renames = {col: COLUMNAS_MAP[col] for col in cols_validas}
    lf = lf.rename(renames)
    
    log(f"Columnas seleccionadas y renombradas: {list(renames.values())}")
    return lf


# ---------------------------------------------------------------------------
# FASE 2 — TRANSFORMACIONES Y FEATURE ENGINEERING
# ---------------------------------------------------------------------------

def transformar(lf: pl.LazyFrame) -> pl.LazyFrame:
    """
    Aplica limpieza, tipado correcto y feature engineering sin salir del
    plan lazy de Polars (todo se ejecuta en un solo paso al coleccionar).
    """
    log("Construyendo plan de transformación (lazy)...")

    lf = (
        lf
        # ── 1. Limpiar texto ────────────────────────────────────────────────
        .with_columns([
            pl.col("departamento_entidad")
              .str.strip_chars()
              .str.to_titlecase()
              .alias("departamento_entidad"),

            pl.col("nombre_entidad")
              .str.strip_chars()
              .alias("nombre_entidad"),

            pl.col("proveedor_adjudicado")
              .str.strip_chars()
              .alias("proveedor_adjudicado"),

            pl.col("modalidad_de_contratacion")
              .str.strip_chars()
              .str.to_titlecase()
              .alias("modalidad_de_contratacion"),
        ])

        # ── 2. Tipado numérico ───────────────────────────────────────────────
        .with_columns([
            # valor_del_contrato: puede venir con puntos/comas como separadores
            pl.col("valor_del_contrato")
              .cast(pl.Utf8)
              .str.replace(",", ".", literal=True)
              .cast(pl.Float64, strict=False)
              .fill_null(0.0)
              .alias("valor_del_contrato"),

            pl.col("numero_de_oferentes")
              .cast(pl.Int32, strict=False)
              .fill_null(0)
              .alias("numero_de_oferentes"),
        ])

        # ── 3. Fecha y año ───────────────────────────────────────────────────
        .with_columns([
            pl.col("fecha_de_firma_del_contrato")
              .str.to_date(format="%m/%d/%Y", strict=False)
              .alias("fecha_de_firma_del_contrato"),
        ])
        .with_columns([
            pl.col("fecha_de_firma_del_contrato")
              .dt.year()
              .cast(pl.Int32)
              .fill_null(0)
              .alias("anio"),
        ])

        # ── 4. Filtrar valores imposibles ────────────────────────────────────
        .filter(pl.col("valor_del_contrato") >= 0)
        .filter(pl.col("numero_de_oferentes") >= 0)

        # ── 5. Feature: nivel_competitividad ─────────────────────────────────
        .with_columns([
            pl.when(pl.col("numero_de_oferentes") == 1)
              .then(pl.lit("1 oferente"))
              .when(pl.col("numero_de_oferentes").is_between(2, 5))
              .then(pl.lit("2-5 oferentes"))
              .when(pl.col("numero_de_oferentes") > 5)
              .then(pl.lit("+5 oferentes"))
              .otherwise(pl.lit("Sin dato"))
              .alias("nivel_competitividad"),
        ])

        # ── 6. Feature: indice_riesgo (normalizado 0–1) ──────────────────────
        # Lógica: penaliza contratos con 1 oferente y alto valor.
        # Score = es_unico_oferente × valor_normalizado_por_percentil
        # Se normaliza en el paso siguiente tras colección parcial.
        .with_columns([
            pl.when(pl.col("numero_de_oferentes") == 1)
              .then(pl.col("valor_del_contrato"))
              .otherwise(0.0)
              .alias("_valor_riesgo"),
        ])
    )

    return lf


def calcular_indice_riesgo(df: pl.DataFrame) -> pl.DataFrame:
    """
    Normaliza el índice de riesgo (0-1) una vez materializado el DataFrame.
    Se hace fuera del lazy porque min/max requieren estadísticas globales.
    """
    log("Calculando índice de riesgo normalizado...")
    val_max = df["_valor_riesgo"].max()
    val_min = df["_valor_riesgo"].min()

    if val_max == val_min or val_max == 0:
        df = df.with_columns(
            pl.lit(0.0).alias("indice_riesgo")
        )
    else:
        df = df.with_columns(
            ((pl.col("_valor_riesgo") - val_min) / (val_max - val_min))
            .round(4)
            .alias("indice_riesgo")
        )

    # Eliminar columna auxiliar
    df = df.drop("_valor_riesgo")
    return df


# ---------------------------------------------------------------------------
# FASE 3 — EXPORTAR A PARQUET (compresión zstd)
# ---------------------------------------------------------------------------

def exportar_parquet(df: pl.DataFrame) -> None:
    log(f"Exportando {len(df):,} filas a Parquet con compresión zstd...")
    df.write_parquet(
        OUT_FILE,
        compression="zstd",
        compression_level=3,    # nivel 3: buen balance velocidad/tamaño
        statistics=True,        # habilita estadísticas para DuckDB
        row_group_size=500_000, # grupos grandes = menos overhead
    )
    size_mb = os.path.getsize(OUT_FILE) / 1_048_576
    log(f"OK Parquet guardado en: {OUT_FILE}  ({size_mb:.1f} MB)")


# ---------------------------------------------------------------------------
# VALIDACIÓN RÁPIDA CON DUCKDB
# ---------------------------------------------------------------------------

def validar_con_duckdb() -> None:
    log("Validando el Parquet con DuckDB...")
    con = duckdb.connect()
    resultado = con.execute(f"""
        SELECT
            COUNT(*)                                   AS total_registros,
            COUNT(DISTINCT departamento_entidad)       AS total_departamentos,
            ROUND(SUM(valor_del_contrato) / 1e12, 2)  AS presupuesto_billones_COP,
            AVG(indice_riesgo)                         AS riesgo_promedio
        FROM read_parquet('{OUT_FILE.replace(chr(92), "/")}')
    """).fetchdf()

    print("\n" + "="*60)
    print("  RESUMEN DEL DATASET PROCESADO")
    print("="*60)
    for col, val in zip(resultado.columns, resultado.iloc[0]):
        print(f"  {col:<35}: {val}")
    print("="*60 + "\n")
    con.close()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    t0 = time.time()
    log("=" * 55)
    log("  SECOP II — ETL Pipeline iniciado")
    log("=" * 55)

    # Verificar que el CSV existe
    if not os.path.exists(CSV_PATH):
        log(f"ERROR: CSV no encontrado en: {CSV_PATH}")
        sys.exit(1)

    size_gb = os.path.getsize(CSV_PATH) / 1_073_741_824
    log(f"Archivo fuente: {CSV_PATH}  ({size_gb:.2f} GB)")

    # Pipeline
    crear_directorio()
    lf  = leer_csv_lazy()
    lf  = transformar(lf)

    log("Materializando transformaciones (lectura real del CSV)...")
    df  = lf.collect(streaming=True)   # streaming=True → procesa en chunks
    log(f"Filas cargadas: {len(df):,}")

    df  = calcular_indice_riesgo(df)
    exportar_parquet(df)
    validar_con_duckdb()

    elapsed = time.time() - t0
    log(f"TIEMPO: Tiempo total de ETL: {elapsed/60:.1f} minutos")
    log("EXITO: Pipeline completado exitosamente.")


if __name__ == "__main__":
    main()
