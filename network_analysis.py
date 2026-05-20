import os
import requests
import pandas as pd
import duckdb
import numpy as np
import networkx as nx
from pyvis.network import Network
import streamlit as st
import streamlit.components.v1 as components

API_RESOURCE = "jbjy-vk9h"
API_BASE     = f"https://www.datos.gov.co/resource/{API_RESOURCE}.json"
API_TIMEOUT  = 120

C = dict(
    bg="#060B14", card="#0D1421", border="#1A2336",
    blue="#4F8EF7", green="#22C55E", amber="#F59E0B", red="#F43F5E",
    purple="#A78BFA", text="#F1F5F9", muted="#64748B",
    gray="#9CA3AF"
)

def format_b(v):
    try:
        v = float(v)
        if v >= 1e12: return f"${v/1e12:.2f} B"
        if v >= 1e9:  return f"${v/1e9:.1f} MM"
        if v >= 1e6:  return f"${v/1e6:.1f} M"
        return f"${v:,.0f}"
    except: return "—"

@st.cache_data(ttl=3600, show_spinner="Extraeción API (Fase 1)...")
def get_network_raw_data(anio: int, dep_raw: str = "", mun_raw: str = "") -> pd.DataFrame:
    """FASE 1: Consulta Base Enriquecida Socrata"""
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
    
    params = {
        "$select": "proveedor_adjudicado, nombre_entidad, modalidad_de_contratacion, tipo_de_contrato, count(*) as contratos, sum(valor_del_contrato) as valor_total",
        "$where": where_clause,
        "$group": "proveedor_adjudicado, nombre_entidad, modalidad_de_contratacion, tipo_de_contrato",
        "$order": "valor_total DESC",
        "$limit": "1500"
    }
    
    try:
        r = requests.get(API_BASE, params=params, timeout=API_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"Error consultando API de red: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=3600, show_spinner="Procesando Métricas y Riesgo (Fase 2-4)...")
def process_metrics_and_risk(df_raw: pd.DataFrame):
    """FASE 2, 3 y 4: Métricas Clave y Score de Riesgo usando DuckDB"""
    if df_raw.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        
    con = duckdb.connect()
    con.register("df_api", df_raw)
    
    # Aristas consolidadas
    edges_df = con.execute("""
        SELECT 
            upper(trim(proveedor_adjudicado)) as proveedor,
            upper(trim(nombre_entidad)) as entidad,
            upper(trim(modalidad_de_contratacion)) as modalidad,
            upper(trim(tipo_de_contrato)) as tipo,
            sum(cast(contratos as bigint)) as contratos,
            sum(cast(valor_total as double)) as valor_total
        FROM df_api
        GROUP BY 1, 2, 3, 4
    """).fetchdf()
    
    # Métricas Proveedores (Fase 2)
    prov_df = con.execute("""
        SELECT 
            proveedor,
            COUNT(DISTINCT entidad) as entidades_distintas,
            SUM(contratos) as contratos_totales,
            SUM(valor_total) as valor_total,
            SUM(CASE WHEN modalidad LIKE '%DIRECTA%' THEN contratos ELSE 0 END) as contratos_directos,
            SUM(CASE WHEN modalidad LIKE '%LICITACI%' THEN contratos ELSE 0 END) as contratos_licitacion
        FROM edges_df
        GROUP BY 1
    """).fetchdf()
    
    # Calcular porcentajes y riesgo (Fase 4)
    prov_df['pct_directa'] = prov_df['contratos_directos'] / prov_df['contratos_totales']
    prov_df['pct_licitacion'] = prov_df['contratos_licitacion'] / prov_df['contratos_totales']
    
    # riesgo = log(contratos_totales) * porcentaje_contratacion_directa * log(valor_total + 1)
    prov_df['score_riesgo'] = (
        np.log1p(prov_df['contratos_totales']) * 
        prov_df['pct_directa'] * 
        np.log1p(prov_df['valor_total'])
    ).round(2)
    
    # Nivel de Riesgo (Fase 3)
    def asignar_nivel(row):
        if row['pct_directa'] >= 0.70 and row['entidades_distintas'] > 1 and row['contratos_totales'] >= 5:
            return '🔴 ALTO'
        elif row['pct_licitacion'] >= 0.50 and row['contratos_totales'] < 5:
            return '🟢 BAJO'
        else:
            return '🟡 MEDIO'
            
    prov_df['nivel_riesgo'] = prov_df.apply(asignar_nivel, axis=1)
    prov_df.sort_values('score_riesgo', ascending=False, inplace=True)
    
    # Métricas Entidades (Fase 2)
    ent_df = con.execute("""
        SELECT 
            entidad,
            COUNT(DISTINCT proveedor) as num_proveedores,
            SUM(contratos) as contratos_totales,
            SUM(valor_total) as valor_total,
            SUM(valor_total) / NULLIF(SUM(contratos), 0) as valor_promedio
        FROM edges_df
        GROUP BY 1
    """).fetchdf()
    
    con.close()
    return edges_df, prov_df, ent_df

@st.cache_resource(show_spinner=False)
def build_base_graph(edges_df, prov_df, ent_df):
    """Construye y cachea la red completa una sola vez por municipio"""
    G = nx.Graph()
    
    prov_dict = prov_df.set_index('proveedor').to_dict('index')
    ent_dict = ent_df.set_index('entidad').to_dict('index')
    
    max_val = edges_df['valor_total'].max() if not edges_df.empty else 1
    if max_val == 0: max_val = 1
    
    for _, row in edges_df.iterrows():
        p = row["proveedor"]
        e = row["entidad"]
        mod = row["modalidad"]
            
        # Atributos de nodo: Proveedor
        if not G.has_node(p):
            p_data = prov_dict.get(p, {})
            # Colores según riesgo
            nivel = p_data.get('nivel_riesgo', '🟡 MEDIO')
            if '🔴' in nivel: color_p = C['red']
            elif '🟢' in nivel: color_p = C['green']
            else: color_p = C['amber']
            
            size_p = 10 + min(p_data.get('contratos_totales', 1) * 2, 50)
            
            G.add_node(p, tipo="proveedor", label=p, color=color_p, size=size_p,
                       title=f"PROVEEDOR<br><b>{p}</b><br>Riesgo: {nivel} (Score: {p_data.get('score_riesgo',0)})<br>Contratos: {p_data.get('contratos_totales',0)}<br>Entidades: {p_data.get('entidades_distintas',0)}<br>Directa: {p_data.get('pct_directa',0)*100:.1f}%")
            
        # Atributos de nodo: Entidad
        if not G.has_node(e):
            e_data = ent_dict.get(e, {})
            color_e = C['blue'] # Entidades en azul estable
            size_e = 15 + min(e_data.get('contratos_totales', 1) * 2, 60)
            
            G.add_node(e, tipo="entidad", label=e, color=color_e, shape="square", size=size_e,
                       title=f"ENTIDAD<br><b>{e}</b><br>Contratos: {e_data.get('contratos_totales',0)}<br>Proveedores: {e_data.get('num_proveedores',0)}<br>Total: {format_b(e_data.get('valor_total',0))}")
            
        # Atributos de Arista
        # Color por modalidad
        if "DIRECTA" in mod: color_edge = C['red']
        elif "LICITACI" in mod: color_edge = C['blue']
        else: color_edge = C['gray']
        
        # Grosor por valor_total
        width = max(1, min((row['valor_total'] / max_val) * 15, 15))
        
        # Tooltip Fase 6
        t_title = f"{p} ↔ {e}<br>Modalidad: {mod}<br>Tipo: {row['tipo']}<br>Contratos: {row['contratos']}<br>Valor: {format_b(row['valor_total'])}"
        
        # NetworkX soporta multi-edges pero Pyvis lo maneja sumando o superponiendo. Usaremos sum si ya existe.
        if G.has_edge(p, e):
            G[p][e]['width'] += width/2 # engrosar
            G[p][e]['title'] += f"<hr>{t_title}" # concatenar tooltip
        else:
            G.add_edge(p, e, weight=row['valor_total'], width=width, color=color_edge, title=t_title)
            
    return G

def build_graph(edges_df, prov_df, ent_df, ego_node=None):
    """FASE 5: Grafo Mejorado (Instantáneo por Caché)"""
    G_base = build_base_graph(edges_df, prov_df, ent_df)
    
    if ego_node and G_base.has_node(ego_node):
        return nx.ego_graph(G_base, ego_node, radius=1)
    
    return G_base

def generate_alerts(prov_df, edges_df):
    """FASE 8: Alertas Automáticas"""
    alertas = []
    
    # Regla 1: Fragmentación Alta (Entidad)
    # Ya manejado en parte en proveedores, busquemos proveedores anómalos
    for _, p in prov_df.head(100).iterrows():
        if p['pct_directa'] > 0.85 and p['contratos_totales'] >= 4:
            alertas.append({
                "Nivel": "🔴 RIESGO CRÍTICO", "Actor": p['proveedor'], "Tipo": "Concentración Directa",
                "Mensaje": f"Proveedor con {p['pct_directa']*100:.1f}% de contratación directa y score {p['score_riesgo']}."
            })
        if p['entidades_distintas'] >= 5:
            alertas.append({
                "Nivel": "🟡 ALERTA ESTRUCTURAL", "Actor": p['proveedor'], "Tipo": "Red Transversal",
                "Mensaje": f"Opera con {p['entidades_distintas']} entidades distintas en el territorio."
            })
            
    # Regla 2: Repetición Sospechosa
    # Agrupar edges para ver repetición pura entre p y e
    rep = edges_df.groupby(['proveedor', 'entidad'])['contratos'].sum().reset_index()
    for _, r in rep[rep['contratos'] >= 10].iterrows():
        alertas.append({
            "Nivel": "🔴 ALTO RIESGO", "Actor": f"{r['proveedor']} ↔ {r['entidad']}", "Tipo": "Fragmentación/Carrusel",
            "Mensaje": f"Relación altamente repetitiva con {r['contratos']} contratos directos."
        })
        
    return pd.DataFrame(alertas) if alertas else pd.DataFrame(columns=["Nivel", "Actor", "Tipo", "Mensaje"])

def create_pyvis_html(G):
    """Genera HTML de Pyvis con soporte a fallbacks."""
    try:
        net = Network(height="650px", width="100%", bgcolor=C['bg'], font_color=C['text'], select_menu=True, cdn_resources='remote')
    except:
        net = Network(height="650px", width="100%", bgcolor=C['bg'], font_color=C['text'], select_menu=True)
        
    net.force_atlas_2based(gravity=-60, central_gravity=0.01, spring_length=120, spring_strength=0.05, damping=0.5)
    
    for n, d in G.nodes(data=True):
        net.add_node(n, label=d.get('label', n[:20]), title=d.get('title',''), color=d.get('color', C['blue']), shape=d.get('shape', 'dot'), size=d.get('size', 10))
        
    for u, v, d in G.edges(data=True):
        net.add_edge(u, v, title=d.get('title',''), color=d.get('color', C['gray']), width=d.get('width', 1))
        
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch", "network_temp.html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    net.save_graph(path)
    
    with open(path, 'r', errors='ignore') as f:
        html = f.read()
        
    if "lib/bindings" in html:
        html = html.replace('src="lib/bindings/utils.js"', '')
        html = html.replace('href="lib/bindings/vis-network.min.css"', 'href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/dist/vis-network.min.css"')
        html = html.replace('src="lib/bindings/vis-network.min.js"', 'src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/vis-network.min.js"')
        
    return html

def render_network_tab(anio: int, dep_raw: str, mun_raw: str):
    st.markdown("""
    <div style='margin-bottom:15px;'>
        <h2 style='font-size:1.4rem;font-weight:900;color:#fff;margin:0;'>🕸️ Detección Estructural de Riesgos y Grafos Enriquecidos</h2>
        <p style='color:#64748B;font-size:.85rem;margin:4px 0 0;'>Sistema experto en grafos que detecta fragmentación, carruseles de contratación directa y transversalidad estructural.</p>
    </div>
    """, unsafe_allow_html=True)
    
    df_raw = get_network_raw_data(anio, dep_raw, mun_raw)
    
    if df_raw.empty:
        st.warning("No hay suficientes datos contractuales para construir una red.")
        return
        
    edges_df, prov_df, ent_df = process_metrics_and_risk(df_raw)
    
    # ── FASE 7: INTERACCIÓN TABLA → GRAFO ─────────────────────────────
    # Sincronización con estado global de KPIs
    def update_actor():
        val = st.session_state.get("select_actor_raw", "🌍 Mostrar Red Completa")
        st.session_state["actor_seleccionado"] = "" if val == "🌍 Mostrar Red Completa" else val

    opciones_nodos = ["🌍 Mostrar Red Completa"] + sorted(list(prov_df['proveedor']) + list(ent_df['entidad']))
    
    # Pre-seleccionar si el estado global tiene algo
    idx = 0
    actor_actual = st.session_state.get("actor_seleccionado", "")
    if actor_actual in opciones_nodos:
        idx = opciones_nodos.index(actor_actual)
        
    nodo_seleccionado = st.selectbox(
        "🔎 Explorar Sub-Grafo por Actor (Entidad o Proveedor):", 
        opciones_nodos, 
        index=idx,
        key="select_actor_raw", 
        on_change=update_actor
    )
    
    ego_filter = None if nodo_seleccionado == "🌍 Mostrar Red Completa" else nodo_seleccionado
    
    # Construir Grafo en Memoria (0.01 seg)
    with st.spinner("Renderizando red geométrica..."):
        G = build_graph(edges_df, prov_df, ent_df, ego_node=ego_filter)
    
    if G is None or len(G.nodes) == 0:
        st.warning("El grafo resultante está vacío para este filtro.")
        return
        
    # Leyenda Visual
    st.markdown(f"""
    <div style='display:flex; gap: 15px; font-size:.7rem; margin-bottom: 10px; flex-wrap:wrap;'>
        <div style='background:{C['card']}; padding:5px 10px; border-radius:5px; border:1px solid {C['border']};'>
            <b>Nodos (Riesgo Proveedor):</b> 
            <span style='color:{C['red']};'>🔴 Alto</span> | 
            <span style='color:{C['amber']};'>🟡 Medio</span> | 
            <span style='color:{C['green']};'>🟢 Bajo</span>
        </div>
        <div style='background:{C['card']}; padding:5px 10px; border-radius:5px; border:1px solid {C['border']};'>
            <b>Aristas (Modalidad):</b> 
            <span style='color:{C['red']};'>🔴 Directa</span> | 
            <span style='color:{C['blue']};'>🔵 Licitación</span> | 
            <span style='color:{C['gray']};'>⚪ Otras</span>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    html_graph = create_pyvis_html(G)
    st.markdown(f"<div style='border:1px solid {C['border']};border-radius:12px;overflow:hidden;'>", unsafe_allow_html=True)
    components.html(html_graph, height=650, scrolling=False)
    st.markdown("</div><br>", unsafe_allow_html=True)
    
    # Tablas Inferiores (Fases 8 y 9)
    col_a, col_b = st.columns([1, 1.5])
    
    with col_a:
        st.markdown(f"<div style='font-size:.7rem;font-weight:700;color:{C['red']};letter-spacing:1px;margin-bottom:10px;'>🚨 ALERTAS AUTOMÁTICAS</div>", unsafe_allow_html=True)
        df_alertas = generate_alerts(prov_df, edges_df)
        if not df_alertas.empty:
            st.dataframe(df_alertas, use_container_width=True, hide_index=True)
        else:
            st.success("No se detectaron riesgos severos.")
            
    with col_b:
        st.markdown(f"<div style='font-size:.7rem;font-weight:700;color:{C['purple']};letter-spacing:1px;margin-bottom:10px;'>📊 TABLA FINAL DE RIESGO (FASE 9)</div>", unsafe_allow_html=True)
        # Preparar tabla final Fase 9
        t_final = prov_df[['proveedor', 'entidades_distintas', 'contratos_totales', 'valor_total', 'pct_directa', 'score_riesgo']].copy()
        t_final['pct_directa'] = (t_final['pct_directa'] * 100).round(1).astype(str) + "%"
        t_final.rename(columns={
            "proveedor": "Proveedor", "entidades_distintas": "Entidades Distintas",
            "contratos_totales": "Contratos", "valor_total": "Valor Total",
            "pct_directa": "% Contratación Directa", "score_riesgo": "Score Riesgo"
        }, inplace=True)
        st.dataframe(t_final, use_container_width=True, hide_index=True)
