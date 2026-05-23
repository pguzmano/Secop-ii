"""
network_analysis.py — SECOP II · Motor Forense de Grafos
=========================================================
Equipo Interdisciplinario:
  🔎 Analista Forense     → Tipologías de corrupción, reglas de alerta roja
  🧮 Científico de Datos  → Centralidad de grafos (betweenness, degree), score forense
  ⚙️ Ingeniero de Datos   → SoQL optimizado, DuckDB, 1000 filas efectivas
  🖥️ Frontend UX          → PyVis con resplandores, tentáculos, panel de riesgo

MISIÓN: Detectar focos de corrupción — proveedores con tentáculos en múltiples
entidades, carruseles de contratación directa y redes de favoritismo estructural.
"""

import os
import requests
import pandas as pd
import duckdb
import numpy as np
import networkx as nx
from pyvis.network import Network
import streamlit as st
import streamlit.components.v1 as components
import time
from functools import wraps
import math

API_RESOURCE = "jbjy-vk9h"
API_BASE     = f"https://www.datos.gov.co/resource/{API_RESOURCE}.json"
API_TIMEOUT  = 30

C = dict(
    bg="#060B14", card="#0D1421", border="#1A2336",
    blue="#4F8EF7", green="#22C55E", amber="#F59E0B", red="#F43F5E",
    purple="#A78BFA", text="#F1F5F9", muted="#64748B",
    gray="#9CA3AF", orange="#FB923C"
)

# ─────────────────────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────────────────────
def format_b(v):
    try:
        v = float(v)
        if pd.isna(v) or v == float('inf') or v == float('-inf'): return "—"
        if v >= 1e12: return f"${v/1e12:.2f} B"
        if v >= 1e9:  return f"${v/1e9:.1f} MM"
        if v >= 1e6:  return f"${v/1e6:.1f} M"
        return f"${v:,.0f}"
    except: return "—"

def monitor_latency(func_name: str):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            t0 = time.time()
            result = func(*args, **kwargs)
            elapsed = time.time() - t0
            if elapsed > 0.5:
                print(f"[LATENCY] {func_name}: {elapsed:.3f}s")
            return result
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# FASE 1 — INGENIERO DE DATOS: Consulta SoQL PRE-AGREGADA (rápida)
# La API hace el GROUP BY en el servidor → nos llegan <200 filas, no 1000
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)  # 5 minutos — más cache
@monitor_latency("get_network_raw_data")
def get_network_raw_data(anio: int, dep_raw: str = "", mun_raw: str = "") -> pd.DataFrame:
    """Consulta SoQL con GROUP BY en servidor — retorna ~100-200 aristas agregadas, no 1000 filas crudas.
    
    OPTIMIZACIÓN CLAVE: Hacer la agregación en Socrata (servidor) en lugar de
    traer 1000 filas y agregar en Python. Esto reduce trfico de red 10x.
    """
    print(f"[get_network_raw_data] anio={anio}, dep='{dep_raw}', mun='{mun_raw}'")

    conds = [f"date_extract_y(fecha_de_firma) = {anio}"]
    conds.append("proveedor_adjudicado IS NOT NULL")
    conds.append("nombre_entidad IS NOT NULL")
    conds.append("valor_del_contrato IS NOT NULL")

    if dep_raw:
        safe_dep = dep_raw.upper().replace("'", "''")
        conds.append(f"upper(departamento) = '{safe_dep}'")
    if mun_raw:
        safe_mun = mun_raw.upper().replace("'", "''")
        conds.append(f"upper(ciudad) = '{safe_mun}'")

    # ⬇️ SoQL con GROUP BY en el servidor → llegan ~150 filas en vez de 1000
    params = {
        "$select": (
            "upper(trim(proveedor_adjudicado)) AS proveedor_adjudicado, "
            "upper(trim(nombre_entidad)) AS nombre_entidad, "
            "upper(trim(modalidad_de_contratacion)) AS modalidad_de_contratacion, "
            "upper(trim(tipo_de_contrato)) AS tipo_de_contrato, "
            "SUM(valor_del_contrato) AS valor_total, "
            "COUNT(*) AS contratos"
        ),
        "$where":  " AND ".join(conds),
        "$group":  "upper(trim(proveedor_adjudicado)), upper(trim(nombre_entidad)), upper(trim(modalidad_de_contratacion)), upper(trim(tipo_de_contrato))",
        "$order":  "SUM(valor_del_contrato) DESC",
        "$limit":  "2000"   # 2000 aristas agregadas, no filas crudas
    }

    try:
        r = requests.get(API_BASE, params=params, timeout=API_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data)
        df['valor_total'] = pd.to_numeric(df['valor_total'], errors='coerce').fillna(0)
        df['contratos']   = pd.to_numeric(df['contratos'],   errors='coerce').fillna(0).astype(int)

        # Renombrar columnas para compatibilidad con el pipeline
        df = df.rename(columns={
            'proveedor_adjudicado':       'proveedor_adjudicado',
            'nombre_entidad':             'nombre_entidad',
            'modalidad_de_contratacion':  'modalidad_de_contratacion',
            'tipo_de_contrato':           'tipo_de_contrato',
        })

        print(f"[get_network_raw_data] {len(df)} aristas pre-agregadas desde API")
        return df

    except requests.Timeout:
        st.error("⏱️ Timeout al consultar la red. Intenta con un municipio más pequeño.")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"Error en red: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# FASE 2 — CIENTÍFICO DE DATOS: Métricas, Riesgo y Centralidad de Grafos
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
@monitor_latency("process_metrics_and_risk")
def process_metrics_and_risk(df_raw: pd.DataFrame):
    """
    Calcula:
      - Score de riesgo (log × pct_directa × log_valor)
      - Nivel de riesgo (ALTO / MEDIO / BAJO)
      - Centralidad de intermediación (betweenness) — identifica "puentes"
      - Centralidad de grado (degree) — mide tentáculos
    """
    if df_raw.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    con = duckdb.connect()
    con.register("df_api", df_raw)

    # Aristas consolidadas (proveedor ↔ entidad)
    edges_df = con.execute("""
        SELECT
            proveedor_adjudicado AS proveedor,
            nombre_entidad       AS entidad,
            modalidad_de_contratacion AS modalidad,
            tipo_de_contrato     AS tipo,
            SUM(valor_total)     AS valor_total,
            SUM(contratos)       AS contratos
        FROM df_api
        GROUP BY 1, 2, 3, 4
    """).fetchdf()

    # Métricas por Proveedor
    prov_df = con.execute("""
        SELECT
            proveedor,
            COUNT(DISTINCT entidad)                                              AS entidades_distintas,
            SUM(contratos)                                                       AS contratos_totales,
            SUM(valor_total)                                                     AS valor_total,
            SUM(CASE WHEN modalidad LIKE '%DIRECTA%' THEN contratos ELSE 0 END) AS contratos_directos,
            SUM(CASE WHEN modalidad LIKE '%LICITACI%' THEN contratos ELSE 0 END) AS contratos_licitacion
        FROM edges_df
        GROUP BY 1
    """).fetchdf()

    # Métricas por Entidad
    ent_df = con.execute("""
        SELECT
            entidad,
            COUNT(DISTINCT proveedor)  AS num_proveedores,
            SUM(contratos)             AS contratos_totales,
            SUM(valor_total)           AS valor_total
        FROM edges_df
        GROUP BY 1
    """).fetchdf()

    con.close()

    # ── Score de Riesgo Forense ────────────────────────────────────────────
    prov_df['pct_directa']   = prov_df['contratos_directos']   / prov_df['contratos_totales'].clip(lower=1)
    prov_df['pct_licitacion'] = prov_df['contratos_licitacion'] / prov_df['contratos_totales'].clip(lower=1)

    prov_df['score_riesgo'] = (
        np.log1p(prov_df['contratos_totales']) *
        prov_df['pct_directa'] *
        np.log1p(prov_df['valor_total'].clip(lower=0))
    ).round(2)

    # ── Nivel de Riesgo (vectorizado) ─────────────────────────────────────
    cond_alto = (prov_df['pct_directa'] >= 0.70) & (prov_df['entidades_distintas'] >= 2) & (prov_df['contratos_totales'] >= 3)
    cond_bajo = (prov_df['pct_licitacion'] >= 0.50) & (prov_df['contratos_totales'] < 5)
    prov_df['nivel_riesgo'] = np.select(
        [cond_alto, cond_bajo],
        ['🔴 ALTO', '🟢 BAJO'],
        default='🟡 MEDIO'
    )
    prov_df.sort_values('score_riesgo', ascending=False, inplace=True)

    # ── Centralidad de Grafos — OPTIMIZADO ──────────────────────────────────
    # REEMPLAZA betweenness_centrality (O(VE), lentísimo) por:
    #   - degree_centrality: O(V+E), instantáneo
    #   - entidades_distintas como proxy de "puente" (igual de informativo)
    # Esto reduce de ~20s a <0.1s sin perder valor forense.
    G_temp = nx.Graph()
    for _, row in edges_df.iterrows():
        G_temp.add_edge(row['proveedor'], row['entidad'], weight=float(row['valor_total']))

    # Degree Centrality → cuántos vecinos directos tiene (tentáculos)
    degree_c = nx.degree_centrality(G_temp)

    # Betweenness Centrality → cuánto "puente" es entre otros nodos
    # Solo calcular si el grafo no es gigantesco (evita OOM)
    n_nodes = len(G_temp.nodes)
    if n_nodes <= 300:
        between_c = nx.betweenness_centrality(G_temp, normalized=True, weight='weight')
    else:
        # Aproximación rápida con k muestras
        between_c = nx.betweenness_centrality(G_temp, k=min(100, n_nodes), normalized=True)

    # Agregar centralidades a prov_df
    prov_df['centralidad_grado']        = prov_df['proveedor'].map(degree_c).fillna(0).round(4)
    prov_df['centralidad_intermediacion'] = prov_df['proveedor'].map(between_c).fillna(0).round(4)

    # Score Forense Final = score_riesgo × (1 + centralidad_intermediacion × 10)
    prov_df['score_forense'] = (
        prov_df['score_riesgo'] * (1 + prov_df['centralidad_intermediacion'] * 10)
    ).round(2)
    prov_df.sort_values('score_forense', ascending=False, inplace=True)

    # Agregar centralidades a ent_df también
    ent_df['centralidad_grado'] = ent_df['entidad'].map(degree_c).fillna(0).round(4)

    return edges_df, prov_df, ent_df


