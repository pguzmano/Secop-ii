# 🚀 Guía de Implementación — Optimizaciones de Rendimiento

## Resumen Rápido

He identificado **8 cuellos de botella** que ralentizan el dashboard. Las optimizaciones pueden mejorar los tiempos **5-10x**.

**Tiempo estimado de implementación:** 2-4 horas

---

## 📋 Checklist de Implementación

### Fase 1: Caché Persistente (30 min) — CRÍTICA
- [ ] Agregar `sqlite3` a requirements.txt (ya incluido en Python)
- [ ] Copiar clase `PersistentCache` de `OPTIMIZACIONES_INMEDIATAS.py`
- [ ] Reemplazar `soql_get()` con `soql_get_optimized()`
- [ ] Testear: cambiar de departamento 2 veces (segunda debe ser <0.5s)

### Fase 2: Batch Queries (20 min) — ALTA
- [ ] Implementar `get_departamentos_y_municipios_batch()`
- [ ] Usar en `render_mapa_departamentos()` y `render_mapa_municipios()`
- [ ] Testear: carga inicial debe ser <2s

### Fase 3: Vectorizar Grafos (30 min) — ALTA
- [ ] Agregar `polars` a requirements.txt
- [ ] Reemplazar loops en `network_analysis.py` con `calcular_riesgo_vectorizado()`
- [ ] Testear: cambio de municipio en grafo debe ser <1s

### Fase 4: Simplificar GeoJSON (20 min) — MEDIA
- [ ] Agregar `shapely` a requirements.txt
- [ ] Ejecutar `simplificar_geojson()` en los archivos GeoJSON
- [ ] Testear: render de mapas debe ser <1s

### Fase 5: KPIs desde Parquet (20 min) — ALTA
- [ ] Reemplazar `get_kpis()` con `get_kpis_from_parquet()`
- [ ] Testear: cambio de filtro debe ser <0.2s

### Fase 6: Callbacks (30 min) — MEDIA
- [ ] Reemplazar `st.rerun()` con callbacks en selectbox/mapas
- [ ] Testear: navegación debe ser fluida sin parpadeos

### Fase 7: Monitoreo (15 min) — BAJA
- [ ] Agregar decorador `@monitor_latency()` a funciones críticas
- [ ] Crear archivo `.logs/latency.log`
- [ ] Revisar logs después de cada cambio

### Fase 8: Preload (10 min) — BAJA
- [ ] Agregar `preload_top_departments()` en `main()`
- [ ] Testear: primera carga debe mostrar datos rápidamente

---

## 🔧 Instrucciones Detalladas

### PASO 1: Actualizar requirements.txt

```bash
# Agregar estas líneas a requirements.txt:
polars>=0.20.0
shapely>=2.0.0
```

Luego instalar:
```bash
pip install -r requirements.txt
```

---

### PASO 2: Implementar Caché Persistente

**Archivo:** `app.py`

**Antes:**
```python
def soql_get(params: dict) -> pd.DataFrame:
    try:
        r = requests.get(API_BASE, params=params, timeout=API_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data)
    except requests.Timeout:
        st.error("⏱️ Timeout al consultar la API...")
        return pd.DataFrame()
```

**Después:**
```python
# Copiar PersistentCache de OPTIMIZACIONES_INMEDIATAS.py
from OPTIMIZACIONES_INMEDIATAS import PersistentCache, soql_get_optimized

_cache = PersistentCache()

def soql_get(params: dict) -> pd.DataFrame:
    return soql_get_optimized(
        params,
        api_base=API_BASE,
        api_timeout=API_TIMEOUT,
        cache_ttl=3600,
        max_retries=2
    )
```

**Testear:**
```python
# En terminal:
python -c "
import streamlit as st
from app import get_departamentos
import time

# Primera llamada (desde API)
t0 = time.time()
df1 = get_departamentos(2024)
print(f'Primera: {time.time() - t0:.2f}s')

# Segunda llamada (desde caché)
t0 = time.time()
df2 = get_departamentos(2024)
print(f'Segunda: {time.time() - t0:.2f}s')  # Debe ser <0.1s
"
```

---

### PASO 3: Implementar Batch Queries

**Archivo:** `app.py`

