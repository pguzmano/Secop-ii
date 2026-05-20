import duckdb
r = duckdb.execute("SELECT DISTINCT departamento_entidad FROM read_parquet('data/secop.parquet') ORDER BY 1").fetchall()
for x in r:
    print(repr(x[0]))
