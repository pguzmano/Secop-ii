# ✅ CHECKLIST DE IMPLEMENTACIÓN

## 📋 Preparación (15 min)

- [ ] Leer `RESUMEN_EJECUTIVO.md`
- [ ] Leer `ANALISIS_RENDIMIENTO.md`
- [ ] Leer `GUIA_IMPLEMENTACION.md`
- [ ] Crear rama de git: `git checkout -b optimize/performance`
- [ ] Hacer backup de `app.py` y `network_analysis.py`

---

## 🔧 Fase 1: Caché Persistente (30 min)

### Paso 1.1: Actualizar dependencias
- [ ] Abrir `requirements.txt`
- [ ] Verificar que `sqlite3` está disponible (incluido en Python)
- [ ] Guardar cambios

### Paso 1.2: Copiar clase PersistentCache
- [ ] Abrir `OPTIMIZACIONES_INMEDIATAS.py`
- [ ] Copiar clase `PersistentCache` (líneas ~30-80)
- [ ] Pegar en `app.py` después de imports
- [ ] Crear instancia global: `_cache = PersistentCache()`

### Paso 1.3: Reemplazar soql_get
- [ ] Copiar función `soql_get_optimized()` de `OPTIMIZACIONES_INMEDIATAS.py`
- [ ] Pegar en `app.py`
- [ ] Reemplazar función `soql_get()` original con:
  ```python
  def soql_get(params: dict) -> pd.DataFrame:
      return soql_get_optimized(
          params,
          api_base=API_BASE,
          api_timeout=API_TIMEOUT,
          cache_ttl=3600,
          max_retries=2
      )
  ```

### Paso 1.4: Testear
- [ ] Ejecutar: `streamlit run app.py`
- [ ] Cambiar de departamento (anotar tiempo)
- [ ] Cambiar a otro departamento (anotar tiempo)
- [ ] Volver al primero (debe ser <0.5s)
- [ ] Verificar que `.cache/api_cache.db` se creó

### Paso 1.5: Validar
- [ ] ✅ Caché funciona (segundo acceso es rápido)
- [ ] ✅ No hay errores en consola
- [ ] ✅ Datos son correctos

---

## 🔄 Fase 2: Batch Queries (20 min)

### Paso 2.1: Copiar función batch
- [ ] Copiar `get_departamentos_y_municipios_batch()` de `OPTIMIZACIONES_INMEDIATAS.py`
- [ ] Pegar en `app.py`

### Paso 2.2: Actualizar get_departamentos
- [ ] Reemplazar función `get_departamentos()` con:
  ```python
  @st.cache_data(ttl=3600, show_spinner="Cargando geografía...")
  def get_departamentos(anio: int) -> pd.DataFrame:
      df_deps, _ = get_departamentos_y_municipios_batch(anio, API_BASE)
      if not df_deps.empty:
          df_deps["valor"] = pd.to_numeric(df_deps["valor"], errors="coerce").fillna(0)
          df_deps["contratos"] = pd.to_numeric(df_deps["contratos"], errors="coerce").fillna(0)
      return df_deps
  ```

### Paso 2.3: Actualizar get_municipios
- [ ] Reemplazar función `get_municipios()` con:
  ```python
  @st.cache_data(ttl=3600, show_spinner="Cargando municipios...")
  def get_municipios(anio: int, dep_raw: str) -> pd.DataFrame:
      _, muns_por_dep = get_departamentos_y_municipios_batch(anio, API_BASE)
      df = muns_por_dep.get(dep_raw, pd.DataFrame())
      if not df.empty:
          df["valor"] = pd.to_numeric(df["valor"], errors="coerce").fillna(0)
          df["contratos"] = pd.to_numeric(df["contratos"], errors="coerce").fillna(0)
      return df
  ```

### Paso 2.4: Testear
- [ ] Ejecutar: `streamlit run app.py`
- [ ] Medir tiempo de carga inicial (debe ser <2s)
- [ ] Cambiar de departamento (debe ser <0.5s)
- [ ] Cambiar de municipio (debe ser <1s)

### Paso 2.5: Validar
- [ ] ✅ Carga inicial es rápida
- [ ] ✅ Datos son correctos
- [ ] ✅ No hay errores

---

## 📊 Fase 3: KPIs desde Parquet (20 min)

### Paso 3.1: Verificar Parquet
- [ ] Verificar que `data/secop.parquet` existe
- [ ] Si no existe, ejecutar: `python etl_secop.py`

### Paso 3.2: Copiar función
- [ ] Copiar `get_kpis_from_parquet()` de `OPTIMIZACIONES_INMEDIATAS.py`
- [ ] Pegar en `app.py`

