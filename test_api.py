
# test_api.py — Verificar que la API SoQL funciona correctamente
import requests, json

BASE = "https://www.datos.gov.co/resource/jbjy-vk9h.json"

# Test 1: años disponibles
r = requests.get(BASE, params={
    "$select": "date_extract_y(fecha_de_firma) AS anio, COUNT(*) AS n",
    "$group":  "date_extract_y(fecha_de_firma)",
    "$where":  "fecha_de_firma IS NOT NULL",
    "$order":  "anio DESC",
    "$limit":  "20",
}, timeout=20)
print("=== AÑOS DISPONIBLES ===")
print(json.dumps(r.json()[:5], indent=2))

# Test 2: departamentos para 2024
r2 = requests.get(BASE, params={
    "$select": "upper(departamento) AS dep, SUM(valor_del_contrato) AS valor, COUNT(*) AS contratos",
    "$where":  "date_extract_y(fecha_de_firma) = 2024 AND departamento IS NOT NULL",
    "$group":  "upper(departamento)",
    "$order":  "valor DESC",
    "$limit":  "5",
}, timeout=20)
print("\n=== TOP DEPARTAMENTOS 2024 ===")
print(json.dumps(r2.json(), indent=2))

# Test 3: KPIs nacionales 2024
r3 = requests.get(BASE, params={
    "$select": "SUM(valor_del_contrato) AS total, COUNT(*) AS contratos",
    "$where":  "date_extract_y(fecha_de_firma) = 2024",
    "$limit":  "1",
}, timeout=20)
print("\n=== KPIs 2024 ===")
print(json.dumps(r3.json(), indent=2))
