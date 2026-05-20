# 🔧 SOLUCIÓN: Bloqueo en Carga Inicial

## 🚨 Problema Reportado

**Síntoma:** El dashboard se queda en "Consultando años disponibles..." y no carga.

**Captura:** La app muestra el spinner pero nunca avanza.

---

## ✅ CORRECCIONES IMPLEMENTADAS

### Commit: `9477cbb` - "fix: Corregir bloqueo en carga inicial"

### 1. **Caché Más Robusto con Fallback**

**Problema anterior:**
- Si el caché SQLite fallaba (permisos, corrupción, etc.), la app se bloqueaba
- No había manejo de errores en inicialización del caché

**Solución:**
```python
class PersistentCache:
    def __init__(self, db_path: str = ".cache/api_cache.db"):
        self.db_path = db_path
        self.enabled = True  # ← NUEVO: Flag de habilitación
        try:
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
            self._init_db()
        except Exception as e:
            print(f"[CACHE] No se pudo inicializar caché persistente: {e}")
            print("[CACHE] Continuando sin caché persistente (solo caché de Streamlit)")
            self.enabled = False  # ← NUEVO: Desactivar si falla
```

**Beneficio:**
- Si el caché SQLite falla, la app continúa usando solo el caché de Streamlit
- No hay bloqueo por problemas de permisos o corrupción

---

### 2. **TTL Consistente (300s en Todos Lados)**

**Problema anterior:**
- `st.cache_data(ttl=300)` pero `_cache.set(ttl_seconds=3600)`
- Inconsistencia causaba datos obsoletos

**Solución:**
```python
# Antes
_cache.set(params, data, ttl_seconds=3600)  # 1 hora

# Después
_cache.set(params, data, ttl_seconds=300)   # 5 minutos (consistente)
```

---

### 3. **Manejo de Errores en get_anios()**

**Problema anterior:**
- Si la consulta fallaba, no había fallback
- Usuario veía spinner infinito

**Solución:**
```python
@st.cache_data(ttl=300, show_spinner="Consultando años disponibles...")
@monitor_latency("get_anios")
def get_anios() -> list[int]:
    try:
        df = soql_get({...})
        if df.empty or "anio" not in df.columns:
            print("[get_anios] DataFrame vacío, usando años por defecto")
            return [2026, 2025, 2024, 2023, 2022, 2021, 2020]
        # ... procesamiento ...
        return anios if anios else [2026, 2025, 2024, 2023]
    except Exception as e:
        print(f"[get_anios] Error: {e}")
        st.error(f"⚠️ Error obteniendo años: {e}")
        return [2026, 2025, 2024, 2023, 2022, 2021, 2020]  # ← FALLBACK
```

**Beneficio:**
- Siempre retorna años válidos, incluso si la API falla
- Usuario ve mensaje de error pero la app continúa

---

### 4. **Logs Detallados en soql_get()**

**Problema anterior:**
- No había visibilidad de qué estaba pasando
- Difícil debuggear en producción

**Solución:**
```python
def soql_get(params: dict) -> pd.DataFrame:
    # 1. Intentar caché
    try:
        cached = _cache.get(params)
        if cached is not None:
            print(f"[soql_get] ✅ Datos obtenidos del caché")
            return pd.DataFrame(cached)
    except Exception as e:
        print(f"[soql_get] ⚠️ Error leyendo caché: {e}")
    
    # 2. Consultar API
    print(f"[soql_get] 📡 Consultando API (timeout: {timeout}s)...")
    
    for attempt in range(max_retries + 1):
        try:
            # ... consulta ...
            print(f"[soql_get] ✅ Respuesta recibida: {len(data)} registros")
            # ...
        except requests.Timeout:
            print(f"[soql_get] ⏱️ Timeout en intento {attempt + 1}/{max_retries + 1}")
            # ...
        except Exception as e:
            print(f"[soql_get] ❌ Error en intento {attempt + 1}/{max_retries + 1}: {e}")
            # ...
```

**Beneficio:**
- Logs visibles en Streamlit Cloud (View logs)
- Fácil identificar dónde falla

---

### 5. **Timeout Adaptativo con Reintentos**

**Configuración:**
- Intento 1: 30s timeout
- Intento 2: 45s timeout (30 * 1.5)
- Intento 3: 67s timeout (45 * 1.5)

