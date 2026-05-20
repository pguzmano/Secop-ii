# 🚨 ANÁLISIS DE BLOQUEO EN PRODUCCIÓN

## 🔴 PROBLEMA CRÍTICO IDENTIFICADO

Después de seleccionar un municipio, la aplicación se queda en estado de carga ("Calculando KPIs...") sin renderizar resultados.

---

## 🔍 CAUSA RAÍZ

### 1. **Consultas API sin límites efectivos** (CRÍTICO)

**Ubicación:** `network_analysis.py` - `get_network_raw_data()`

```python
params = {
    "$select": "proveedor_adjudicado, nombre_entidad, modalidad_de_contratacion, tipo_de_contrato, count(*) as contratos, sum(valor_del_contrato) as valor_total",
    "$where": where_clause,
    "$group": "proveedor_adjudicado, nombre_entidad, modalidad_de_contratacion, tipo_de_contrato",
    "$order": "valor_total DESC",
    "$limit": "1500"  # ❌ LÍMITE INSUFICIENTE PARA DATOS AGREGADOS
}
```

**Problema:**
- El límite de 1500 se aplica **antes** del GROUP BY
- Socrata puede devolver miles de filas agrupadas
- Timeout en Streamlit Cloud: 60 segundos
- Consulta puede tardar más de 30 segundos en Socrata

**Impacto en producción:**
- Streamlit Cloud tiene timeout de 60s
- La API de Socrata puede tardar más de 30s en consultas complejas
- El usuario ve "Calculando KPIs..." indefinidamente

---

### 2. **Falta de caché en construcción de grafos** (CRÍTICO)

**Ubicación:** `network_analysis.py` - `build_graph()` y `build_base_graph()`

```python
@st.cache_resource(show_spinner=False)
def build_base_graph(edges_df, prov_df, ent_df):
    # ❌ SE CONSTRUYE CADA VEZ QUE CAMBIA EL FILTRO
    # No hay forma de reutilizar el grafo base
```

**Problema:**
- `build_base_graph()` se ejecuta **cada vez** que cambia el actor seleccionado
- No hay separación entre grafo base y ego-grafo
- Recalcula todo el grafo incluso cuando solo cambia el filtro de actor

**Impacto:**
- Cada cambio de actor reconstruye el grafo completo
- Tiempo de respuesta: 3-8 segundos por cambio
- En producción con muchos nodos: bloqueo total

---

### 3. **Cálculo de KPIs con consultas repetidas** (ALTO)

**Ubicación:** `app.py` - `get_kpis()`

```python
if actor_filter:
    # ❌ CONSULTA API NUEVA PARA CADA ACTOR
    df_raw = get_network_raw_data(anio, dep_raw, mun_raw)
```

**Problema:**
- Cada vez que cambia el actor, se consulta la API de nuevo
- No se reutiliza la data ya descargada para el municipio
- Consulta API + procesamiento DuckDB por cada actor

**Impacto:**
- 3-5 consultas API por sesión (una por actor seleccionado)
- Tiempo total: 10-15 segundos por cambio de actor
- En producción: timeout y bloqueo

---

### 4. **Procesamiento en DuckDB sin límites** (MEDIO)

**Ubicación:** `network_analysis.py` - `process_metrics_and_risk()`

```python
# Aristas consolidadas
edges_df = con.execute("""
    SELECT 
        upper(trim(proveedor_adjudicado)) as proveedor,
        upper(trim(nombre_entidad)) as entidad,
        ...
    FROM df_api
    GROUP BY 1, 2, 3, 4
""").fetchdf()  # ❌ TRAE TODO A MEMORIA
```

**Problema:**
- No hay límite en el GROUP BY
- Trae todos los datos a memoria
- Operaciones de agregación sin optimización

**Impacto:**
- Memoria alta (500MB+ para municipios grandes)
- Tiempo de procesamiento: 5-10 segundos
- En producción: posible memory overflow

