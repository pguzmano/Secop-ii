import polars as pl

lf = pl.scan_csv(
    "SECOP_II_-_Procesos_de_Contrataci_n.csv",
    encoding="utf8-lossy",
    infer_schema_length=100,
    n_rows=3,
)
cols = lf.collect_schema().names()
print("TODAS LAS COLUMNAS DEL CSV:")
for i, c in enumerate(cols):
    print(f"  {i:02d}. {repr(c)}")