**Beneficio:**
- Más robusto ante conexiones lentas
- No se rinde al primer timeout

---

## 🧪 VERIFICACIÓN LOCAL

### Prueba 1: API Funciona
```bash
python test_api_simple.py
```

**Resultado esperado:**
```
✅ Status: 200
📊 Registros recibidos: 12
🎯 Primeros 5 años:
  1. Año 2026: 544345 contratos
  2. Año 2025: 1016475 contratos
  ...
✅ Consulta exitosa!
```

### Prueba 2: Import Sin Errores
```bash
python -c "import app; print('✅ Import OK')"
```

**Resultado esperado:**
```
✅ Import OK
```

---

## 🔍 DIAGNÓSTICO EN STREAMLIT CLOUD

### Paso 1: Ver Logs
1. Ir a Streamlit Cloud
2. Abrir tu app
3. Click en "Manage app" (esquina inferior derecha)
4. Click en "View logs"

### Paso 2: Buscar Estos Mensajes

**✅ Si funciona correctamente:**
```
[soql_get] 📡 Consultando API (timeout: 30s)...
[soql_get] ✅ Respuesta recibida: 12 registros
[get_anios] Años obtenidos: [2026, 2025, 2024, ...]
```

**⚠️ Si hay problemas de caché:**
```
[CACHE] No se pudo inicializar caché persistente: ...
[CACHE] Continuando sin caché persistente (solo caché de Streamlit)
```
→ **Esto es NORMAL en Streamlit Cloud** (no hay permisos de escritura en algunas carpetas)

**❌ Si hay timeout:**
```
[soql_get] ⏱️ Timeout en intento 1/3
[soql_get] ⏱️ Timeout en intento 2/3
[soql_get] ⏱️ Timeout en intento 3/3
```
→ Problema de red o API lenta

**❌ Si hay error de API:**
```
[soql_get] ❌ Error en intento 1/3: ...
```
→ Problema con la API de datos.gov.co

---

## 🚀 PRÓXIMOS PASOS

### Si el Problema Persiste

#### Opción 1: Limpiar Caché de Streamlit Cloud
1. En Streamlit Cloud, click en "⋮" (menú)
2. Click en "Reboot app"
3. Esperar a que reinicie

#### Opción 2: Verificar Límites de Streamlit Cloud
- **Timeout máximo:** 60 segundos
- **Memoria máxima:** 1 GB
- **CPU:** Compartida

Si la API tarda más de 60s, considera:
- Reducir `$limit` en las consultas
- Pre-filtrar por año más reciente
- Usar datos locales (Parquet)

#### Opción 3: Usar Datos Locales (Fallback Total)
Si la API está caída, modificar `get_anios()`:
```python
def get_anios() -> list[int]:
    # Fallback inmediato a años conocidos
    return [2026, 2025, 2024, 2023, 2022, 2021, 2020]
```

---

## 📊 RESUMEN DE CAMBIOS

| Cambio | Antes | Después | Beneficio |
|--------|-------|---------|-----------|
| Caché SQLite | Sin manejo de errores | Con fallback | No bloquea si falla |
| TTL caché | 3600s (inconsistente) | 300s (consistente) | Datos más frescos |
| get_anios() | Sin try-catch | Con fallback | Siempre retorna años |
| soql_get() | Sin logs | Con logs detallados | Fácil debuggear |
| Reintentos | 2 intentos | 3 intentos con timeout adaptativo | Más robusto |

---

## ✅ ESTADO ACTUAL

**Commit:** `9477cbb`  
**Branch:** master  
**Desplegado:** ✅ Sí (GitHub → Streamlit Cloud)

**Archivos modificados:**
- `app.py` - Caché más robusto y mejor manejo de errores
- `test_api_simple.py` - Script de prueba de API

---

## 📞 SOPORTE

Si el problema persiste después de estos cambios:

1. **Capturar logs de Streamlit Cloud** (View logs)
2. **Ejecutar `test_api_simple.py` localmente** para verificar que la API funciona
3. **Verificar que el commit `9477cbb` está desplegado** en Streamlit Cloud
4. **Considerar usar datos locales** si la API está caída

---

**Última actualización:** Mayo 20, 2026  
**Versión:** 3.1  
**Estado:** ✅ DESPLEGADO EN PRODUCCIÓN