### Paso 3.3: Actualizar get_kpis
- [ ] Reemplazar función `get_kpis()` con:
  ```python
  @st.cache_data(ttl=3600, show_spinner="Calculando KPIs...")
  def get_kpis(anio: int, dep_raw: str, mun_raw: str, actor_filter: str = "") -> dict:
      if actor_filter:
          # Mantener lógica original para filtro de actor
          df_raw = get_network_raw_data(anio, dep_raw, mun_raw)
          if df_raw.empty:
              return {"total_valor": 0, "total_contratos": 0, "total_entidades": 0}
          # ... resto de lógica original ...
      else:
          # Usar Parquet para KPIs normales
          return get_kpis_from_parquet(anio, dep_raw, mun_raw, "data/secop.parquet")
  ```

### Paso 3.4: Testear
- [ ] Ejecutar: `streamlit run app.py`
- [ ] Cambiar de departamento (anotar tiempo de KPIs)
- [ ] Cambiar de municipio (anotar tiempo de KPIs)
- [ ] Tiempos deben ser <0.2s

### Paso 3.5: Validar
- [ ] ✅ KPIs se calculan rápidamente
- [ ] ✅ Valores son correctos
- [ ] ✅ No hay errores

---

## 🚀 Fase 4: Vectorizar Grafos (30 min)

### Paso 4.1: Instalar Polars
- [ ] Ejecutar: `pip install polars>=0.20.0`
- [ ] Verificar: `python -c "import polars; print(polars.__version__)"`

### Paso 4.2: Copiar función
- [ ] Copiar `calcular_riesgo_vectorizado()` de `OPTIMIZACIONES_INMEDIATAS.py`
- [ ] Pegar en `network_analysis.py`

### Paso 4.3: Actualizar process_metrics_and_risk
- [ ] En `network_analysis.py`, reemplazar la sección de cálculo de riesgo con:
  ```python
  # Usar Polars para vectorización
  prov_df = calcular_riesgo_vectorizado(df_raw)
  
  # Asignar nivel de riesgo
  def asignar_nivel(row):
      if row['pct_directa'] >= 0.70 and row['entidades_distintas'] > 1:
          return '🔴 ALTO'
      elif row['pct_licitacion'] >= 0.50 and row['contratos_totales'] < 5:
          return '🟢 BAJO'
      else:
          return '🟡 MEDIO'
  
  prov_df['nivel_riesgo'] = prov_df.apply(asignar_nivel, axis=1)
  ```

### Paso 4.4: Testear
- [ ] Ejecutar: `streamlit run app.py`
- [ ] Ir a tab de Redes
- [ ] Cambiar de municipio (anotar tiempo)
- [ ] Tiempo debe ser <1s

### Paso 4.5: Validar
- [ ] ✅ Grafo se renderiza rápidamente
- [ ] ✅ Datos de riesgo son correctos
- [ ] ✅ No hay errores

---

## 🗺️ Fase 5: Simplificar GeoJSON (20 min)

### Paso 5.1: Instalar Shapely
- [ ] Ejecutar: `pip install shapely>=2.0.0`
- [ ] Verificar: `python -c "import shapely; print(shapely.__version__)"`

### Paso 5.2: Simplificar archivos
- [ ] Ejecutar:
  ```bash
  python -c "
  from OPTIMIZACIONES_INMEDIATAS import simplificar_geojson
  import json
  
  # Departamentos
  geo = simplificar_geojson('data/depto.json', tolerance=0.01)
  with open('data/depto.json', 'w') as f:
      json.dump(geo, f)
  
  # Municipios
  geo = simplificar_geojson('data/mpio.json', tolerance=0.005)
  with open('data/mpio.json', 'w') as f:
      json.dump(geo, f)
  
  print('✅ GeoJSON simplificado')
  "
  ```

### Paso 5.3: Testear
- [ ] Ejecutar: `streamlit run app.py`
- [ ] Ir a tab de Mapas
- [ ] Verificar que mapas se renderizan rápidamente (<1s)
- [ ] Verificar que geometrías se ven bien

### Paso 5.4: Validar
- [ ] ✅ Mapas se renderizan rápidamente
- [ ] ✅ Geometrías se ven correctas
- [ ] ✅ No hay errores

---

## 📈 Fase 6: Monitoreo de Latencia (15 min)

### Paso 6.1: Copiar decorador
- [ ] Copiar `monitor_latency()` de `OPTIMIZACIONES_INMEDIATAS.py`
- [ ] Pegar en `app.py`

### Paso 6.2: Decorar funciones críticas
- [ ] Agregar `@monitor_latency("get_departamentos")` a `get_departamentos()`
- [ ] Agregar `@monitor_latency("get_municipios")` a `get_municipios()`
- [ ] Agregar `@monitor_latency("get_kpis")` a `get_kpis()`
- [ ] Agregar `@monitor_latency("get_entidades")` a `get_entidades()`

### Paso 6.3: Crear directorio de logs
- [ ] Ejecutar: `mkdir -p .logs`

### Paso 6.4: Testear
- [ ] Ejecutar: `streamlit run app.py`
- [ ] Usar la app normalmente
- [ ] Ejecutar: `tail -f .logs/latency.log`
- [ ] Verificar que se registran tiempos