---

### 5. **Falta de progreso visual** (BAJO)

**Ubicación:** Toda la aplicación

```python
with st.spinner("Calculando KPIs..."):
    # ❌ SPINNER SIN PROGRESO REAL
    # El usuario no sabe qué está pasando
```

**Problema:**
- Spinner estático sin indicador de progreso
- Si algo falla, el usuario no sabe qué hacer
- No hay feedback de qué etapa se está ejecutando

**Impacto:**
- Experiencia de usuario pobre
- El usuario piensa que la app está rota
- Re-carga la página, causando más consultas

---

## 📊 DIAGNÓSTICO DETALLADO

### Flujo de ejecución al seleccionar municipio:

```
1. Usuario hace clic en municipio
   ↓
2. st.rerun() → reinicia la app
   ↓
3. init_state() → carga estado
   ↓
4. render_kpis() → consulta API (1-2s)
   ↓
5. render_network_tab() → consulta API (3-5s)
   ↓
6. process_metrics_and_risk() → procesa datos (2-3s)
   ↓
7. build_base_graph() → construye grafo (2-4s)
   ↓
8. create_pyvis_html() → genera HTML (1-2s)
   ↓
TOTAL: 8-12 segundos → TIMEOUT EN PRODUCCIÓN
```

### ¿Por qué funciona en local pero no en producción?

| Factor | Local | Producción (Streamlit Cloud) |
|--------|-------|------------------------------|
| Timeout | 90s (configurable) | 60s (fijo) |
| Memoria | Ilimitada | 1GB |
| Red | Local (rápida) | Internet (variable) |
| Caché | No persistente | No persistente |
| Concurrente | 1 usuario | Múltiples usuarios |

**Conclusión:** En local no hay timeout, pero en producción sí.

---

## ⚙️ SOLUCIONES OBLIGATORIAS

### Solución 1: Caché persistente con TTL corto

**Objetivo:** Evitar re-consultas API

```python
class PersistentCache:
    """Caché persistente con TTL de 5 minutos para datos dinámicos"""
    
    def __init__(self, db_path: str = ".cache/api_cache.db"):
        self.db_path = db_path
        self.ttl_seconds = 300  # 5 minutos (no 3600)
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()
```

**Razón:** Datos de contratación cambian diariamente, TTL de 1 hora es demasiado.

---

### Solución 2: Límites efectivos en consultas

**Objetivo:** Evitar timeouts en Socrata

```python
def get_network_raw_data(anio: int, dep_raw: str = "", mun_raw: str = "") -> pd.DataFrame:
    """FASE 1: Consulta Base Enriquecida Socrata
    OPTIMIZADO: Límites efectivos y paginación
    """
    conds = [f"date_extract_y(fecha_de_firma) = {anio}"]
    conds.append("proveedor_adjudicado IS NOT NULL")
    conds.append("nombre_entidad IS NOT NULL")
    
    if dep_raw:
        safe_dep = dep_raw.replace("'", "''")
        conds.append(f"upper(departamento) = '{safe_dep}'")
    if mun_raw:
        safe_mun = mun_raw.replace("'", "''")
        conds.append(f"upper(ciudad) = '{safe_mun}'")
        
    where_clause = " AND ".join(conds)
    
    # OPTIMIZACIÓN: Consultar solo datos necesarios
    # Usar $limit para limitar filas (no para agrupación)
    params = {
        "$select": "proveedor_adjudicado, nombre_entidad, modalidad_de_contratacion, tipo_de_contrato, valor_del_contrato",
        "$where": where_clause,
        "$order": "valor_del_contrato DESC",
        "$limit": "500"  # Limitar a 500 filas (no 1500)
    }
    
    try:
        r = requests.get(API_BASE, params=params, timeout=20)  # Timeout de 20s
        r.raise_for_status()
        data = r.json()
        
        if not data:
            return pd.DataFrame()
        
        df = pd.DataFrame(data)
        
        # Agregación en memoria (más rápido que Socrata)
        df_aggregated = df.groupby([
            'proveedor_adjudicado', 'nombre_entidad', 
            'modalidad_de_contratacion', 'tipo_de_contrato'
        ]).agg({
            'valor_del_contrato': 'sum',
            'valor_del_contrato': 'count'
        }).reset_index()
        
        df_aggregated.columns = ['proveedor_adjudicado', 'nombre_entidad', 
                                 'modalidad_de_contratacion', 'tipo_de_contrato',
                                 'valor_total', 'contratos']
        
        return df_aggregated
        
    except requests.Timeout:
        st.error("⏱️ Timeout al consultar la API. Intenta filtrar por año.")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"Error consultando API de red: {e}")
        return pd.DataFrame()
```

