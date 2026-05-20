import duckdb
conn = duckdb.connect()
df = conn.query("SELECT * FROM read_csv_auto('https://www.datos.gov.co/resource/jbjy-vk9h.csv') LIMIT 1").df()
print(df.columns.tolist())