# ─────────────────────────────────────────────────────────────────────────────
# FASE 3A — INGENIERÍA: Construcción del Grafo NetworkX
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=120, show_spinner=False)
def build_base_graph(edges_df, prov_df, ent_df, _cache_key: tuple = ()):
    """Construye grafo NetworkX base con atributos forenses (tamaño ∝ valor)."""
    print(f"[build_base_graph] {len(edges_df)} aristas | cache_key={_cache_key}")

    G = nx.Graph()
    prov_dict = prov_df.set_index('proveedor').to_dict('index')
    ent_dict  = ent_df.set_index('entidad').to_dict('index')

    max_val  = max(float(edges_df['valor_total'].max()), 1)
    max_pval = max(float(prov_df['valor_total'].max()), 1)
    max_eval = max(float(ent_df['valor_total'].max()), 1)

    # ── Nodos Proveedor ────────────────────────────────────────────────────
    for p in edges_df['proveedor'].unique():
        p_data  = prov_dict.get(p, {})
        nivel   = p_data.get('nivel_riesgo', '🟡 MEDIO')
        score_f = p_data.get('score_forense', 0)
        bet     = p_data.get('centralidad_intermediacion', 0)
        ent_cnt = p_data.get('entidades_distintas', 1)
        val     = p_data.get('valor_total', 0)
        contratos = p_data.get('contratos_totales', 1)
        pct_d   = p_data.get('pct_directa', 0)

        # Tamaño basado en valor total del proveedor (forense: dinero = peligro)
        size_p = 12 + min((val / max_pval) * 55, 55)

        if '🔴' in nivel:
            color_p = C['red']
        elif '🟢' in nivel:
            color_p = C['green']
        else:
            color_p = C['amber']

        # Nodo ancla de riesgo máximo si centralidad alta
        is_critical = (bet > 0.1) or (score_f > 5 and ent_cnt >= 3)

        G.add_node(
            p,
            tipo="proveedor",
            label=p[:25],
            color=color_p,
            size=float(size_p),
            borderWidth=3 if is_critical else 1,
            borderWidthSelected=5,
            shadow=is_critical,  # Marcamos para aplicar glow rojo después
            title=(
                f"🔎 PROVEEDOR<br><b>{p}</b><br>"
                f"━━━━━━━━━━━━━━━━━━━━<br>"
                f"⚠️ Riesgo: {nivel} (Score Forense: {score_f:.1f})<br>"
                f"💰 Valor Total: {format_b(val)}<br>"
                f"📋 Contratos: {contratos}<br>"
                f"🏛️ Entidades distintas: {ent_cnt}<br>"
                f"🎯 Contratación Directa: {pct_d*100:.1f}%<br>"
                f"🕸️ Centralidad (Puente): {bet:.3f}<br>"
                f"━━━━━━━━━━━━━━━━━━━━<br>"
                f"<i>Haz clic para ver sub-grafo</i>"
            )
        )

    # ── Nodos Entidad ──────────────────────────────────────────────────────
    for e in edges_df['entidad'].unique():
        e_data    = ent_dict.get(e, {})
        val_e     = e_data.get('valor_total', 0)
        num_prov  = e_data.get('num_proveedores', 0)
        contratos_e = e_data.get('contratos_totales', 0)
        c_grado   = e_data.get('centralidad_grado', 0)

        size_e = 15 + min((val_e / max_eval) * 50, 50)

        G.add_node(
            e,
            tipo="entidad",
            label=e[:25],
            color=C['blue'],
            shape="square",
            size=float(size_e),
            borderWidth=2,
            title=(
                f"🏛️ ENTIDAD PÚBLICA<br><b>{e}</b><br>"
                f"━━━━━━━━━━━━━━━━━━━━<br>"
                f"💰 Presupuesto: {format_b(val_e)}<br>"
                f"📋 Contratos: {contratos_e}<br>"
                f"🤝 Proveedores distintos: {num_prov}<br>"
                f"🕸️ Conectividad: {c_grado:.3f}"
            )
        )

    # ── Aristas ────────────────────────────────────────────────────────────
    for row in edges_df.itertuples(index=False):
        p   = row.proveedor
        e   = row.entidad
        mod = row.modalidad

        if "DIRECTA" in mod:
            color_edge = C['red']
        elif "LICITACI" in mod:
            color_edge = C['blue']
        elif "CONCUR" in mod or "MÉRITO" in mod or "MERITO" in mod:
            color_edge = C['purple']
        elif "MENOR" in mod:
            color_edge = C['orange']
        else:
            color_edge = C['gray']

        width = max(1.5, min((float(row.valor_total) / max_val) * 18, 18))
        t_title = (
            f"{p} ↔ {e}<br>"
            f"Modalidad: {mod}<br>"
            f"Tipo: {row.tipo}<br>"
            f"Contratos: {int(row.contratos)}<br>"
            f"Valor: {format_b(row.valor_total)}"
        )

        if G.has_edge(p, e):
            G[p][e]['width'] += width / 2
            G[p][e]['title'] += f"<hr>{t_title}"
        else:
            G.add_edge(p, e, weight=float(row.valor_total), width=width,
                       color=color_edge, title=t_title)

    return G