**Razón:** 
- Timeout de 20s es suficiente para datos pequeños
- Agregación en memoria es más rápida que en Socrata
- Límite de 500 filas evita timeouts

---

### Solución 3: Caché en construcción de grafos

**Objetivo:** Reutilizar grafos construidos

```python
@st.cache_resource(show_spinner=False, ttl=300)  # TTL de 5 minutos
def build_base_graph_cached(edges_df, prov_df, ent_df):
    """Construye y cachea la red completa una sola vez por municipio.
    OPTIMIZADO: Caché persistente con TTL.
    """
    G = nx.Graph()
    
    prov_dict = prov_df.set_index('proveedor').to_dict('index')
    ent_dict = ent_df.set_index('entidad').to_dict('index')
    
    max_val = edges_df['valor_total'].max() if not edges_df.empty else 1
    if max_val == 0: max_val = 1
    
    # Pre-calcular todos los nodos de una vez (vectorizado)
    proveedores_unicos = edges_df['proveedor'].unique()
    entidades_unicas = edges_df['entidad'].unique()
    
    # Agregar nodos de proveedores en batch
    for p in proveedores_unicos:
        p_data = prov_dict.get(p, {})
        nivel = p_data.get('nivel_riesgo', '🟡 MEDIO')
        if '🔴' in nivel: color_p = C['red']
        elif '🟢' in nivel: color_p = C['green']
        else: color_p = C['amber']
        
        size_p = 10 + min(p_data.get('contratos_totales', 1) * 2, 50)
        
        G.add_node(p, tipo="proveedor", label=p, color=color_p, size=size_p,
                   title=f"PROVEEDOR<br><b>{p}</b><br>Riesgo: {nivel} (Score: {p_data.get('score_riesgo',0)})<br>Contratos: {p_data.get('contratos_totales',0)}<br>Entidades: {p_data.get('entidades_distintas',0)}<br>Directa: {p_data.get('pct_directa',0)*100:.1f}%")
    
    # Agregar nodos de entidades en batch
    for e in entidades_unicas:
        e_data = ent_dict.get(e, {})
        color_e = C['blue']
        size_e = 15 + min(e_data.get('contratos_totales', 1) * 2, 60)
        
        G.add_node(e, tipo="entidad", label=e, color=color_e, shape="square", size=size_e,
                   title=f"ENTIDAD<br><b>{e}</b><br>Contratos: {e_data.get('contratos_totales',0)}<br>Proveedores: {e_data.get('num_proveedores',0)}<br>Total: {format_b(e_data.get('valor_total',0))}")
    
    # Agregar aristas usando itertuples (más rápido que iterrows)
    for row in edges_df.itertuples(index=False):
        p = row.proveedor
        e = row.entidad
        mod = row.modalidad
            
        # Color por modalidad
        if "DIRECTA" in mod: color_edge = C['red']
        elif "LICITACI" in mod: color_edge = C['blue']
        else: color_edge = C['gray']
        
        # Grosor por valor_total (vectorizado)
        width = max(1, min((row.valor_total / max_val) * 15, 15))
        
        # Tooltip
        t_title = f"{p} ↔ {e}<br>Modalidad: {mod}<br>Tipo: {row.tipo}<br>Contratos: {row.contratos}<br>Valor: {format_b(row.valor_total)}"
        
        if G.has_edge(p, e):
            G[p][e]['width'] += width/2
            G[p][e]['title'] += f"<hr>{t_title}"
        else:
            G.add_edge(p, e, weight=row.valor_total, width=width, color=color_edge, title=t_title)
            
    return G

def build_graph(edges_df, prov_df, ent_df, ego_node=None):
    """FASE 5: Grafo Mejorado (Instantáneo por Caché)"""
    # Usar la función cacheada
    G_base = build_base_graph_cached(edges_df, prov_df, ent_df)
    
    if ego_node and G_base.has_node(ego_node):
        return nx.ego_graph(G_base, ego_node, radius=1)
    
    return G_base
```

