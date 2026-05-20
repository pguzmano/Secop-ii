# 📊 Análisis de Rendimiento — SECOP II Dashboard

## 🎯 Resumen Ejecutivo

El proyecto es un **dashboard Streamlit bicapa** que consulta datos de contratación pública colombiana desde la API Socrata. He identificado **8 cuellos de botella críticos** que afectan los tiempos de respuesta.

---

## 🔴 Problemas Identificados

### 1. **Consultas API Redundantes (CRÍTICO)**
**Ubicación:** `app.py` - funciones `get_departamentos()`, `get_municipios()`, `get_entidades()`, etc.

**Problema:**
- Cada función hace una petición HTTP separada a Socrata
- No hay deduplicación: si el usuario navega Dep → Municipio → Entidades, se hacen 3+ llamadas
- Timeout de 90 segundos es muy alto (bloquea la UI)
- Sin reintentos automáticos

**Impacto:** Latencia de 2-5 segundos por navegación

**Solución:**
```python
# Implementar caché de 2 niveles:
# 1. Caché en memoria (Streamlit @st.cache_data)
# 2. Caché persistente (SQLite/DuckDB local)
# 3. Batch queries: consolidar múltiples SELECT en una sola petición
```

---

### 2. **Cálculo de KPIs Ineficiente (ALTO)**
**Ubicación:** `app.py` - función `get_kpis()`

**Problema:**
- Hace petición Socrata para cada filtro (global, dep, mun, actor)
- `COUNT(DISTINCT nombre_entidad)` a nivel nacional causa timeout
- No usa índices de la API

**Impacto:** 1-3 segundos por cambio de filtro

**Solución:**
```python
# Pre-calcular KPIs agregados en DuckDB local
# Usar el Parquet ya procesado en lugar de consultar API
# Implementar caché de 1 hora para datos nacionales
```

---

### 3. **Procesamiento de Grafos Lento (ALTO)**
**Ubicación:** `network_analysis.py` - función `build_base_graph()`

**Problema:**
- Construye grafo completo cada vez (1500+ nodos)
- Cálculos de riesgo sin vectorización (loops en Pandas)
- Pyvis renderiza HTML pesado (>2 MB)
- Sin compresión de aristas

**Impacto:** 3-8 segundos por cambio de municipio

**Solución:**
```python
# Usar NetworkX con algoritmos optimizados
# Vectorizar cálculos con NumPy/Polars
# Implementar sub-grafos pre-calculados
# Comprimir aristas (agregar por modalidad)
```

---

### 4. **Mapas Choropleth Lentos (MEDIO)**
**Ubicación:** `app.py` - funciones `render_mapa_departamentos()`, `render_mapa_municipios()`

**Problema:**
- Plotly recalcula geometrías cada render
- GeoJSON sin simplificación (geometrías complejas)
- Merge de DataFrames sin índices

**Impacto:** 1-2 segundos por render

**Solución:**
```python
# Simplificar GeoJSON (reducir puntos en polígonos)
# Usar índices en merge
# Cachear figuras Plotly
```

---

### 5. **Sincronización de Estado Deficiente (MEDIO)**
**Ubicación:** `app.py` - funciones `set_dep()`, `set_mun()`, `init_state()`

**Problema:**
- `st.rerun()` causa re-renderizado completo
- No hay diferenciación entre cambios de estado
- Caché se invalida innecesariamente

**Impacto:** Parpadeos, re-renders innecesarios

**Solución:**
```python
# Usar callbacks en lugar de st.rerun()
# Implementar invalidación selectiva de caché
# Usar session_state más eficientemente
```

---

### 6. **Falta de Paginación en Tablas (MEDIO)**
**Ubicación:** `app.py` - función `render_tabla_entidades()`

**Problema:**
- Carga todas las entidades (20+) en una tabla
- Plotly renderiza todos los datos

**Impacto:** Lentitud en municipios con muchas entidades

**Solución:**
```python
# Implementar paginación (10 filas por página)
# Lazy loading de datos
```

---

### 7. **Consultas SoQL Subóptimas (MEDIO)**
**Ubicación:** `app.py` - función `soql_get()`

**Problema:**
- No usa `$limit` eficientemente
- Retorna columnas innecesarias
- Sin índices en la API

**Impacto:** Transferencia de datos innecesaria

**Solución:**
```python
# Especificar solo columnas necesarias en $select
# Usar $limit más agresivo
# Implementar compresión gzip
```

---

### 8. **Falta de Preload de Datos (BAJO)**
**Ubicación:** Toda la aplicación

**Problema:**
- Primer acceso es lento (sin caché)
- No hay precarga de años/departamentos

**Impacto:** Experiencia inicial pobre

**Solución:**
```python
# Precargar años y top 20 departamentos al iniciar
# Usar background tasks
```

---

## 📈 Benchmarks Actuales vs Propuestos

| Operación | Actual | Propuesto | Mejora |
|-----------|--------|-----------|--------|
| Cambio de departamento | 2-3s | 0.3-0.5s | **6-10x** |
| Cambio de municipio | 3-5s | 0.5-1s | **5-10x** |
| Cálculo de KPIs | 1-2s | 0.1-0.2s | **10-20x** |
| Render de grafo | 3-8s | 0.5-1s | **6-16x** |
| Carga inicial | 5-10s | 1-2s | **5-10x** |

---

## 🛠️ Plan de Implementación (Prioridad)

### Fase 1: Crítica (Impacto Alto, Esfuerzo Bajo)
1. ✅ Implementar caché persistente con DuckDB
2. ✅ Optimizar consultas SoQL (reducir columnas, límites)
3. ✅ Usar Parquet local para KPIs

### Fase 2: Alta (Impacto Alto, Esfuerzo Medio)
4. ✅ Vectorizar cálculos de riesgos en grafos
5. ✅ Simplificar GeoJSON
6. ✅ Implementar callbacks en lugar de st.rerun()

### Fase 3: Media (Impacto Medio, Esfuerzo Medio)
7. ✅ Paginación en tablas
8. ✅ Preload de datos

---

## 📊 Recomendaciones Técnicas

### Stack Recomendado
- **Caché:** DuckDB (ya en requirements.txt) + SQLite
- **Procesamiento:** Polars (más rápido que Pandas)
- **Grafos:** NetworkX + Pyvis (optimizado)
- **API:** Requests con retry + timeout adaptativo

### Configuración Recomendada
```python
# Timeouts adaptativos
API_TIMEOUT_QUICK = 10  # años, departamentos
API_TIMEOUT_NORMAL = 30  # municipios, entidades
API_TIMEOUT_SLOW = 60   # grafos, análisis

# Caché
CACHE_TTL_GLOBAL = 3600  # 1 hora
CACHE_TTL_LOCAL = 1800   # 30 min
CACHE_TTL_GRAFO = 900    # 15 min
```

---

## 🎯 Próximos Pasos

1. **Implementar caché persistente** (DuckDB)
2. **Optimizar consultas API** (batch queries)
3. **Vectorizar grafos** (Polars/NumPy)
4. **Simplificar GeoJSON** (Shapely)
5. **Implementar callbacks** (Streamlit)
6. **Agregar monitoreo** (logs de latencia)

