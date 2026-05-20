"""
Test simple de la API para verificar que get_anios() funciona
"""
import requests
import json

API_RESOURCE = "jbjy-vk9h"
API_BASE = f"https://www.datos.gov.co/resource/{API_RESOURCE}.json"

print("🔍 Probando consulta de años...")

params = {
    "$select": "date_extract_y(fecha_de_firma) AS anio, COUNT(*) AS n",
    "$group": "date_extract_y(fecha_de_firma)",
    "$where": "fecha_de_firma IS NOT NULL",
    "$order": "anio DESC",
    "$limit": "50",
}

try:
    print(f"📡 Consultando: {API_BASE}")
    print(f"📋 Parámetros: {json.dumps(params, indent=2)}")
    
    headers = {
        "Accept-Encoding": "gzip",
        "User-Agent": "SECOP-Dashboard/2.0"
    }
    
    r = requests.get(API_BASE, params=params, timeout=30, headers=headers)
    print(f"✅ Status: {r.status_code}")
    print(f"📦 Tamaño respuesta: {len(r.content)} bytes")
    
    data = r.json()
    print(f"📊 Registros recibidos: {len(data)}")
    
    if data:
        print("\n🎯 Primeros 5 años:")
        for i, row in enumerate(data[:5]):
            print(f"  {i+1}. Año {row.get('anio')}: {row.get('n')} contratos")
    
    print("\n✅ Consulta exitosa!")
    
except requests.Timeout:
    print("❌ Timeout - La API no respondió a tiempo")
except Exception as e:
    print(f"❌ Error: {e}")