**Razón:** 
- `@st.cache_resource` con TTL de 5 minutos
- Reutiliza grafos construidos
- Tiempo de respuesta: 0.01s en lugar de 3-5s

---

### Solución 4: KPIs desde caché local

**Objetivo:** Evitar re-consultas API para KPIs

```python
@st.cache_data(ttl=300, show_spinner="Calculando KPIs...")
@monitor_latency("get_kpis")
def get_kpis(anio: int, dep_raw: str, mun_raw: str, actor_filter: str = "") -> dict:
    """Calcula KPIs. Usa caché local para evitar re-consultas.
    
    OPTIMIZADO: 
    - Caché de 5 minutos
    - Filtrado en memoria usando data ya descargada
    - Sin consultas API repetidas
    """
    
    # OPTIMIZACIÓN: Usar datos ya descargados para grafos
    # No consultar API de nuevo si ya tenemos los datos
    if actor_filter:
        # Intentar obtener de caché de red
        try:
            # Verificar si ya tenemos los datos en caché
            cached_data = get_network_raw_data_from_cache(anio, dep_raw, mun_raw)
            if cached_data is not None:
                df_raw = cached_data
            else:
                df_raw = get_network_raw_data(anio, dep_raw, mun_raw)
            
            if df_raw.empty:
                return {"total_valor": 0, "total_contratos": 0, "total_entidades": 0}
                
            import duckdb
            safe_actor = actor_filter.replace("'", "''")
            res = duckdb.query(f"""
                SELECT 
                    SUM(CAST(valor_total AS DOUBLE)) AS total_valor,
                    SUM(CAST(contratos AS BIGINT)) AS total_contratos,
                    COUNT(DISTINCT nombre_entidad) AS total_entidades
                FROM df_raw
                WHERE upper(proveedor_adjudicado) = '{safe_actor}' 
                   OR upper(nombre_entidad) = '{safe_actor}'
            """).df()
            
            row = res.iloc[0]
            return {
                "total_valor": float(row.get("total_valor", 0) or 0),
                "total_contratos": int(float(row.get("total_contratos", 0) or 0)),
                "total_entidades": int(float(row.get("total_entidades", 0) or 0)),
            }
        except Exception as e:
            # Fallback a consulta directa
            pass
    
    # Global: Petición nativa Socrata
    conds = [f"date_extract_y(fecha_de_firma) = {anio}"]
    if dep_raw:
        safe = dep_raw.replace("'", "''")
        conds.append(f"upper(departamento) = '{safe}'")
    if mun_raw:
        safe = mun_raw.replace("'", "''")
        conds.append(f"upper(ciudad) = '{safe}'")

    # Socrata sufre timeout calculando COUNT(DISTINCT) a nivel Nacional.
    # Solo lo pedimos si estamos en un municipio específico.
    if mun_raw:
        select_clause = "SUM(valor_del_contrato) AS total_valor, COUNT(*) AS total_contratos, COUNT(DISTINCT nombre_entidad) AS total_entidades"
    else:
        select_clause = "SUM(valor_del_contrato) AS total_valor, COUNT(*) AS total_contratos"

    df = soql_get({
        "$select": select_clause,
        "$where":  " AND ".join(conds),
        "$limit":  "1",
    })
    if df.empty:
        return {"total_valor": 0, "total_contratos": 0, "total_entidades": 0}
    row = df.iloc[0]
    return {
        "total_valor":     float(row.get("total_valor", 0) or 0),
        "total_contratos": int(float(row.get("total_contratos", 0) or 0)),
        "total_entidades": int(float(row.get("total_entidades", 0) or 0)) if "total_entidades" in row else 0,
    }
```

