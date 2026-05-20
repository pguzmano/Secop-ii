# 🐛 DEBUG: Grafo No Se Actualiza con Municipio

## 🚨 Problema Reportado

**Síntoma:** El grafo se actualiza cuando cambias el departamento, pero NO cuando seleccionas un municipio.

---

## 🔍 ANÁLISIS DEL FLUJO

### Flujo Esperado

1. **Usuario hace clic en municipio** en el mapa (tab "Explorador Territorial")
2. **`set_mun()` actualiza session_state:**
   ```python
   st.session_state["mun_norm"] = mun_norm_click  # Para display
   st.session_state["mun_raw"] = mun_raw_click    # Para API WHERE
   ```
3. **`st.rerun()` recarga la app**
4. **`render_network_tab()` recibe `mun_raw` actualizado**
5. **`get_network_raw_data(anio, dep_raw, mun_raw)` consulta API con filtro de municipio**
6. **Grafo se regenera con datos del municipio**

### Código Relevante

**app.py línea 722:**
```python
set_mun(mun_norm=mun_norm_click, mun_raw=mun_raw_click)
st.rerun()
```

**app.py línea 999:**
```python
render_network_tab(anio, dep_raw, mun_raw)
```

**network_analysis.py línea 51:**
```python
@st.cache_data(ttl=300, show_spinner="Extraeción API (Fase 1)...")
def get_network_raw_data(anio: int, dep_raw: str = "", mun_raw: str = "") -> pd.DataFrame:
```

**network_analysis.py línea 63:**
```python
if mun_raw:
    safe_mun = mun_raw.replace("'", "''")
    conds.append(f"upper(ciudad) = '{safe_mun}'")
```

---

## ✅ VERIFICACIONES IMPLEMENTADAS

### 1. **Logs de Debug (Commit `c7f3622`)**

Agregado en `render_network_tab()`:
```python
print(f"[render_network_tab] anio={anio}, dep_raw='{dep_raw}', mun_raw='{mun_raw}'")
```

**Cómo verificar:**
1. Ir a Streamlit Cloud → "Manage app" → "View logs"
2. Seleccionar un municipio
3. Buscar línea: `[render_network_tab] anio=2026, dep_raw='...', mun_raw='...'`

**Resultado esperado:**
- Si `mun_raw` está vacío (`''`) → El problema está en `set_mun()` o `session_state`
- Si `mun_raw` tiene valor → El problema está en `get_network_raw_data()` o caché

---

### 2. **Indicador Visual de Filtros**

Agregado en `render_network_tab()`:
```python
if filtros_activos:
    st.markdown(f"""
    <div style='background:rgba(79,142,247,.1);...'>
        <b>🔍 Filtros activos:</b> {' | '.join(filtros_activos)}
    </div>
    """, unsafe_allow_html=True)
```

**Cómo verificar:**
1. Seleccionar un departamento → Debe mostrar: "📍 Departamento: BOLIVAR"
2. Seleccionar un municipio → Debe mostrar: "📍 Departamento: BOLIVAR | 🏙️ Municipio: CARTAGENA"

**Resultado esperado:**
- Si NO muestra el municipio → El problema está en `mun_raw` no llegando a `render_network_tab()`
- Si SÍ muestra el municipio → El problema está en la consulta API o caché

---

## 🔧 POSIBLES CAUSAS Y SOLUCIONES

### Causa 1: `mun_raw` No Se Actualiza en `session_state`

**Síntoma:** Logs muestran `mun_raw=''` incluso después de seleccionar municipio

**Solución:**
```python
# Verificar que set_mun() se está llamando correctamente
def set_mun(mun_norm: str, mun_raw: str):
    print(f"[set_mun] mun_norm='{mun_norm}', mun_raw='{mun_raw}'")  # ← Agregar log
    st.session_state["mun_norm"] = mun_norm
    st.session_state["mun_raw"]  = mun_raw
```

---

### Causa 2: Caché No Considera `mun_raw`

**Síntoma:** Logs muestran `mun_raw` correcto pero grafo no cambia

**Diagnóstico:**
- `@st.cache_data` considera TODOS los parámetros de la función
- Si `mun_raw` cambia, el caché debería invalidarse automáticamente

**Solución:** Limpiar caché manualmente
```python
# En sidebar, botón "🗑️ Limpiar caché"
st.cache_data.clear()
st.rerun()
```

---

### Causa 3: Consulta API No Filtra por Municipio

**Síntoma:** Logs muestran `mun_raw` correcto pero API retorna datos de todo el departamento

**Diagnóstico:**
```python
# network_analysis.py línea 63
if mun_raw:
    safe_mun = mun_raw.replace("'", "''")
    conds.append(f"upper(ciudad) = '{safe_mun}'")  # ← Verificar que esto se ejecuta
```

