import requests

url = "https://www.datos.gov.co/resource/jbjy-vk9h.json"

q1 = {
    "$select": "proveedor_adjudicado",
    "$where": "upper(trim(proveedor_adjudicado)) like '%ACCION COMUNAL%' AND date_extract_y(fecha_de_firma) = 2026",
    "$limit": "5"
}

r1 = requests.get(url, params=q1)
print("Q1 Status:", r1.status_code)
if r1.status_code == 200:
    print("Q1 Data:", r1.json())
else:
    print("Q1 Error:", r1.text)

# Also test if 'identificacion_representante_legal' throws a 400
q2 = {
    "$select": "identificacion_representante_legal",
    "$limit": "1"
}
r2 = requests.get(url, params=q2)
print("Q2 Status:", r2.status_code)
if r2.status_code != 200:
    print("Q2 Error:", r2.text)

# And test 'identificaci_n_representante_legal'
q3 = {
    "$select": "identificaci_n_representante_legal",
    "$limit": "1"
}
r3 = requests.get(url, params=q3)
print("Q3 Status:", r3.status_code)
if r3.status_code != 200:
    print("Q3 Error:", r3.text)

