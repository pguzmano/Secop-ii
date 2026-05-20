import json
with open("data/colombia_dptos.geojson", encoding="utf-8") as f:
    g = json.load(f)
feat = g["features"][0]
print("Tipo geometría:", feat["geometry"]["type"])
print("Propiedades disponibles:")
for k, v in feat["properties"].items():
    print(f"  {k!r}: {v!r}")
print(f"\nTotal features: {len(g['features'])}")