**Antes:**
```python
@st.cache_data(ttl=3600, show_spinner="Cargando departamentos...")
def get_departamentos(anio: int) -> pd.DataFrame:
    df = soql_get({
        "$select": "upper(departamento) AS departamento, ...",
        "$where": f"date_extract_y(fecha_de_firma) = {anio} ...",
        ...
    })
    return df

@st.cache_data(ttl=3600, show_spinner="Cargando municipios...")
def get_municipios(anio: int, dep_raw: str) -> pd.DataFrame:
    df = soql_get({
        "$select": "upper(ciudad) AS ciudad, ...",
        "$where": f"... AND upper(departamento) = '{dep_q}' ...",
        ...
    })
    return df
```

**Después:**
```python
from OPTIMIZACIONES_INMEDIATAS import get_departamentos_y_municipios_batch

@st.cache_data(ttl=3600, show_spinner="Cargando geografía...")
def get_departamentos(anio: int) -> pd.DataFrame:
    df_deps, _ = get_departamentos_y_municipios_batch(anio, API_BASE)
    return df_deps

@st.cache_data(ttl=3600, show_spinner="Cargando municipios...")
def get_municipios(anio: int, dep_raw: str) -> pd.DataFrame:
    _, muns_por_dep = get_departamentos_y_municipios_batch(anio, API_BASE)
    return muns_por_dep.get(dep_raw, pd.DataFrame())
```

---

### PASO 4: Vectorizar Cálculos de Riesgos

**Archivo:** `network_analysis.py`

**Antes:**
```python
def process_metrics_and_risk(df_raw: pd.DataFrame):
    # ... código que usa loops de Pandas ...
    prov_df['score_riesgo'] = (
        np.log1p(prov_df['contratos_totales']) * 
        prov_df['pct_directa'] * 
        np.log1p(prov_df['valor_total'])
    ).round(2)
```

**Después:**
```python
from OPTIMIZACIONES_INMEDIATAS import calcular_riesgo_vectorizado

def process_metrics_and_risk(df_raw: pd.DataFrame):
    # Usar Polars para cálculos vectorizados
    prov_df = calcular_riesgo_vectorizado(df_raw)
    # ... resto del código ...
```

---

### PASO 5: Simplificar GeoJSON

**Ejecutar una sola vez:**

```bash
python -c "
from OPTIMIZACIONES_INMEDIATAS import simplificar_geojson
import json

# Simplificar departamentos
geo_deps = simplificar_geojson('data/depto.json', tolerance=0.01)
with open('data/depto.json', 'w') as f:
    json.dump(geo_deps, f)

# Simplificar municipios
geo_muns = simplificar_geojson('data/mpio.json', tolerance=0.005)
with open('data/mpio.json', 'w') as f:
    json.dump(geo_muns, f)

print('GeoJSON simplificado')
"
```

---

### PASO 6: Usar Parquet para KPIs

**Archivo:** `app.py`

**Antes:**
```python
@st.cache_data(ttl=3600, show_spinner="Calculando KPIs...")
def get_kpis(anio: int, dep_raw: str, mun_raw: str, actor_filter: str = "") -> dict:
    # Consulta Socrata
    df = soql_get({
        "$select": "SUM(valor_del_contrato) AS total_valor, ...",
        ...
    })
    ...
```

**Después:**
```python
from OPTIMIZACIONES_INMEDIATAS import get_kpis_from_parquet

@st.cache_data(ttl=3600, show_spinner="Calculando KPIs...")
def get_kpis(anio: int, dep_raw: str, mun_raw: str, actor_filter: str = "") -> dict:
    # Usar Parquet local (ultrarápido)
    return get_kpis_from_parquet(
        anio,
        dep_raw=dep_raw,
        mun_raw=mun_raw,
        parquet_path="data/secop.parquet"
    )
```

---

### PASO 7: Agregar Monitoreo

**Archivo:** `app.py`

```python
from OPTIMIZACIONES_INMEDIATAS import monitor_latency

# Decorar funciones críticas:
@st.cache_data(ttl=3600)
@monitor_latency("get_departamentos")
def get_departamentos(anio: int) -> pd.DataFrame:
    ...

@st.cache_data(ttl=3600)
@monitor_latency("get_municipios")
def get_municipios(anio: int, dep_raw: str) -> pd.DataFrame:
    ...

@st.cache_data(ttl=3600)
@monitor_latency("get_kpis")
def get_kpis(...) -> dict:
    ...
```