def build_graph(edges_df, prov_df, ent_df, ego_node=None, cache_key: tuple = ()):
    """Grafo filtrado por ego_node (sub-grafo de tentáculos) o grafo completo."""
    try:
        G_base = build_base_graph(edges_df, prov_df, ent_df, _cache_key=cache_key)

        if ego_node and G_base.has_node(ego_node):
            # Radio 1 = proveedor + sus entidades directas
            return nx.ego_graph(G_base, ego_node, radius=1)

        return G_base
    except Exception as e:
        st.error(f"❌ Error construyendo grafo: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# FASE 2 — FORENSE: Generación de Alertas Rojas
# ─────────────────────────────────────────────────────────────────────────────
def generate_alerts(prov_df: pd.DataFrame, edges_df: pd.DataFrame) -> pd.DataFrame:
    """
    Analista Forense: Aplica tipologías de corrupción pública:
      - Monopolio Directo: >85% contratación directa en ≥4 contratos
      - Red Transversal:   proveedor opera en ≥4 entidades distintas
      - Favoritismo:       ≥8 contratos con la misma entidad
      - Fraccionamiento:   muchos contratos pequeños que suman mucho
      - Puente Estructural: alta centralidad de intermediación (broker de red)
    """
    try:
        alertas = []

        for _, p in prov_df.head(150).iterrows():
            score_f  = p.get('score_forense', 0)
            bet      = p.get('centralidad_intermediacion', 0)
            pct_d    = p.get('pct_directa', 0)
            ent_cnt  = p.get('entidades_distintas', 1)
            contratos = p.get('contratos_totales', 0)

            # 🔴 Monopolio Directo
            if pct_d >= 0.85 and contratos >= 4:
                alertas.append({
                    "Nivel": "🔴 CRÍTICO", "Actor": p['proveedor'],
                    "Tipología": "Monopolio Directo",
                    "Detalle": f"{pct_d*100:.0f}% directa en {contratos} contratos. Valor: {format_b(p['valor_total'])}",
                    "Score": round(score_f, 1)
                })

            # 🔴 Puente Estructural — broker que conecta múltiples entidades
            if bet >= 0.15 and ent_cnt >= 3:
                alertas.append({
                    "Nivel": "🔴 CRÍTICO", "Actor": p['proveedor'],
                    "Tipología": "Puente Estructural",
                    "Detalle": f"Centralidad de intermediación {bet:.3f} — opera como broker entre {ent_cnt} entidades.",
                    "Score": round(score_f, 1)
                })

            # 🟠 Red Transversal
            if ent_cnt >= 4 and pct_d >= 0.60:
                alertas.append({
                    "Nivel": "🟠 ALTO", "Actor": p['proveedor'],
                    "Tipología": "Red Transversal",
                    "Detalle": f"Opera simultáneamente en {ent_cnt} entidades con {pct_d*100:.0f}% contratación directa.",
                    "Score": round(score_f, 1)
                })

        # Favoritismo y Fraccionamiento (por par proveedor-entidad)
        rep = edges_df.groupby(['proveedor', 'entidad']).agg(
            contratos=('contratos', 'sum'),
            valor_total=('valor_total', 'sum')
        ).reset_index()

        for _, r in rep[rep['contratos'] >= 7].iterrows():
            alertas.append({
                "Nivel": "🔴 CRÍTICO", "Actor": f"{r['proveedor']}",
                "Tipología": "Favoritismo Extremo",
                "Detalle": f"{int(r['contratos'])} contratos repetidos con {r['entidad']}. Total: {format_b(r['valor_total'])}",
                "Score": round(float(r['contratos']) * float(r['valor_total']) / 1e9, 1)
            })

        for _, r in rep[(rep['contratos'] >= 5) & (rep['valor_total'] > 5e7)].iterrows():
            avg_val = float(r['valor_total']) / max(float(r['contratos']), 1)
            if avg_val < 3e7:
                alertas.append({
                    "Nivel": "🟠 ALTO", "Actor": f"{r['proveedor']}",
                    "Tipología": "Posible Fraccionamiento",
                    "Detalle": f"{int(r['contratos'])} contratos pequeños con {r['entidad']}. Suma: {format_b(r['valor_total'])} (avg {format_b(avg_val)})",
                    "Score": round(float(r['valor_total']) / 1e8, 1)
                })

        if not alertas:
            return pd.DataFrame(columns=["Nivel", "Actor", "Tipología", "Detalle", "Score"])

        df_al = pd.DataFrame(alertas).drop_duplicates(subset=["Actor", "Tipología"])
        df_al = df_al.sort_values("Score", ascending=False).reset_index(drop=True)
        return df_al

    except Exception as e:
        st.error(f"❌ Error en alertas: {e}")
        return pd.DataFrame(columns=["Nivel", "Actor", "Tipología", "Detalle", "Score"])


# ─────────────────────────────────────────────────────────────────────────────
# FASE 3B — FRONTEND: PyVis con Resplandores y Efectos Forenses
# ─────────────────────────────────────────────────────────────────────────────
@monitor_latency("create_pyvis_html")
def create_pyvis_html(G, ego_node=None, critical_nodes: set = None):
    """
    Genera HTML PyVis con:
      - Resplandor rojo (#F43F5E) para nodos críticos
      - Tamaño proporcional al valor económico
      - Modo radial para ego-grafo (tentáculos de un actor)
    """
    if critical_nodes is None:
        critical_nodes = set()

    try:
        net = Network(height="600px", width="100%", bgcolor=C['bg'],
                      font_color=C['text'], select_menu=False, cdn_resources='remote')
    except Exception:
        net = Network(height="600px", width="100%", bgcolor=C['bg'],
                      font_color=C['text'], select_menu=False)

    if ego_node and G.has_node(ego_node):
        net.repulsion(node_distance=250, central_gravity=0.04,
                      spring_length=300, spring_strength=0.04, damping=0.09)
    else:
        net.repulsion(node_distance=200, central_gravity=0.015,
                      spring_length=250, spring_strength=0.03, damping=0.09)

    # Limitar a 120 nodos más relevantes (por tamaño = valor)
    max_nodes = 120
    nodes_all  = list(G.nodes())

    if len(nodes_all) > max_nodes:
        scored = sorted(nodes_all, key=lambda n: G.nodes[n].get('size', 10), reverse=True)
        nodes_vis = set(scored[:max_nodes])
    else:
        nodes_vis = set(nodes_all)

    for n in nodes_vis:
        d = G.nodes[n]
        is_ego      = (ego_node is not None and n == ego_node)
        is_critical = (n in critical_nodes)

        color = d.get('color', C['blue'])
        size  = d.get('size', 12)
        shape = d.get('shape', 'dot')
        label = d.get('label', str(n)[:20])
        title = d.get('title', '')

        # Resalte visual para nodo central del ego-grafo
        if is_ego:
            color = "#F43F5E" if shape == 'dot' else "#10B981"
            size  = max(size, 40)

        # Configuración de resplandor (shadow) para nodos críticos
        if is_critical or is_ego:
            shadow_color = "#F43F5E"
            net.add_node(
                n, label=label, title=title,
                color={"background": color, "border": "#F43F5E",
                       "highlight": {"background": "#FF6B6B", "border": "#FF0000"}},
                shape=shape, size=size,
                shadow={"enabled": True, "color": shadow_color, "size": 20, "x": 0, "y": 0},
                borderWidth=3,
                **({"fixed": True, "x": 0, "y": 0} if is_ego else {})
            )
        else:
            net.add_node(n, label=label, title=title,
                         color=color, shape=shape, size=size)

    # Aristas (solo entre nodos visibles)
    for u, v, d in G.edges(data=True):
        if u in nodes_vis and v in nodes_vis:
            net.add_edge(u, v,
                         title=d.get('title', ''),
                         color=d.get('color', C['gray']),
                         width=d.get('width', 1))

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch", "network_temp.html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    net.save_graph(path)

    try:
        with open(path, 'r', errors='ignore') as f:
            html = f.read()
    except Exception as e:
        return f"<div>Error HTML: {e}</div>"

    # Inyectar CSS personalizado forense
    custom_css = f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap');
    body {{
        font-family: 'Inter', sans-serif !important;
        background: {C['bg']} !important;
        margin: 0; padding: 0;
        touch-action: pan-y;
    }}
    #mynetwork {{
        background: radial-gradient(ellipse at center, #0D1421 0%, {C['bg']} 100%) !important;
        border: none !important;
    }}
    div.vis-tooltip {{
        background: rgba(13,20,33,0.97) !important;
        border: 1px solid {C['border']} !important;
        border-radius: 10px !important;
        padding: 10px 14px !important;
        font-size: 0.78rem !important;
        color: {C['text']} !important;
        max-width: 280px !important;
        box-shadow: 0 4px 20px rgba(0,0,0,0.6) !important;
        line-height: 1.6 !important;
        pointer-events: none;
        z-index: 1000 !important;
    }}
    div.vis-tooltip b {{ color: {C['blue']} !important; font-weight: 800 !important; }}
    div.vis-tooltip hr {{ border-color: rgba(255,255,255,0.1) !important; margin: 6px 0 !important; }}
    </style>
    """
    html = html.replace('</head>', f'{custom_css}</head>')

    if "lib/bindings" in html:
        html = html.replace('src="lib/bindings/utils.js"', '')
        html = html.replace('href="lib/bindings/vis-network.min.css"',
                            'href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/dist/vis-network.min.css"')
        html = html.replace('src="lib/bindings/vis-network.min.js"',
                            'src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/vis-network.min.js"')

    return html


# ─────────────────────────────────────────────────────────────────────────────
# FASE 3C — FRONTEND: Panel "Tentáculos del Actor" (al seleccionar proveedor)
# ─────────────────────────────────────────────────────────────────────────────
def render_tentaculos(actor: str, edges_df: pd.DataFrame, prov_df: pd.DataFrame):
    """Muestra todas las entidades donde opera el proveedor seleccionado."""
    es_proveedor = actor in prov_df['proveedor'].values
    if not es_proveedor:
        return

    df_actor = edges_df[edges_df['proveedor'] == actor].copy()
    if df_actor.empty:
        return

    prov_info = prov_df[prov_df['proveedor'] == actor].iloc[0]
    nivel = prov_info.get('nivel_riesgo', '🟡 MEDIO')
    score = prov_info.get('score_forense', 0)
    bet   = prov_info.get('centralidad_intermediacion', 0)
    pct_d = prov_info.get('pct_directa', 0)

    # Resumen por entidad
    resumen = df_actor.groupby('entidad').agg(
        contratos=('contratos', 'sum'),
        valor_total=('valor_total', 'sum'),
        modalidades=('modalidad', lambda x: ', '.join(set(x)))
    ).reset_index().sort_values('valor_total', ascending=False)

    nivel_color = C['red'] if '🔴' in nivel else (C['green'] if '🟢' in nivel else C['amber'])

    st.markdown(f"""
    <div style='background:rgba(244,63,94,.08);border:1px solid rgba(244,63,94,.3);
                border-radius:12px;padding:16px;margin-bottom:16px;'>
        <div style='font-size:.65rem;font-weight:700;letter-spacing:1px;
                    color:{C['red']};margin-bottom:6px;'>🔎 ANÁLISIS FORENSE DE ACTOR</div>
        <div style='font-size:1rem;font-weight:900;color:#fff;margin-bottom:10px;'>{actor[:60]}</div>
        <div style='display:flex;gap:12px;flex-wrap:wrap;font-size:.75rem;'>
            <span style='background:rgba(255,255,255,.05);padding:3px 10px;border-radius:6px;'>
                ⚠️ Nivel: <b style='color:{nivel_color};'>{nivel}</b>
            </span>
            <span style='background:rgba(255,255,255,.05);padding:3px 10px;border-radius:6px;'>
                🎯 Score Forense: <b style='color:{C['amber']};'>{score:.1f}</b>
            </span>
            <span style='background:rgba(255,255,255,.05);padding:3px 10px;border-radius:6px;'>
                🕸️ Centralidad Puente: <b style='color:{C['purple']};'>{bet:.3f}</b>
            </span>
            <span style='background:rgba(255,255,255,.05);padding:3px 10px;border-radius:6px;'>
                📋 Directa: <b style='color:{C["red"] if pct_d > 0.7 else C["amber"]};'>{pct_d*100:.0f}%</b>
            </span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown(f"<div style='font-size:.8rem;font-weight:700;color:{C['muted']};margin-bottom:8px;'>"
                f"🏛️ ENTIDADES DONDE OPERA ({len(resumen)})</div>", unsafe_allow_html=True)

    max_val = float(resumen['valor_total'].max() or 1)
    for i, row in enumerate(resumen.itertuples(), 1):
        pct_bar = int(float(row.valor_total) / max_val * 100)
        mod_icons = "🔴" if "DIRECTA" in str(row.modalidades) else "🔵"
        st.markdown(f"""
        <div style='display:flex;align-items:center;gap:8px;padding:6px 0;
                    border-bottom:1px solid {C['border']};font-size:.75rem;'>
            <div style='color:{C['muted']};width:20px;text-align:right;'>{i}</div>
            <div style='flex:1;color:{C['text']};overflow:hidden;text-overflow:ellipsis;
                        white-space:nowrap;' title='{row.entidad}'>{row.entidad[:50]}</div>
            <div style='width:60px;height:4px;background:{C['border']};border-radius:2px;flex-shrink:0;'>
                <div style='width:{pct_bar}%;height:4px;border-radius:2px;
                            background:linear-gradient(90deg,{C['red']},{C['amber']});'></div>
            </div>
            <div style='color:{C['blue']};font-weight:700;flex-shrink:0;width:70px;text-align:right;'>{format_b(row.valor_total)}</div>
            <div style='color:{C['muted']};flex-shrink:0;'>{mod_icons} {int(row.contratos)}c</div>
        </div>
        """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# FASE 3D — FRONTEND: Panel Top 10 Contratistas de Riesgo (siempre visible)
# ─────────────────────────────────────────────────────────────────────────────
def render_top_risk_panel(prov_df: pd.DataFrame):
    """Panel fijo con el TOP 10 contratistas de mayor score forense."""
    top10 = prov_df.head(10)
    if top10.empty:
        return

    st.markdown(f"""
    <div style='background:linear-gradient(135deg,rgba(244,63,94,.08),rgba(167,139,250,.08));
                border:1px solid rgba(244,63,94,.25);border-radius:14px;
                padding:16px 18px;margin-bottom:16px;'>
        <div style='display:flex;align-items:center;gap:10px;margin-bottom:12px;'>
            <div style='font-size:1.3rem;'>🚨</div>
            <div>
                <div style='font-size:.65rem;font-weight:700;letter-spacing:1px;color:{C['red']};'>
                    RADAR DE CORRUPCIÓN — EQUIPO FORENSE SECOP II
                </div>
                <div style='font-size:.95rem;font-weight:900;color:#fff;'>
                    TOP 10 Contratistas de Mayor Riesgo
                </div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    max_score = float(top10['score_forense'].max() or 1)

    for i, row in enumerate(top10.itertuples(), 1):
        nivel = row.nivel_riesgo
        score = float(row.score_forense)
        pct_d = float(row.pct_directa)
        ent_c = int(row.entidades_distintas)
        val   = float(row.valor_total)
        bet   = float(row.centralidad_intermediacion)
        pct_bar = int(score / max_score * 100)

        if '🔴' in nivel:
            badge_bg   = 'rgba(244,63,94,.15)'
            badge_col  = C['red']
            badge_bord = 'rgba(244,63,94,.3)'
        elif '🟢' in nivel:
            badge_bg   = 'rgba(34,197,94,.1)'
            badge_col  = C['green']
            badge_bord = 'rgba(34,197,94,.2)'
        else:
            badge_bg   = 'rgba(245,158,11,.1)'
            badge_col  = C['amber']
            badge_bord = 'rgba(245,158,11,.2)'

        # Construir string condicional sin crear líneas en blanco
        puente_html = f" · 🕸️ Puente {bet:.2f}" if bet > 0.05 else ""
        
        # Escribir HTML sin indentación para evitar que Markdown lo tome como bloque de código
        html_str = f"""<div style='display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid {C['border']};'>
<div style='font-size:.75rem;font-weight:900;color:{badge_col};width:22px;'>#{i}</div>
<div style='flex:1;min-width:0;'>
<div style='font-size:.78rem;color:#fff;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' title='{row.proveedor}'>{row.proveedor[:45]}</div>
<div style='font-size:.68rem;color:{C['muted']};margin-top:2px;'>{ent_c} entidades · {format_b(val)} · Directa {pct_d*100:.0f}%{puente_html}</div>
<div style='width:{pct_bar}%;height:3px;background:linear-gradient(90deg,{badge_col},{C['purple']});border-radius:2px;margin-top:4px;'></div>
</div>
<div style='background:{badge_bg};border:1px solid {badge_bord};color:{badge_col};border-radius:6px;padding:2px 8px;font-size:.68rem;font-weight:700;flex-shrink:0;'>{score:.1f}</div>
</div>"""
        st.markdown(html_str, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# ORQUESTADOR PRINCIPAL: render_network_tab
# ─────────────────────────────────────────────────────────────────────────────
def render_network_tab(anio: int, dep_raw: str, mun_raw: str):
    """Renderiza la pestaña de Análisis de Redes & Anomalías — Escuadrón Forense."""

    print(f"[render_network_tab] anio={anio}, dep_raw='{dep_raw}', mun_raw='{mun_raw}'")

    st.markdown("""
    <div style='margin-bottom:15px;'>
        <h2 style='font-size:1.4rem;font-weight:900;color:#fff;margin:0;'>
            🕸️ Radar Forense de Contratación — Detección de Corrupción
        </h2>
        <p style='color:#64748B;font-size:.82rem;margin:4px 0 0;'>
            Motor de grafos con centralidad forense, detección de carruseles y análisis de tentáculos.
            Los nodos más grandes = más dinero. Rojo = alerta crítica.
        </p>
    </div>
    """, unsafe_allow_html=True)

    # Filtros activos
    filtros = []
    if dep_raw: filtros.append(f"📍 {dep_raw}")
    if mun_raw: filtros.append(f"🏙️ {mun_raw}")
    if filtros:
        st.markdown(f"""
        <div style='background:rgba(79,142,247,.1);border:1px solid rgba(79,142,247,.25);
                    border-radius:8px;padding:10px;margin-bottom:12px;font-size:.8rem;'>
            <b>🔍 Filtrando:</b> {' | '.join(filtros)}
        </div>
        """, unsafe_allow_html=True)

    # ── FASE 1: Datos ──────────────────────────────────────────────────────
    with st.spinner("📡 Consultando contratos..."):
        df_raw = get_network_raw_data(anio, dep_raw, mun_raw)

    if df_raw.empty:
        st.warning("⚠️ No hay suficientes datos contractuales para construir la red forense.")
        return

    # ── FASE 2: Métricas y Centralidad ────────────────────────────────────
    with st.spinner("🧮 Calculando centralidad y scores forenses..."):
        try:
            edges_df, prov_df, ent_df = process_metrics_and_risk(df_raw)
        except Exception as e:
            st.error(f"❌ Error en procesamiento: {e}")
            return

    if edges_df.empty:
        st.warning("⚠️ Datos insuficientes para construir la red.")
        return

    # Nodos críticos (para resplandor rojo)
    critical_nodes = set(
        prov_df[
            (prov_df['nivel_riesgo'] == '🔴 ALTO') |
            (prov_df['centralidad_intermediacion'] >= 0.1)
        ]['proveedor'].tolist()
    )

    # ── FASE 3: Panel Top 10 (siempre visible) ────────────────────────────
    render_top_risk_panel(prov_df)

    # ── Selector de Actor ─────────────────────────────────────────────────
    def update_actor():
        val = st.session_state.get("select_actor_raw", "🌍 Mostrar Red Completa")
        st.session_state["actor_seleccionado"] = "" if val == "🌍 Mostrar Red Completa" else val

    max_opts = 60
    proveedores_list = list(prov_df['proveedor'].head(max_opts))
    entidades_list   = list(ent_df['entidad'].head(max_opts))
    opciones_nodos   = ["🌍 Mostrar Red Completa"] + sorted(proveedores_list + entidades_list)

    actor_actual = st.session_state.get("actor_seleccionado", "")
    idx = opciones_nodos.index(actor_actual) if actor_actual in opciones_nodos else 0

    st.markdown(f"""
    <div style='background:{C['card']};border:1px solid {C['border']};border-radius:10px;
                padding:10px 14px;margin-bottom:8px;'>
        <div style='font-size:.65rem;font-weight:700;color:{C['muted']};letter-spacing:1px;margin-bottom:4px;'>
            🔎 FILTRAR POR ACTOR → VER TENTÁCULOS Y ACTUALIZA KPIs
        </div>
    """, unsafe_allow_html=True)

    nodo_seleccionado = st.selectbox(
        "Actor",
        opciones_nodos,
        index=idx,
        key="select_actor_raw",
        on_change=update_actor,
        label_visibility="collapsed"
    )
    st.markdown("</div>", unsafe_allow_html=True)

    ego_filter = None if nodo_seleccionado == "🌍 Mostrar Red Completa" else nodo_seleccionado

    # ── Panel de Tentáculos (solo si se selecciona proveedor) ─────────────
    if ego_filter:
        render_tentaculos(ego_filter, edges_df, prov_df)

    # ── Construcción del Grafo ────────────────────────────────────────────
    cache_key = (anio, dep_raw, mun_raw)
    G = build_graph(edges_df, prov_df, ent_df, ego_node=ego_filter, cache_key=cache_key)

    if G is None or len(G.nodes) == 0:
        st.warning("El grafo está vacío para este filtro.")
        return

    # ── Leyenda Visual ────────────────────────────────────────────────────
    st.markdown(f"""
    <div style='display:flex;gap:10px;font-size:.7rem;margin-bottom:10px;flex-wrap:wrap;'>
        <div style='background:{C['card']};padding:5px 10px;border-radius:5px;border:1px solid {C['border']};'>
            <b>Nodos Proveedor:</b>
            <span style='color:{C['red']};'>● Alto</span> |
            <span style='color:{C['amber']};'>● Medio</span> |
            <span style='color:{C['green']};'>● Bajo</span>
        </div>
        <div style='background:{C['card']};padding:5px 10px;border-radius:5px;border:1px solid {C['border']};'>
            <b>Entidades:</b> <span style='color:{C['blue']};'>■ Azul</span>
        </div>
        <div style='background:{C['card']};padding:5px 10px;border-radius:5px;border:1px solid {C['border']};'>
            <b>Aristas:</b>
            <span style='color:{C['red']};'>— Directa</span> |
            <span style='color:{C['blue']};'>— Licitación</span> |
            <span style='color:{C['purple']};'>— Concurso</span> |
            <span style='color:{C['orange']};'>— Menor Cuantía</span>
        </div>
        <div style='background:rgba(244,63,94,.1);padding:5px 10px;border-radius:5px;border:1px solid rgba(244,63,94,.3);'>
            🔴 Resplandor = Nodo Crítico (Score Alto)
        </div>
        <div style='background:{C['card']};padding:5px 10px;border-radius:5px;border:1px solid {C['border']};'>
            Tamaño = 💰 Valor económico
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Visualización PyVis ───────────────────────────────────────────────
    with st.spinner("🗺️ Renderizando grafo forense..."):
        ego_actual   = ego_filter
        html_graph   = create_pyvis_html(G, ego_node=ego_actual, critical_nodes=critical_nodes)

    st.markdown(f"<div style='border:1px solid {C['border']};border-radius:12px;overflow:hidden;'>",
                unsafe_allow_html=True)
    components.html(html_graph, height=600, scrolling=False)
    st.markdown("</div><br>", unsafe_allow_html=True)

    # ── Tabla Maestra Forense + Expediente Dinámico ───────────────────────
    render_forensic_master_table(prov_df, anio, dep_raw, mun_raw, edges_df=edges_df)


# =============================================================================
# MODULO FORENSE: Tabla Maestra + Expediente Contractual Dinamico
# Equipo: Analista Forense / Cientifico de Datos / Ingeniero de Datos / UX
# =============================================================================
def soql_focal(params: dict) -> pd.DataFrame:
    """Consulta puntual al API Socrata — para el expediente dinamico.
    Independiente de app.py para evitar importaciones circulares."""
    for attempt in range(3):
        try:
            r = requests.get(
                API_BASE, params=params, timeout=30,
                headers={"Accept-Encoding": "gzip", "User-Agent": "SECOP-Forense/2.0"}
            )
            if r.status_code == 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            return pd.DataFrame(data) if data else pd.DataFrame()
        except requests.Timeout:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return pd.DataFrame()
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return pd.DataFrame()

def soql_get_procesos(params: dict) -> pd.DataFrame:
    """Consulta al dataset de Procesos de Contratación (p6dx-8zbt)."""
    API_PROCESOS = "https://www.datos.gov.co/resource/p6dx-8zbt.json"
    for attempt in range(3):
        try:
            r = requests.get(
                API_PROCESOS, params=params, timeout=30,
                headers={"Accept-Encoding": "gzip", "User-Agent": "SECOP-Forense-Procesos/2.0"}
            )
            if r.status_code == 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            return pd.DataFrame(data) if data else pd.DataFrame()
        except requests.Timeout:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return pd.DataFrame()
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return pd.DataFrame()



def render_forensic_master_table(prov_df: pd.DataFrame, anio: int,
                                  dep_raw: str, mun_raw: str, edges_df: pd.DataFrame = None):
    """Tabla Maestra Unificada + Expediente Dinamico:
       Rep. Legal -> Malla Corporativa -> Grafo focalizado -> Auditoria financiera."""

    TEMP_DIR = os.path.dirname(os.path.abspath(__file__))

    st.markdown("<h3 style='color:#fff;'>🕵️ Radar Forense y Red de Vinculación Coincidentes</h3>", unsafe_allow_html=True)

    # Forzar inicialización limpia de variables de estado de sesión
    if "forensic_prov_selected" not in st.session_state:
        st.session_state["forensic_prov_selected"] = None
    if "forensic_malla_data" not in st.session_state:
        st.session_state["forensic_malla_data"] = None
    if "forensic_contratos_data" not in st.session_state:
        st.session_state["forensic_contratos_data"] = None
    if "forensic_metadata" not in st.session_state:
        st.session_state["forensic_metadata"] = {}

    if prov_df.empty:
        st.info("No hay datos de contratistas disponibles en la capa filtrada.")
    else:
        # 1. MAPEO ELÁSTICO DE COLUMNAS ORIGINALES (Evita KeyError de Pandas)
        cols_actuales = prov_df.columns.tolist()
        
        col_prov = "proveedor_adjudicado" if "proveedor_adjudicado" in cols_actuales else ("proveedor" if "proveedor" in cols_actuales else cols_actuales[0])
        col_cnt = "contratos_totales" if "contratos_totales" in cols_actuales else ("contratos" if "contratos" in cols_actuales else "contratos")
        col_val = "valor_total" if "valor_total" in cols_actuales else ("valor" if "valor" in cols_actuales else "valor")
        col_pct = "pct_directa" if "pct_directa" in cols_actuales else ("directa_pct" if "directa_pct" in cols_actuales else "pct_directa")
        col_score = "score_forense" if "score_forense" in cols_actuales else ("score" if "score" in cols_actuales else "score_forense")

        # Construcción de la Tabla Maestra Visual
        df_maestra = prov_df[[col_prov, col_cnt, col_val, col_pct, col_score]].copy()
        df_maestra.columns = ["Contratista / Proveedor", "Contratos", "Monto Total", "% Contr. Directa", "Score Forense"]
        df_maestra = df_maestra.sort_values(by="Score Forense", ascending=False)

        st.markdown("<div style='font-size:0.82rem; color:#64748B; margin-bottom:8px;'>👇 Selecciona un contratista de la tabla para recalcular las métricas y actualizar el mapa de relaciones:</div>", unsafe_allow_html=True)

        # Tabla maestra interactiva con retorno de evento para re-run controlado
        evento_seleccion = st.dataframe(
            df_maestra.style.background_gradient(subset=["Score Forense"], cmap="Reds"),
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="tabla_maestra_forense"
        )

        # 2. CAPA DE PROCESAMIENTO FORENSE INDEXADA POR NIT
        if evento_seleccion and evento_seleccion.selection and evento_seleccion.selection.rows:
            idx_tabla = evento_seleccion.selection.rows[0]
            
            # Recuperamos la fila original completa de prov_df usando el índice de posición absoluta
            fila_origen = prov_df.iloc[idx_tabla]
            proveedor_actual = fila_origen[col_prov]
            
            # Evaluamos cambio de selección para no repetir llamadas asíncronas
            if st.session_state["forensic_prov_selected"] != proveedor_actual:
                st.session_state["forensic_prov_selected"] = proveedor_actual
                
                # Intentamos extraer el NIT del proveedor directamente de los datos locales precalculados
                nit_prov_central = fila_origen.get("nit_proveedor", "N/A")
                
                # Si el NIT no viene en prov_df, hacemos una única consulta rápida a Socrata usando el índice exacto
                if pd.isna(nit_prov_central) or nit_prov_central == "N/A":
                    with st.spinner("Indexando identificadores únicos (NIT) en Socrata..."):
                        safe_prov_raw = str(proveedor_actual).replace("'", "''")
                        df_ids = soql_focal({
                            "$select": "documento_proveedor AS nit_proveedor, nombre_representante_legal, identificaci_n_representante_legal",
                            "$where": f"upper(proveedor_adjudicado) = '{safe_prov_raw.upper()}'",
                            "$limit": "1"
                        })
                        
                    if not df_ids.empty:
                        nit_prov_central = df_ids.iloc[0].get("nit_proveedor", "N/A")
                        rep_legal_nom = str(df_ids.iloc[0].get("nombre_representante_legal", "NO REGISTRADO")).upper()
                        nit_rep_central = df_ids.iloc[0].get("identificaci_n_representante_legal", "N/A")
                    else:
                        nit_prov_central = "N/A"
                        rep_legal_nom = "NO REGISTRADO"
                        nit_rep_central = "N/A"
                else:
                    # Extraemos el resto de metadatos de la fila local si existen, o les damos un fallback por defecto
                    rep_legal_nom = str(fila_origen.get("nombre_representante_legal", fila_origen.get("representante_legal", "NO REGISTRADO"))).upper()
                    nit_rep_central = fila_origen.get("identificaci_n_representante_legal", fila_origen.get("identificacion_representante_legal", fila_origen.get("nit_representante_legal", "N/A")))

                # Normalización de seguridad para NIT del Representante Legal
                nit_rep_str = str(nit_rep_central).strip().upper()
                invalid_reps = ["", "N/A", "NAN", "NONE", "SIN DESCRIPCION", "SIN DESCRIPCIÓN", "0", "000000", "000000000", "NO DEFINIDO", "NO REPORTA", "NO APLICA"]
                if pd.isna(nit_rep_central) or nit_rep_str in invalid_reps:
                    nit_rep_central = "N/A"

                # Almacenamos la estructura en la sesión de forma segura
                st.session_state["forensic_metadata"] = {
                    "nit_proveedor": nit_prov_central,
                    "rep_legal": rep_legal_nom,
                    "nit_rep": nit_rep_central
                }


                if nit_prov_central != "N/A":
                    # Si hay NIT de representante barremos la malla corporativa completa; si no, aislamos por el NIT de la empresa
                    if nit_rep_central != "N/A":
                        where_malla = f"identificaci_n_representante_legal = '{nit_rep_central}'"
                        where_contratos = f"identificaci_n_representante_legal = '{nit_rep_central}'"
                    else:
                        where_malla = f"documento_proveedor = '{nit_prov_central}'"
                        where_contratos = f"documento_proveedor = '{nit_prov_central}'"
                else:
                    safe_prov_raw = str(proveedor_actual).replace("'", "''").upper()
                    where_malla = f"upper(trim(proveedor_adjudicado)) = '{safe_prov_raw}'"
                    where_contratos = f"upper(trim(proveedor_adjudicado)) = '{safe_prov_raw}'"

                # 🌍 Aplicar filtro geográfico si existe para focalizar el expediente
                if dep_raw:
                    where_malla += f" AND upper(departamento) = '{dep_raw.upper().replace(chr(39), chr(39)+chr(39))}'"
                    where_contratos += f" AND upper(departamento) = '{dep_raw.upper().replace(chr(39), chr(39)+chr(39))}'"
                if mun_raw:
                    where_malla += f" AND upper(ciudad) = '{mun_raw.upper().replace(chr(39), chr(39)+chr(39))}'"
                    where_contratos += f" AND upper(ciudad) = '{mun_raw.upper().replace(chr(39), chr(39)+chr(39))}'"

                with st.spinner("Construyendo matriz relacional y buscando transacciones..."):
                    # Consulta B: Malla relacional
                    df_malla_api = soql_focal({
                        "$select": "proveedor_adjudicado, documento_proveedor AS nit_proveedor, MAX(nombre_representante_legal) AS rep_legal_nombre, nombre_entidad, nit_entidad, SUM(valor_del_contrato) as sum_valor, COUNT(*) as cant_contratos",
                        "$where": f"{where_malla} AND date_extract_y(fecha_de_firma) = {anio}",
                        "$group": "proveedor_adjudicado, documento_proveedor, nombre_entidad, nit_entidad",
                        "$limit": "250"
                    })
                    
                    # Consulta C: Historial financiero detallado
                    df_contratos_api = soql_focal({
                        "$select": "nombre_entidad, valor_del_contrato, fecha_de_firma, fecha_de_inicio_del_contrato AS fecha_de_inicio, fecha_de_fin_del_contrato AS fecha_fin, estado_contrato AS estado_del_contrato, tipo_de_contrato, modalidad_de_contratacion, valor_de_pago_adelantado AS valor_pago_adelantado, valor_amortizado, valor_pagado, nombre_supervisor",
                        "$where": f"date_extract_y(fecha_de_firma) = {anio} AND {where_contratos}",
                        "$order": "valor_del_contrato DESC",
                        "$limit": "100"
                    })

                if df_malla_api is not None and not df_malla_api.empty:
                    st.session_state["forensic_malla_data"] = df_malla_api
                    st.session_state["forensic_contratos_data"] = df_contratos_api
                else:
                    # FALLBACK SUPREMO: Si falla Socrata, extraemos la matriz de relaciones de la memoria local (edges_df)
                    with st.spinner("Construyendo matriz desde registros indexados localmente..."):
                        if edges_df is not None and not edges_df.empty:
                            # Filtrar relaciones de este proveedor desde edges_df
                            df_malla_local = edges_df[edges_df["proveedor"] == proveedor_actual].copy()
                            
                            # Mapear columnas para simular respuesta de Socrata (Malla)
                            df_malla_local["nit_proveedor"] = "N/A"
                            df_malla_local["nit_entidad"] = df_malla_local["entidad"]
                            df_malla_local["proveedor_adjudicado"] = df_malla_local["proveedor"]
                            df_malla_local["nombre_entidad"] = df_malla_local["entidad"]
                            df_malla_local["sum_valor"] = df_malla_local["valor_total"]
                            df_malla_local["cant_contratos"] = df_malla_local["contratos"]
                            
                            st.session_state["forensic_malla_data"] = df_malla_local
                            
                            # Mapear columnas para simular respuesta de Socrata (Contratos/Matriz)
                            df_contratos_local = df_malla_local.copy()
                            df_contratos_local["valor_del_contrato"] = df_malla_local["valor_total"]
                            df_contratos_local["estado_del_contrato"] = "Agregado Local"
                            df_contratos_local["fecha_de_firma"] = "Varios Contratos"
                            df_contratos_local["fecha_de_inicio"] = ""
                            df_contratos_local["fecha_fin"] = ""
                            df_contratos_local["valor_pago_adelantado"] = 0
                            df_contratos_local["valor_amortizado"] = 0
                            df_contratos_local["valor_pagado"] = 0
                            df_contratos_local["nombre_supervisor"] = "No reportado" 
                            
                            st.session_state["forensic_contratos_data"] = df_contratos_local
                        else:
                            st.session_state["forensic_malla_data"] = None
                            st.session_state["forensic_contratos_data"] = None

        # 3. CAPA DE RENDERIZADO VISUAL ESTABLE (Pinta leyendo la memoria caché local de la sesión)
        if st.session_state["forensic_prov_selected"] is not None:
            prov_activo = st.session_state["forensic_prov_selected"]
            metadata = st.session_state["forensic_metadata"]
            df_malla_total = st.session_state["forensic_malla_data"]
            df_contratos = st.session_state["forensic_contratos_data"]
            
            st.markdown("<hr style='border-top: 1px dashed #1A2336; margin:20px 0;'>", unsafe_allow_html=True)
            st.markdown(f"<h4>📋 Expediente de Auditoría: <span style='color:#4F8EF7;'>{prov_activo}</span></h4>", unsafe_allow_html=True)

            if df_malla_total is not None and not df_malla_total.empty:
                if "nit_proveedor" not in df_malla_total.columns: df_malla_total["nit_proveedor"] = df_malla_total.get("proveedor_adjudicado", "N/A")
                if "nit_entidad" not in df_malla_total.columns: df_malla_total["nit_entidad"] = df_malla_total.get("nombre_entidad", "N/A")
                
                df_malla_total["sum_valor"] = pd.to_numeric(df_malla_total["sum_valor"], errors="coerce").fillna(0)
                df_malla_total["cant_contratos"] = pd.to_numeric(df_malla_total["cant_contratos"], errors="coerce").fillna(0)
                
                # Cálculo de los KPIs de la Red
                total_empresas_malla = df_malla_total["nit_proveedor"].nunique()
                total_entidades_afectadas = df_malla_total["nit_entidad"].nunique()
                monto_global_red = df_malla_total["sum_valor"].sum()
                
                col_kpi1, col_kpi2, col_kpi3, col_kpi4 = st.columns(4)
                with col_kpi1:
                    color_alert = "#F43F5E" if total_empresas_malla > 1 else "#22C55E"
                    st.markdown(f"""
                    <div style='background:#0D1421; border:1px solid #1A2336; border-radius:8px; padding:12px; text-align:center;'>
                        <div style='font-size:0.65rem; color:#64748B; font-weight:700; text-transform:uppercase;'>Razones Sociales Relacionadas</div>
                        <div style='font-size:1.6rem; font-weight:900; color:{color_alert};'>{total_empresas_malla}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col_kpi2:
                    st.markdown(f"""
                    <div style='background:#0D1421; border:1px solid #1A2336; border-radius:8px; padding:12px; text-align:center;'>
                        <div style='font-size:0.65rem; color:#64748B; font-weight:700; text-transform:uppercase;'>Entidades Públicas Compradoras</div>
                        <div style='font-size:1.6rem; font-weight:900; color:#4F8EF7;'>{total_entidades_afectadas}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col_kpi3:
                    st.markdown(f"""
                    <div style='background:#0D1421; border:1px solid #1A2336; border-radius:8px; padding:12px; text-align:center;'>
                        <div style='font-size:0.65rem; color:#64748B; font-weight:700; text-transform:uppercase;'>Presupuesto Comprometido en Red</div>
                        <div style='font-size:1.2rem; font-weight:900; color:#F59E0B; margin-top:6px;'>{format_b(monto_global_red)}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col_kpi4:
                    rep_nombre = metadata.get("rep_legal", "NO REGISTRADO")
                    rep_doc = metadata.get("nit_rep", "N/A")
                    st.markdown(f"""
                    <div style='background:#0D1421; border:1px solid #1A2336; border-radius:8px; padding:12px; text-align:center;'>
                        <div style='font-size:0.65rem; color:#64748B; font-weight:700; text-transform:uppercase;'>Representante Legal</div>
                        <div style='font-size:0.9rem; font-weight:900; color:#E2E8F0; margin-top:4px;' title='{rep_nombre}'>{rep_nombre[:25] + '...' if len(rep_nombre)>25 else rep_nombre}</div>
                        <div style='font-size:0.75rem; font-weight:700; color:#4F8EF7; margin-top:2px;'>ID: {rep_doc}</div>
                    </div>
                    """, unsafe_allow_html=True)

                # Construcción del Grafo Interactivo con PyVis
                st.markdown("<br><div style='font-size:0.75rem; font-weight:700; color:#fff; text-transform:uppercase;'>🕸️ Grafo de Coincidencia de Estructura de Control e Impacto Económico</div>", unsafe_allow_html=True)
                
                net = Network(height="450px", width="100%", bgcolor="#0D1421", font_color="#F1F5F9")
                net.repulsion(node_distance=180, central_gravity=0.05, spring_length=220, spring_strength=0.05, damping=0.09)
                
                id_raiz = metadata["nit_rep"] if metadata["nit_rep"] != "N/A" else metadata["nit_proveedor"]
                lbl_origen = f"REP: {metadata['rep_legal'][:15]}..." if metadata["nit_rep"] != "N/A" else "CONTRATISTA"
                net.add_node(id_raiz, label=lbl_origen, title=lbl_origen, color="#F43F5E", size=24, shape="diamond")
                
                for _, fila in df_malla_total.iterrows():
                    grosor_arista = max(1, math.log(fila["sum_valor"] + 1) / 3.5) if fila["sum_valor"] > 0 else 1
                    
                    nom_proveedor = str(fila.get("proveedor_adjudicado", "Desconocido"))
                    if nom_proveedor.isdigit() and "rep_legal_nombre" in fila and pd.notna(fila["rep_legal_nombre"]):
                        nom_proveedor = str(fila["rep_legal_nombre"]).upper()
                        
                    label_prov = (nom_proveedor[:16] + "...") if len(nom_proveedor) > 16 else nom_proveedor
                    
                    net.add_node(fila["nit_proveedor"], label=label_prov, title=nom_proveedor, color="#4F8EF7", size=18, shape="dot")
                    # Arista Representante -> Empresa
                    net.add_edge(id_raiz, fila["nit_proveedor"], color="#F43F5E", width=2, dash=True, title="Vínculo Societario / Representación Legal", arrows="to")
                    
                    net.add_node(fila["nit_entidad"], label=fila["nombre_entidad"][:16]+"...", title=fila["nombre_entidad"], color="#64748B", size=14, shape="triangle")
                    # Arista Empresa -> Entidad
                    edge_title = f"Contratos: {fila['cant_contratos']} | Total: {format_b(fila['sum_valor'])}"
                    net.add_edge(fila["nit_proveedor"], fila["nit_entidad"], color="#1A2336", width=grosor_arista, title=edge_title, arrows="to")
                
                path_html = os.path.join(TEMP_DIR, "temp_interactive_network.html")
                net.save_graph(path_html)
                with open(path_html, 'r', encoding='utf-8') as f:
                    components.html(f.read(), height=390, scrolling=False)

            # Renderizado de la Tabla Detallada de Auditoría Financiera
            st.markdown("<br><div style='font-size:0.75rem; font-weight:700; color:#fff; text-transform:uppercase; margin-bottom:6px;'>💸 Historial Detallado de Procesos y Estados Financieros</div>", unsafe_allow_html=True)
            if df_contratos is not None and not df_contratos.empty:
                if "nombre_entidad" not in df_contratos.columns: df_contratos["nombre_entidad"] = "Entidad Desconocida"
                if "valor_del_contrato" not in df_contratos.columns: df_contratos["valor_del_contrato"] = 0
                if "estado_del_contrato" not in df_contratos.columns: df_contratos["estado_del_contrato"] = "Desconocido"
                if "fecha_de_firma" not in df_contratos.columns: df_contratos["fecha_de_firma"] = "No Especificada"
                if "valor_pago_adelantado" not in df_contratos.columns: df_contratos["valor_pago_adelantado"] = 0
                if "valor_amortizado" not in df_contratos.columns: df_contratos["valor_amortizado"] = 0
                if "valor_pagado" not in df_contratos.columns: df_contratos["valor_pagado"] = 0
                if "fecha_de_inicio" not in df_contratos.columns: df_contratos["fecha_de_inicio"] = ""
                if "fecha_fin" not in df_contratos.columns: df_contratos["fecha_fin"] = ""
                if "tipo_de_contrato" not in df_contratos.columns: df_contratos["tipo_de_contrato"] = "No Especificado"
                if "nombre_supervisor" not in df_contratos.columns: df_contratos["nombre_supervisor"] = "Sin Supervisor"

                df_contratos["valor_del_contrato"] = pd.to_numeric(df_contratos["valor_del_contrato"], errors="coerce").fillna(0)
                df_contratos["valor_pago_adelantado"] = pd.to_numeric(df_contratos["valor_pago_adelantado"], errors="coerce").fillna(0)
                df_contratos["valor_amortizado"] = pd.to_numeric(df_contratos["valor_amortizado"], errors="coerce").fillna(0)
                df_contratos["valor_pagado"] = pd.to_numeric(df_contratos["valor_pagado"], errors="coerce").fillna(0)

                for col_f in ["fecha_de_firma", "fecha_de_inicio", "fecha_fin"]:
                    if col_f in df_contratos.columns:
                        df_contratos[col_f] = df_contratos[col_f].astype(str).str.replace("T00:00:00.000", "", regex=False)

                mod_col = df_contratos["modalidad_de_contratacion"] if "modalidad_de_contratacion" in df_contratos.columns else df_contratos["tipo_de_contrato"]
                df_detallado_disp = pd.DataFrame({
                    "Entidad Contratante": df_contratos["nombre_entidad"],
                    "Modalidad / Tipo": mod_col.fillna("No Especificado"),
                    "Estado": df_contratos["estado_del_contrato"].fillna("Desconocido"),
                    "Cuantía": df_contratos["valor_del_contrato"].apply(format_b),
                    "Firma": df_contratos["fecha_de_firma"],
                    "Inicio": df_contratos["fecha_de_inicio"],
                    "Fin": df_contratos["fecha_fin"],
                    "Anticipo": df_contratos["valor_pago_adelantado"].apply(format_b),
                    "Amortizado": df_contratos["valor_amortizado"].apply(format_b),
                    "Pagado": df_contratos["valor_pagado"].apply(format_b),
                    "Supervisor": df_contratos["nombre_supervisor"].fillna("Sin Supervisor").str.title()
                })
                st.dataframe(df_detallado_disp, use_container_width=True, hide_index=True, key="tabla_auditoria_financiera")
            else:
                st.info("No se reportaron transacciones financieras sueltas para la auditoría de este contratista.")

            # ── INTEGRACIÓN VISUAL EN EL EXPEDIENTE DINÁMICO DEL CONTRATISTA (PROCESOS p6dx-8zbt) ──
            nit_target = metadata.get("nit_proveedor")
            
            if nit_target and nit_target != "N/A":
                cond_prov_procesos = f"nit_del_proveedor_adjudicado = '{nit_target}'"
            else:
                safe_prov_raw = str(prov_activo).replace("'", "''").upper()
                cond_prov_procesos = f"upper(nombre_del_proveedor) = '{safe_prov_raw}'"

            st.markdown("<br><hr style='border-top: 1px dashed #1A2336; margin:20px 0;'>", unsafe_allow_html=True)
            st.markdown("<h4>📊 Auditoría Forense de Competencia y Ofertas</h4>", unsafe_allow_html=True)
            
            with st.spinner("Consultando nivel de competencia en licitaciones..."):
                df_ofertas_proceso = soql_get_procesos({
                    "$select": "entidad, descripci_n_del_procedimiento, referencia_del_proceso, conteo_de_respuestas_a_ofertas, modalidad_de_contratacion, precio_base",
                    "$where": f"{cond_prov_procesos}",
                    "$order": "conteo_de_respuestas_a_ofertas ASC",
                    "$limit": "100"
                })
            
            if df_ofertas_proceso.empty:
                st.info("No se registran métricas de competencia indexadas para este proveedor en el dataset de Procesos.")
            else:
                    df_ofertas_proceso["conteo_de_respuestas_a_ofertas"] = pd.to_numeric(df_ofertas_proceso["conteo_de_respuestas_a_ofertas"], errors="coerce").fillna(0)
                    df_ofertas_proceso["precio_base"] = pd.to_numeric(df_ofertas_proceso["precio_base"], errors="coerce").fillna(0)
                    
                    # Filtramos procesos sin competencia (Ofertas == 1 o Ofertas == 0, típico en Contratación Directa)
                    procesos_monopolio = df_ofertas_proceso[df_ofertas_proceso["conteo_de_respuestas_a_ofertas"] <= 1]
                    pct_proponente_unico = (len(procesos_monopolio) / len(df_ofertas_proceso)) * 100
                    
                    presupuesto_sin_competencia = procesos_monopolio["precio_base"].sum()
                    
                    col_m1, col_m2, col_m3 = st.columns(3)
                    with col_m1:
                        st.metric(
                            label="Promedio de Ofertas en Adjudicaciones", 
                            value=f"{df_ofertas_proceso['conteo_de_respuestas_a_ofertas'].mean():.1f} Ofertas"
                        )
                    with col_m2:
                        color_risk = "inverse" if pct_proponente_unico > 50 else "normal"
                        st.metric(
                            label="Índice Proponente Único (Ofertas = 1)", 
                            value=f"{pct_proponente_unico:.1f}%",
                            delta="Riesgo de Pliego Sastre" if pct_proponente_unico > 50 else "Normal",
                            delta_color=color_risk
                        )
                    with col_m3:
                        st.metric(
                            label="Adjudicado Sin Competencia", 
                            value=format_b(presupuesto_sin_competencia),
                            delta="Foco Veeduría" if presupuesto_sin_competencia > 0 else "Competitivo",
                            delta_color="inverse" if presupuesto_sin_competencia > 0 else "normal"
                        )
                        
                    st.markdown("<div style='font-size:0.75rem; font-weight:700; color:#fff; text-transform:uppercase;'>📋 Análisis de Concurrencia por Proceso Convocado</div>", unsafe_allow_html=True)
                    
                    df_view_ofertas = pd.DataFrame({
                        "Entidad Compradora": df_ofertas_proceso["entidad"].str.title(),
                        "Referencia Proceso": df_ofertas_proceso["referencia_del_proceso"],
                        "Modalidad": df_ofertas_proceso["modalidad_de_contratacion"],
                        "Precio Base": df_ofertas_proceso["precio_base"].apply(format_b),
                        "Ofertas Presentadas": df_ofertas_proceso["conteo_de_respuestas_a_ofertas"].astype(int)
                    })
                    
                    st.dataframe(
                        df_view_ofertas.style.map(
                            lambda x: "background-color: rgba(244,63,94,0.15); color: #F43F5E;" if x <= 1 else "", 
                            subset=["Ofertas Presentadas"]
                        ),
                        use_container_width=True,
                        hide_index=True
                    )
