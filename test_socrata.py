import time
import requests
import pandas as pd
from app import soql_get

t0 = time.time()
print("Starting query with DISTINCT...")
try:
    df = soql_get({
        '$select': 'SUM(valor_del_contrato) AS total_valor, COUNT(*) AS total_contratos, COUNT(DISTINCT nombre_entidad) AS total_entidades',
        '$where': 'date_extract_y(fecha_de_firma)=2026',
        '$limit': '1'
    })
    print(f"Time with DISTINCT: {time.time() - t0}")
    print(df)
except Exception as e:
    print(e)

t0 = time.time()
print("\nStarting query without DISTINCT...")
try:
    df = soql_get({
        '$select': 'SUM(valor_del_contrato) AS total_valor, COUNT(*) AS total_contratos',
        '$where': 'date_extract_y(fecha_de_firma)=2026',
        '$limit': '1'
    })
    print(f"Time without DISTINCT: {time.time() - t0}")
    print(df)
except Exception as e:
    print(e)