Luego revisar logs:
```bash
tail -f .logs/latency.log
```

---

### PASO 8: Preload de Datos

**Archivo:** `app.py` - función `main()`

**Antes:**
```python
def main():
    init_state()
    anios = get_anios()
    ...
```

**Después:**
```python
from OPTIMIZACIONES_INMEDIATAS import preload_top_departments

def main():
    init_state()
    anios = get_anios()
    
    # Precargar top departamentos (sin mostrar spinner)
    if not st.session_state.get("preload_done"):
        preload_top_departments(anios[0], API_BASE)
        st.session_state["preload_done"] = True
    
    ...
```

---

## 📊 Benchmarks Esperados

Después de implementar todas las optimizaciones:

| Operación | Antes | Después | Mejora |
|-----------|-------|---------|--------|
| Cambio de departamento | 2-3s | 0.3-0.5s | **6-10x** |
| Cambio de municipio | 3-5s | 0.5-1s | **5-10x** |
| Cálculo de KPIs | 1-2s | 0.1-0.2s | **10-20x** |
| Render de grafo | 3-8s | 0.5-1s | **6-16x** |
| Carga inicial | 5-10s | 1-2s | **5-10x** |

---

## 🧪 Plan de Testing

### Test 1: Caché Persistente
```bash
# Ejecutar app
streamlit run app.py

# En navegador:
# 1. Seleccionar departamento (anotar tiempo)
# 2. Seleccionar otro departamento (anotar tiempo)
# 3. Volver al primero (debe ser <0.5s)
```

### Test 2: Batch Queries
```bash
# Medir tiempo de carga inicial
# Debe ser <2s
```

### Test 3: Grafos Vectorizados
```bash
# Cambiar de municipio en tab de Redes
# Debe ser <1s
```

### Test 4: Monitoreo
```bash
# Revisar .logs/latency.log
# Verificar que todas las funciones están <1s
```

---

## 🐛 Troubleshooting

### Problema: "ModuleNotFoundError: No module named 'polars'"
**Solución:**
```bash
pip install polars
```

### Problema: "ModuleNotFoundError: No module named 'shapely'"
**Solución:**
```bash
pip install shapely
```

### Problema: Caché no se actualiza
**Solución:**
```bash
# Limpiar caché
rm -rf .cache/api_cache.db

# O en Python:
from OPTIMIZACIONES_INMEDIATAS import _cache
_cache.clear_expired()
```

### Problema: GeoJSON simplificado se ve mal
**Solución:**
Aumentar `tolerance` en `simplificar_geojson()`:
```python
geo_deps = simplificar_geojson('data/depto.json', tolerance=0.005)  # Más detalle
```

---

## 📝 Notas Importantes

1. **Caché persistente:** Se almacena en `.cache/api_cache.db`. Ocupará ~50-100 MB después de 1 semana de uso.

2. **Parquet:** Asegúrate de que `data/secop.parquet` existe. Si no, ejecutar `etl_secop.py` primero.

3. **Polars:** Es opcional. Si no se instala, el código fallará gracefully y usará Pandas.

4. **Shapely:** Es opcional. Si no se instala, se usará GeoJSON original sin simplificar.

5. **Logs:** Los logs de latencia se guardan en `.logs/latency.log`. Revisar regularmente para identificar nuevos cuellos de botella.

---

## 🎯 Próximos Pasos (Fase 2)

Después de implementar estas optimizaciones, considerar:

1. **Caché distribuido:** Redis para compartir caché entre instancias
2. **CDN para GeoJSON:** Servir desde CloudFront/Cloudflare
3. **Compresión de datos:** Usar Brotli en lugar de gzip
4. **Índices en API:** Solicitar a Socrata que agregue índices
5. **GraphQL:** Reemplazar REST con GraphQL para queries más eficientes

---

## 📞 Soporte

Si tienes problemas durante la implementación:

1. Revisar `.logs/latency.log` para identificar qué función es lenta
2. Ejecutar tests individuales
3. Verificar que todas las dependencias están instaladas
4. Limpiar caché y reintentar

