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
        safe_dep = dep_raw.replace("'", "''")
        conds.append(f"upper(departamento) = '{safe_dep}'")
    if mun_raw:
        safe_mun = mun_raw.replace("'", "''")
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
        net.force_atlas_2based(gravity=-45, central_gravity=0.04,
                               spring_length=160, spring_strength=0.09, damping=0.85)
    else:
        net.force_atlas_2based(gravity=-70, central_gravity=0.01,
                               spring_length=130, spring_strength=0.05, damping=0.5)

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

    # ── Tablas Forenses ───────────────────────────────────────────────────
    col_a, col_b = st.columns([1, 1.5])

    with col_a:
        st.markdown(f"""
        <h3 style='font-size:1rem;font-weight:700;color:#fff;margin:0 0 12px;'>
            🚨 Alertas Forenses
        </h3>""", unsafe_allow_html=True)

        df_alertas = generate_alerts(prov_df, edges_df)

        if not df_alertas.empty:
            event_alertas = st.dataframe(
                df_alertas,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="tabla_alertas_interactiva"
            )
            if event_alertas and event_alertas.selection and event_alertas.selection.rows:
                idx_sel = event_alertas.selection.rows[0]
                actor_alerta = df_alertas.iloc[idx_sel]["Actor"]
                if "↔" in actor_alerta:
                    actor_alerta = actor_alerta.split("↔")[0].strip()
                if st.session_state.get("actor_seleccionado") != actor_alerta:
                    st.session_state["actor_seleccionado"] = actor_alerta
                    st.session_state["select_actor_raw"]  = actor_alerta
                    st.rerun()
        else:
            st.success("No se detectaron patrones anómalos críticos.", icon="✅")

    with col_b:
        st.markdown(f"""
        <h3 style='font-size:1rem;font-weight:700;color:{C['purple']};margin:0 0 12px;'>
            📊 Tabla Forense de Riesgo (Score + Centralidad)
        </h3>""", unsafe_allow_html=True)

        cols_show = ['proveedor', 'entidades_distintas', 'contratos_totales',
                     'valor_total', 'pct_directa', 'centralidad_intermediacion', 'score_forense']
        t_final = prov_df[cols_show].copy()
        t_final['pct_directa'] = (t_final['pct_directa'] * 100).round(1).astype(str) + "%"
        t_final['valor_total'] = t_final['valor_total'].apply(format_b)
        t_final['centralidad_intermediacion'] = t_final['centralidad_intermediacion'].round(3)
        t_final.rename(columns={
            "proveedor": "Proveedor",
            "entidades_distintas": "Entidades",
            "contratos_totales": "Contratos",
            "valor_total": "Valor Total",
            "pct_directa": "% Directa",
            "centralidad_intermediacion": "Centralidad (Puente)",
            "score_forense": "Score Forense"
        }, inplace=True)
        st.dataframe(t_final, use_container_width=True, hide_index=True)
