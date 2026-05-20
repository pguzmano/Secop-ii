"""
etl_geografia.py
================
Enriquecimiento geográfico SECOP II con códigos DIVIPOLA usando mpio.json.
Genera: dep_clean, mun_clean, codigo_departamento, codigo_municipio

Stack: Polars
Ejecución: python etl_geografia.py
"""

import os, sys, time, json, unicodedata
import polars as pl
import duckdb

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PARQUET_IN   = os.path.join(BASE_DIR, "data", "secop.parquet")
PARQUET_OUT  = os.path.join(BASE_DIR, "data", "secop_enriquecido.parquet")
MPIO_JSON    = os.path.join(BASE_DIR, "data", "mpio.json")

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def quitar_tildes(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))

def expr_normalizar(col_name: str) -> pl.Expr:
    return (
        pl.col(col_name)
        .cast(pl.Utf8)
        .str.strip_chars()
        .str.to_uppercase()
        .str.replace_all("Á", "A", literal=True).str.replace_all("á", "A", literal=True)
        .str.replace_all("É", "E", literal=True).str.replace_all("é", "E", literal=True)
        .str.replace_all("Í", "I", literal=True).str.replace_all("í", "I", literal=True)
        .str.replace_all("Ó", "O", literal=True).str.replace_all("ó", "O", literal=True)
        .str.replace_all("Ú", "U", literal=True).str.replace_all("ú", "U", literal=True)
        .str.replace_all("Ü", "U", literal=True).str.replace_all("ü", "U", literal=True)
        .str.replace_all("Ñ", "N", literal=True).str.replace_all("ñ", "N", literal=True)
        .str.replace_all("BOGOTA D.C.", "BOGOTA", literal=True)
        .str.replace_all("BOGOTA, D.C.", "BOGOTA", literal=True)
        .str.replace_all("DISTRITO CAPITAL DE BOGOTA", "BOGOTA", literal=True)
        .str.replace_all("DISTRITO CAPITAL", "BOGOTA", literal=True)
        .str.replace_all("D.C.", "", literal=True)
        .str.replace_all(r"\s+", " ")
        .str.strip_chars()
    )

def main() -> None:
    t0 = time.time()
    log("="*55)
    log("  ETL Geografia — Fase 1 y 2")
    log("="*55)

    if not os.path.exists(PARQUET_IN):
        log(f"ERROR: No encontrado {PARQUET_IN}. Ejecuta etl_secop.py primero.")
        sys.exit(1)
    
    if not os.path.exists(MPIO_JSON):
        log(f"ERROR: No encontrado {MPIO_JSON}.")
        sys.exit(1)

    # 1. Cargar datos del GeoJSON de Municipios
    log("Cargando diccionario de municipios desde mpio.json...")
    with open(MPIO_JSON, "r", encoding="utf-8") as f:
        geo_data = json.load(f)
    
    mun_records = []
    for f in geo_data["features"]:
        props = f["properties"]
        # Normalize directly the names to match
        nombre_dpt = normalizar_texto(props.get("NOMBRE_DPT", ""))
        nombre_mpi = normalizar_texto(props.get("NOMBRE_MPI", ""))
        mun_records.append({
            "geo_dep_clean": nombre_dpt,
            "geo_mun_clean": nombre_mpi,
            "codigo_departamento": props.get("DPTO", ""),
            "codigo_municipio": props.get("MPIOS", "")
        })
    
    df_geo = pl.DataFrame(mun_records)
    
    # 2. Cargar SECOP
    log(f"Leyendo: {PARQUET_IN}")
    df = pl.read_parquet(PARQUET_IN)
    
    # Limpiar columnas geo viejas
    cols_drop = [c for c in ["codigo_departamento","departamento_normalizado","dep_clean","mun_clean","codigo_municipio"] if c in df.columns]
    if cols_drop:
        df = df.drop(cols_drop)

    # FASE 1: Normalización de datos con Polars
    log("Normalizando departamentos y municipios (Fase 1)...")
    tiene_ciudad = "ciudad_entidad" in df.columns

    exprs = [expr_normalizar("departamento_entidad").alias("dep_clean")]
    if tiene_ciudad:
        exprs.append(
            pl.when(
                pl.col("ciudad_entidad").is_null()
                | (pl.col("ciudad_entidad").cast(pl.Utf8).str.strip_chars() == "")
            )
            .then(pl.lit("SIN MUNICIPIO"))
            .otherwise(expr_normalizar("ciudad_entidad"))
            .alias("mun_clean")
        )
    else:
        exprs.append(pl.lit("SIN MUNICIPIO").alias("mun_clean"))

    df = df.with_columns(exprs)

    # FASE 2: JOIN Correcto con DANE (GeoJSON) - LEFT JOIN
    log("Realizando LEFT JOIN con datos de DIVIPOLA (Fase 2)...")
    df = df.join(
        df_geo, 
        left_on=["dep_clean", "mun_clean"], 
        right_on=["geo_dep_clean", "geo_mun_clean"], 
        how="left"
    )
    
    # Rellenar códigos faltantes
    df = df.with_columns([
        pl.col("codigo_departamento").fill_null("00").cast(pl.Categorical),
        pl.col("codigo_municipio").fill_null("00000").cast(pl.Categorical),
        pl.col("dep_clean").cast(pl.Categorical),
        pl.col("mun_clean").cast(pl.Categorical)
    ])

    log(f"Exportando a {PARQUET_OUT}...")
    df.write_parquet(PARQUET_OUT, compression="zstd", compression_level=3,
                     statistics=True, row_group_size=500_000)
    log(f"OK Parquet: {os.path.getsize(PARQUET_OUT)/1_048_576:.1f} MB")

    # Validación rápida
    con = duckdb.connect()
    path = PARQUET_OUT.replace("\\", "/")
    stats = con.execute(f"SELECT COUNT(*) as total, COUNT(codigo_municipio) filter (where codigo_municipio != '00000') as match_mun FROM read_parquet('{path}')").fetchone()
    log(f"Registros totales: {stats[0]:,}")
    log(f"Registros con municipio DIVIPOLA: {stats[1]:,} ({stats[1]/stats[0]*100:.1f}%)")
    con.close()

    log(f"TIEMPO: {time.time()-t0:.1f}s | EXITO: ETL completado.")

def normalizar_texto(texto: str) -> str:
    if not texto: return ""
    t = quitar_tildes(str(texto)).upper().strip()
    t = t.replace("BOGOTA D.C.", "BOGOTA").replace("BOGOTA, D.C.", "BOGOTA")
    t = t.replace("DISTRITO CAPITAL DE BOGOTA", "BOGOTA")
    t = t.replace("DISTRITO CAPITAL", "BOGOTA").replace("D.C.", "").strip()
    return " ".join(t.split())

if __name__ == "__main__":
    main()