**Solución:** Agregar log en `get_network_raw_data()`:
```python
@st.cache_data(ttl=300, show_spinner="Extraeción API (Fase 1)...")
def get_network_raw_data(anio: int, dep_raw: str = "", mun_raw: str = "") -> pd.DataFrame:
    print(f"[get_network_raw_data] anio={anio}, dep_raw='{dep_raw}', mun_raw='{mun_raw}'")
    
    conds = [f"date_extract_y(fecha_de_firma) = {anio}"]
    # ...
    if mun_raw:
        safe_mun = mun_raw.replace("'", "''")
        conds.append(f"upper(ciudad) = '{safe_mun}'")
        print(f"[get_network_raw_data] Filtrando por municipio: {safe_mun}")
    
    where_clause = " AND ".join(conds)
    print(f"[get_network_raw_data] WHERE: {where_clause}")
```

---

### Causa 4: Nombre de Municipio No Coincide

**Síntoma:** API retorna 0 resultados para el municipio

**Diagnóstico:**
- El nombre del municipio en GeoJSON puede ser diferente al de la API
- Ejemplo: GeoJSON tiene "CARTAGENA" pero API tiene "CARTAGENA DE INDIAS"

**Solución:** Verificar nombres en API vs GeoJSON
```python
# Agregar en get_network_raw_data() después de consulta
print(f"[get_network_raw_data] Registros obtenidos: {len(data)}")
if len(data) == 0 and mun_raw:
    print(f"[get_network_raw_data] ⚠️ No hay datos para municipio '{mun_raw}'")
```

---

## 🧪 PASOS DE VERIFICACIÓN

### Paso 1: Verificar Logs en Streamlit Cloud

1. Desplegar cambios (commit `c7f3622`)
2. Ir a "Manage app" → "View logs"
3. Seleccionar un departamento (ej: BOLIVAR)
4. Ir a tab "Análisis de Redes"
5. Volver a tab "Explorador Territorial"
6. Seleccionar un municipio (ej: CARTAGENA)
7. Volver a tab "Análisis de Redes"
8. Buscar en logs:
   ```
   [render_network_tab] anio=2026, dep_raw='BOLIVAR', mun_raw='CARTAGENA'
   ```

### Paso 2: Verificar Indicador Visual

1. En tab "Análisis de Redes", debe aparecer:
   ```
   🔍 Filtros activos: 📍 Departamento: BOLIVAR | 🏙️ Municipio: CARTAGENA
   ```

### Paso 3: Verificar Datos API

1. Si logs muestran `mun_raw` correcto pero grafo no cambia:
   - Problema está en consulta API o caché
2. Si logs muestran `mun_raw=''`:
   - Problema está en `set_mun()` o `session_state`

---

## 🚀 PRÓXIMOS PASOS

### Si `mun_raw` Está Vacío en Logs

**Agregar más logs en `set_mun()`:**
```python
def set_mun(mun_norm: str, mun_raw: str):
    print(f"[set_mun] ANTES - mun_norm: {st.session_state.get('mun_norm')}, mun_raw: {st.session_state.get('mun_raw')}")
    st.session_state["mun_norm"] = mun_norm
    st.session_state["mun_raw"]  = mun_raw
    print(f"[set_mun] DESPUÉS - mun_norm: {st.session_state['mun_norm']}, mun_raw: {st.session_state['mun_raw']}")
```

### Si `mun_raw` Tiene Valor Pero Grafo No Cambia

**Agregar logs en `get_network_raw_data()`:**
```python
@st.cache_data(ttl=300, show_spinner="Extraeción API (Fase 1)...")
def get_network_raw_data(anio: int, dep_raw: str = "", mun_raw: str = "") -> pd.DataFrame:
    print(f"[get_network_raw_data] INICIO - anio={anio}, dep_raw='{dep_raw}', mun_raw='{mun_raw}'")
    
    # ... código ...
    
    print(f"[get_network_raw_data] WHERE clause: {where_clause}")
    print(f"[get_network_raw_data] Registros obtenidos: {len(data)}")
    
    return df_aggregated
```

### Si API Retorna 0 Registros

**Verificar nombres de municipios:**
```python
# Crear script de verificación
import requests
import pandas as pd

API_BASE = "https://www.datos.gov.co/resource/jbjy-vk9h.json"

# Obtener municipios únicos de BOLIVAR
params = {
    "$select": "DISTINCT upper(ciudad) AS ciudad",
    "$where": "upper(departamento) = 'BOLIVAR' AND ciudad IS NOT NULL",
    "$limit": "1000"
}

r = requests.get(API_BASE, params=params, timeout=30)
data = r.json()
df = pd.DataFrame(data)

print("Municipios en API para BOLIVAR:")
print(df['ciudad'].sort_values().tolist())
```

---

## 📊 RESUMEN

| Verificación | Método | Resultado Esperado |
|--------------|--------|-------------------|
| Logs de `render_network_tab` | View logs en Streamlit Cloud | `mun_raw` debe tener valor |
| Indicador visual | Interfaz de usuario | Debe mostrar municipio seleccionado |
| Logs de `get_network_raw_data` | View logs (si se agrega) | Debe mostrar filtro de municipio |
| Datos API | Logs de registros obtenidos | Debe retornar >0 registros |

---

**Última actualización:** Mayo 20, 2026  
**Commit:** `c7f3622`  
**Estado:** 🔍 EN DIAGNÓSTICO