### Paso 6.5: Validar
- [ ] ✅ Logs se crean correctamente
- [ ] ✅ Tiempos se registran
- [ ] ✅ Todos <1s

---

## 🎯 Fase 7: Preload de Datos (10 min)

### Paso 7.1: Copiar función
- [ ] Copiar `preload_top_departments()` de `OPTIMIZACIONES_INMEDIATAS.py`
- [ ] Pegar en `app.py`

### Paso 7.2: Actualizar main()
- [ ] En función `main()`, después de `init_state()`, agregar:
  ```python
  # Precargar datos
  if not st.session_state.get("preload_done"):
      preload_top_departments(anios[0], API_BASE)
      st.session_state["preload_done"] = True
  ```

### Paso 7.3: Testear
- [ ] Ejecutar: `streamlit run app.py`
- [ ] Primera carga debe mostrar datos rápidamente
- [ ] Medir tiempo de carga inicial (debe ser <2s)

### Paso 7.4: Validar
- [ ] ✅ Datos se precargan
- [ ] ✅ Primera carga es rápida
- [ ] ✅ No hay errores

---

## 🧪 Fase 8: Testing Completo (30 min)

### Paso 8.1: Ejecutar test_performance.py
- [ ] Ejecutar: `python test_performance.py --baseline`
- [ ] Anotar resultados
- [ ] Ejecutar: `python test_performance.py --optimized`
- [ ] Anotar resultados
- [ ] Ejecutar: `python test_performance.py --compare`

### Paso 8.2: Revisar resultados
- [ ] Abrir `.performance/baseline.json`
- [ ] Abrir `.performance/optimized.json`
- [ ] Comparar tiempos
- [ ] Verificar que mejora es 5-10x

### Paso 8.3: Validar
- [ ] ✅ Mejora de 5-10x en operaciones críticas
- [ ] ✅ Todos los tests pasan
- [ ] ✅ No hay errores

---

## 📝 Fase 9: Documentación (10 min)

### Paso 9.1: Actualizar README
- [ ] Agregar sección "Optimizaciones de Rendimiento"
- [ ] Documentar cambios realizados
- [ ] Incluir benchmarks

### Paso 9.2: Crear CHANGELOG
- [ ] Crear archivo `CHANGELOG.md`
- [ ] Documentar todas las optimizaciones
- [ ] Incluir fecha y versión

### Paso 9.3: Validar
- [ ] ✅ Documentación está completa
- [ ] ✅ Benchmarks están documentados

---

## 🚀 Fase 10: Deployment (15 min)

### Paso 10.1: Commit y Push
- [ ] Ejecutar: `git add -A`
- [ ] Ejecutar: `git commit -m "perf: optimizar tiempos de respuesta 5-10x"`
- [ ] Ejecutar: `git push origin optimize/performance`

### Paso 10.2: Crear Pull Request
- [ ] Ir a GitHub/GitLab
- [ ] Crear PR desde `optimize/performance` a `main`
- [ ] Incluir descripción de cambios
- [ ] Incluir benchmarks

### Paso 10.3: Code Review
- [ ] Pedir review a compañeros
- [ ] Resolver comentarios
- [ ] Aprobar PR

### Paso 10.4: Merge
- [ ] Mergear PR a `main`
- [ ] Eliminar rama: `git branch -d optimize/performance`

### Paso 10.5: Deploy
- [ ] Deployar a producción
- [ ] Monitorear logs
- [ ] Verificar que todo funciona

### Paso 10.6: Validar
- [ ] ✅ Cambios están en main
- [ ] ✅ App funciona en producción
- [ ] ✅ Usuarios reportan mejora

---

## 📊 Resumen Final

### Tiempo Total
- Fase 1: 30 min ✅
- Fase 2: 20 min ✅
- Fase 3: 20 min ✅
- Fase 4: 30 min ✅
- Fase 5: 20 min ✅
- Fase 6: 15 min ✅
- Fase 7: 10 min ✅
- Fase 8: 30 min ✅
- Fase 9: 10 min ✅
- Fase 10: 15 min ✅

**Total: 3.5-4 horas**

### Beneficios Esperados
- ✅ 5-10x más rápido
- ✅ 80% menos consultas API
- ✅ Mejor experiencia de usuario
- ✅ Escalabilidad mejorada

### Próximos Pasos
- [ ] Monitorear logs de latencia
- [ ] Recopilar feedback de usuarios
- [ ] Considerar Fase 2 (Redis, CDN, etc.)

---

## ✅ COMPLETADO

Cuando hayas completado todos los pasos, marca este checklist como completado:

- [ ] Todas las fases implementadas
- [ ] Todos los tests pasan
- [ ] Documentación actualizada
- [ ] Cambios en producción
- [ ] Usuarios reportan mejora

**Felicidades! 🎉 Has optimizado el dashboard 5-10x**