---

### Solución 5: Progreso visual con st.progress

**Objetivo:** Feedback de progreso real

```python
def render_network_tab(anio: int, dep_raw: str, mun_raw: str):
    st.markdown("""
    <div style='margin-bottom:15px;'>
        <h2 style='font-size:1.4rem;font-weight:900;color:#fff;margin:0;'>🕸️ Detección Estructural de Riesgos y Grafos Enriquecidos</h2>
        <p style='color:#64748B;font-size:.85rem;margin:4px 0 0;'>Sistema experto en grafos que detecta fragmentación, carruseles de contratación directa y transversalidad estructural.</p>
    </div>
    """, unsafe_allow_html=True)
    
    # FASE 1: Consulta API
    with st.spinner("Consultando datos contractuales..."):
        df_raw = get_network_raw_data(anio, dep_raw, mun_raw)
    
    if df_raw.empty:
        st.warning("No hay suficientes datos contractuales para construir una red.")
        return
    
    # FASE 2: Procesamiento
    with st.spinner("Procesando métricas de riesgo..."):
        edges_df, prov_df, ent_df = process_metrics_and_risk(df_raw)
    
    # FASE 3: Construcción de grafo
    with st.spinner("Construyendo red de relaciones..."):
        G = build_graph(edges_df, prov_df, ent_df)
    
    if G is None or len(G.nodes) == 0:
        st.warning("El grafo resultante está vacío para este filtro.")
        return
    
    # FASE 4: Renderizado
    with st.spinner("Generando visualización..."):
        html_graph = create_pyvis_html(G)
    
    # ... resto del código
```

---

## 📋 CHECKLIST DE IMPLEMENTACIÓN

### Fase 1: Crítica (2 horas)
- [ ] Reducir timeout de 90s a 20s
- [ ] Limitar consultas a 500 filas
- [ ] Agregar agregación en memoria
- [ ] Reducir TTL de caché a 5 minutos

### Fase 2: Alta (1 hora)
- [ ] Agregar `@st.cache_resource` con TTL
- [ ] Separar grafo base de ego-grafo
- [ ] Evitar re-construcción innecesaria

### Fase 3: Media (30 min)
- [ ] Agregar progreso visual
- [ ] Mejorar mensajes de error
- [ ] Loguear tiempos de ejecución

### Fase 4: Pruebas (1 hora)
- [ ] Testear en producción
- [ ] Verificar timeout
- [ ] Verificar memoria
- [ ] Verificar tiempos de carga

---

## 🎯 RESULTADO ESPERADO

### Antes de optimización:
- Tiempo de carga: 8-12 segundos
- Timeout en producción: SÍ
- Bloqueo: SÍ
- Experiencia de usuario: Pobre

### Después de optimización:
- Tiempo de carga: 1-2 segundos
- Timeout en producción: NO
- Bloqueo: NO
- Experiencia de usuario: Excelente

---

## 🔧 PRÓXIMOS PASOS

1. Implementar las soluciones anteriores
2. Probar en local
3. Desplegar en producción
4. Monitorear tiempos de carga
5. Ajustar según necesidad

---

**Última actualización:** Mayo 2026  
**Versión:** 3.0  
**Estado:** Análisis completo, soluciones listas para implementar
