"""
app.py — SECOP II · Dashboard Bicapa
=====================================
Arquitectura bicapa:
  CAPA GLOBAL  → sin filtros territoriales → ranking nacional
  CAPA FILTRADA→ dep + municipio → mapa, KPIs, tabla local
SoQL nativo: el servidor agrega, nosotros recibimos <400 filas.
"""

import os, json, unicodedata
import duckdb
import requests
import streamlit as st
import plotly.express as px
import pandas as pd
from network_analysis import render_network_tab

# ─────────────────────────────────────────────────────────────────────────────
# RUTAS Y CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────
BASE      = os.path.dirname(os.path.abspath(__file__))
GEOJSON   = os.path.join(BASE, "data", "depto.json")
GEOJSON_MUN = os.path.join(BASE, "data", "mpio.json")

API_RESOURCE = "jbjy-vk9h"
API_BASE     = f"https://www.datos.gov.co/resource/{API_RESOURCE}.json"
API_TIMEOUT  = 90  # segundos

C = dict(
    bg="#060B14", card="#0D1421", border="#1A2336",
    blue="#4F8EF7", green="#22C55E", amber="#F59E0B", red="#F43F5E",
    purple="#A78BFA", text="#F1F5F9", muted="#64748B",
)

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG + CSS
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="SECOP II · Tiempo Real", page_icon="🇨🇴", layout="wide")
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&display=swap');
html,body,[class*="css"]{{font-family:'Inter',sans-serif;background:{C['bg']};color:{C['text']};}}
.hero{{background:linear-gradient(135deg,#0A1628,#111E35,#0A1628);border:1px solid {C['border']};
       border-radius:18px;padding:28px 36px;margin-bottom:24px;}}
.kpi{{background:{C['card']};border:1px solid {C['border']};border-radius:14px;padding:20px;position:relative;}}
.kpi-bar{{position:absolute;bottom:0;left:0;right:0;height:3px;border-radius:0 0 14px 14px;}}
.kpi-lbl{{font-size:.63rem;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:{C['muted']};margin-bottom:4px;}}
.kpi-val{{font-size:1.8rem;font-weight:900;color:#fff;line-height:1;}}
.kpi-sub{{font-size:.7rem;color:{C['muted']};margin-top:5px;}}
.sec-ttl{{font-size:1rem;font-weight:700;color:#fff;margin-bottom:14px;}}
.panel{{background:{C['card']};border:1px solid {C['border']};border-radius:14px;padding:20px 22px;margin-bottom:4px;}}
.panel-hdr{{display:flex;align-items:center;gap:8px;margin-bottom:14px;}}
.panel-badge{{display:inline-block;padding:2px 10px;border-radius:100px;font-size:.63rem;font-weight:700;letter-spacing:.8px;}}
.badge-global{{background:rgba(167,139,250,.12);color:{C['purple']};border:1px solid rgba(167,139,250,.25);}}
.badge-local{{background:rgba(34,197,94,.12);color:{C['green']};border:1px solid rgba(34,197,94,.25);}}
.ranking-row{{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid {C['border']};}}
.ranking-num{{font-size:.75rem;font-weight:700;color:{C['muted']};width:22px;text-align:right;flex-shrink:0;}}
.ranking-name{{font-size:.78rem;color:{C['text']};flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.ranking-val{{font-size:.75rem;font-weight:700;color:{C['blue']};flex-shrink:0;}}
.ranking-bar-wrap{{width:80px;height:5px;background:{C['border']};border-radius:3px;flex-shrink:0;}}
.ranking-bar-fill{{height:5px;border-radius:3px;background:linear-gradient(90deg,{C['blue']},{C['purple']});}}
.breadcrumb{{background:rgba(79,142,247,.07);border:1px solid rgba(79,142,247,.15);border-radius:10px;
              padding:10px 16px;margin-bottom:16px;font-size:.82rem;color:{C['muted']};}}
.tag-api{{display:inline-block;background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.25);
           color:{C['green']};border-radius:100px;padding:2px 12px;font-size:.68rem;font-weight:700;}}
[data-testid="stSidebar"]{{background:{C['card']};border-right:1px solid {C['border']};}}
header[data-testid="stHeader"]{{background:transparent;height:0;}}
#MainMenu,footer{{display:none;}}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────────────────────
def normalizar(texto: str) -> str:
    """Quita tildes y estandariza nombres para cruzar con GeoJSON.
    ⚠️ NO usar para filtros WHERE contra la API — usar el nombre raw."""
    if not texto: return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in nfkd if not unicodedata.combining(c)).upper().strip()
    # Normalizar variantes de Bogotá
    for bogota_variant in [
        "DISTRITO CAPITAL DE BOGOTA", "BOGOTA D.C.",
        "BOGOTA, D.C.", "DISTRITO ESPECIAL",
    ]:
        t = t.replace(bogota_variant, "BOGOTA")
    t = t.replace("D.C.", "").strip()
    return " ".join(t.split())  # colapsa espacios dobles

def fmt_b(v):
    try:
        v = float(v)
        if v >= 1e12: return f"${v/1e12:.2f} B"
        if v >= 1e9:  return f"${v/1e9:.1f} MM"
        if v >= 1e6:  return f"${v/1e6:.0f} M"
        return f"${v:,.0f}"
    except: return "—"

def fmt_n(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return "—"


# ─────────────────────────────────────────────────────────────────────────────
# CAPA DE CONSULTAS SOQL — filtra en el servidor Socrata
# ─────────────────────────────────────────────────────────────────────────────

def soql_get(params: dict) -> pd.DataFrame:
    """Ejecuta una consulta SoQL contra la API y retorna un DataFrame."""
    try:
        r = requests.get(API_BASE, params=params, timeout=API_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data)
    except requests.Timeout:
        st.error("⏱️ Timeout al consultar la API. Intenta filtrar por año.")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"Error consultando API: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner="Consultando años disponibles...")
def get_anios() -> list[int]:
    df = soql_get({
        "$select": "date_extract_y(fecha_de_firma) AS anio, COUNT(*) AS n",
        "$group":  "date_extract_y(fecha_de_firma)",
        "$where":  "fecha_de_firma IS NOT NULL",
        "$order":  "anio DESC",
        "$limit":  "50",
    })
    if df.empty or "anio" not in df.columns:
        return [2026, 2025, 2024, 2023, 2022, 2021, 2020]
    anios = sorted([int(float(x)) for x in df["anio"].dropna().tolist()], reverse=True)
    # Filtrar años con datos reales (>=100 contratos)
    if "n" in df.columns:
        df["n"]    = pd.to_numeric(df["n"], errors="coerce").fillna(0)
        df["anio"] = pd.to_numeric(df["anio"], errors="coerce")
        anios = sorted(
            [int(a) for a in df.loc[df["n"] >= 5, "anio"].tolist()],
            reverse=True
        )
    return anios if anios else [2026, 2025, 2024, 2023]


@st.cache_data(ttl=3600, show_spinner="Cargando departamentos...")
def get_departamentos(anio: int) -> pd.DataFrame:
    """Suma por departamento — retorna ~35 filas máximo."""
    df = soql_get({
        "$select": "upper(departamento) AS departamento, SUM(valor_del_contrato) AS valor, COUNT(*) AS contratos",
        "$where":  f"date_extract_y(fecha_de_firma) = {anio} AND departamento IS NOT NULL",
        "$group":  "upper(departamento)",
        "$order":  "valor DESC",
        "$limit":  "50",
    })
    if not df.empty:
        df["valor"]     = pd.to_numeric(df["valor"],     errors="coerce").fillna(0)
        df["contratos"] = pd.to_numeric(df["contratos"], errors="coerce").fillna(0)
    else:
        df = pd.DataFrame(columns=["departamento", "valor", "contratos"])
    return df


@st.cache_data(ttl=3600, show_spinner="Cargando municipios...")
def get_municipios(anio: int, dep_raw: str) -> pd.DataFrame:
    """Suma por ciudad — dep_raw es el nombre con acentos tal como lo retorna la API."""
    dep_q = dep_raw.replace("'", "''")
    df = soql_get({
        "$select": "upper(ciudad) AS ciudad, SUM(valor_del_contrato) AS valor, COUNT(*) AS contratos",
        "$where":  (f"date_extract_y(fecha_de_firma) = {anio} "
                    f"AND upper(departamento) = '{dep_q}' "
                    f"AND ciudad IS NOT NULL"),
        "$group":  "upper(ciudad)",
        "$order":  "valor DESC",
        "$limit":  "400",
    })
    if not df.empty:
        df["valor"]     = pd.to_numeric(df["valor"],     errors="coerce").fillna(0)
        df["contratos"] = pd.to_numeric(df["contratos"], errors="coerce").fillna(0)
    else:
        df = pd.DataFrame(columns=["ciudad", "valor", "contratos"])
    return df


@st.cache_data(ttl=3600, show_spinner="Cargando entidades...")
def get_entidades(anio: int, mun_raw: str) -> pd.DataFrame:
    """Top 20 entidades — mun_raw es el nombre con acentos tal como lo retorna la API."""
    mun_q = mun_raw.replace("'", "''")
    df = soql_get({
        "$select": "nombre_entidad, COUNT(*) AS contratos, SUM(valor_del_contrato) AS valor",
        "$where":  (f"date_extract_y(fecha_de_firma) = {anio} "
                    f"AND upper(ciudad) = '{mun_q}' "
                    f"AND nombre_entidad IS NOT NULL"),
        "$group":  "nombre_entidad",
        "$order":  "valor DESC",
        "$limit":  "20",
    })
    if not df.empty:
        df["valor"]     = pd.to_numeric(df["valor"],     errors="coerce").fillna(0)
        df["contratos"] = pd.to_numeric(df["contratos"], errors="coerce").fillna(0)
    else:
        df = pd.DataFrame(columns=["nombre_entidad", "valor", "contratos"])
    return df


@st.cache_data(ttl=3600, show_spinner="Cargando ranking nacional...")
def get_top_entidades_global(anio: int, n: int = 20) -> pd.DataFrame:
    """Top N entidades a nivel nacional. CAPA GLOBAL — sin filtro territorial."""
    df = soql_get({
        "$select": "nombre_entidad, upper(departamento) AS departamento, COUNT(*) AS contratos, SUM(valor_del_contrato) AS valor",
        "$where":  f"date_extract_y(fecha_de_firma) = {anio} AND nombre_entidad IS NOT NULL",
        "$group":  "nombre_entidad, upper(departamento)",
        "$order":  "valor DESC",
        "$limit":  str(n),
    })
    if not df.empty:
        df["valor"]     = pd.to_numeric(df["valor"],     errors="coerce").fillna(0)
        df["contratos"] = pd.to_numeric(df["contratos"], errors="coerce").fillna(0)
    else:
        df = pd.DataFrame(columns=["nombre_entidad", "departamento", "valor", "contratos"])
    return df


@st.cache_data(ttl=3600, show_spinner="Cargando ranking por departamento...")
def get_top_entidades_dep(anio: int, dep_raw: str, n: int = 15) -> pd.DataFrame:
    """Top N entidades dentro de un departamento — usa dep_raw con acentos."""
    dep_q = dep_raw.replace("'", "''")
    df = soql_get({
        "$select": "nombre_entidad, upper(ciudad) AS ciudad, COUNT(*) AS contratos, SUM(valor_del_contrato) AS valor",
        "$where":  (f"date_extract_y(fecha_de_firma) = {anio} "
                    f"AND upper(departamento) = '{dep_q}' "
                    f"AND nombre_entidad IS NOT NULL"),
        "$group":  "nombre_entidad, upper(ciudad)",
        "$order":  "valor DESC",
        "$limit":  str(n),
    })
    if not df.empty:
        df["valor"]     = pd.to_numeric(df["valor"],     errors="coerce").fillna(0)
        df["contratos"] = pd.to_numeric(df["contratos"], errors="coerce").fillna(0)
    else:
        df = pd.DataFrame(columns=["nombre_entidad", "ciudad", "valor", "contratos"])
    return df


@st.cache_data(ttl=3600, show_spinner="Calculando KPIs...")
def get_kpis(anio: int, dep_raw: str, mun_raw: str, actor_filter: str = "") -> dict:
    """KPIs — usa nombres RAW de la API (con acentos) para que el WHERE haga match."""
    conds = [f"date_extract_y(fecha_de_firma) = {anio}"]
    if dep_raw:
        safe = dep_raw.replace("'", "''")
        conds.append(f"upper(departamento) = '{safe}'")
    if mun_raw:
        safe = mun_raw.replace("'", "''")
        conds.append(f"upper(ciudad) = '{safe}'")
    if actor_filter:
        safe_actor = actor_filter.replace("'", "''")
        conds.append(f"(upper(proveedor_adjudicado) = '{safe_actor}' OR upper(nombre_entidad) = '{safe_actor}')")

    df = soql_get({
        "$select": "SUM(valor_del_contrato) AS total_valor, COUNT(*) AS total_contratos, COUNT(DISTINCT nombre_entidad) AS total_entidades",
        "$where":  " AND ".join(conds),
        "$limit":  "1",
    })
    if df.empty:
        return {"total_valor": 0, "total_contratos": 0, "total_entidades": 0}
    row = df.iloc[0]
    return {
        "total_valor":     float(row.get("total_valor", 0) or 0),
        "total_contratos": int(float(row.get("total_contratos", 0) or 0)),
        "total_entidades": int(float(row.get("total_entidades", 0) or 0)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# GEOJSON
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def load_geojson(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# SESIÓN
# Doble nombre: _norm (para GeoJSON/display) y _raw (para WHERE en la API)
# ─────────────────────────────────────────────────────────────────────────────
def init_state():
    # El año por defecto se determina dinámicamente (primer año con datos reales)
    # Para no llamar a la API en init (antes del primer render), usamos 2024 como
    # fallback seguro y lo actualizamos en main() después de get_anios().
    defaults = {
        "dep_norm": "", "dep_raw": "",
        "mun_norm": "", "mun_raw": "",
        "anio": 2024,
        "anio_inicializado": False,   # flag para saber si ya sincronizamos con la API
        "actor_seleccionado": ""      # Sincronización entre Redes y KPIs
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

def set_dep(dep_norm: str, dep_raw: str):
    """dep_norm = sin tildes (GeoJSON). dep_raw = nombre original de la API."""
    st.session_state["dep_norm"] = dep_norm
    st.session_state["dep_raw"]  = dep_raw
    st.session_state["mun_norm"] = ""
    st.session_state["mun_raw"]  = ""

def set_mun(mun_norm: str, mun_raw: str):
    st.session_state["mun_norm"] = mun_norm
    st.session_state["mun_raw"]  = mun_raw

def reset():
    st.session_state["dep_norm"]  = ""
    st.session_state["dep_raw"]   = ""
    st.session_state["mun_norm"]  = ""
    st.session_state["mun_raw"]   = ""


# ─────────────────────────────────────────────────────────────────────────────
# RENDERIZADO KPIs
# ─────────────────────────────────────────────────────────────────────────────
def render_kpis(anio, dep, mun, actor_filter=""):
    k = get_kpis(anio, dep, mun, actor_filter)
    items = [
        ("💰", "Presupuesto Total",  fmt_b(k["total_valor"]),      "Valor adjudicado", C["blue"]),
        ("📄", "Total Contratos",    fmt_n(k["total_contratos"]),   "Procesos firmados", C["green"]),
        ("🏛️", "Entidades Únicas",   fmt_n(k["total_entidades"]),   "NITs distintos",   C["purple"]),
    ]
    cols = st.columns(3)
    for (ico, lbl, val, sub, color), col in zip(items, cols):
        with col:
            st.markdown(f"""
            <div class="kpi">
                <div style="font-size:1.3rem;margin-bottom:6px">{ico}</div>
                <div class="kpi-lbl">{lbl}</div>
                <div class="kpi-val">{val}</div>
                <div class="kpi-sub">{sub}</div>
                <div class="kpi-bar" style="background:linear-gradient(90deg,{color}33,{color});"></div>
            </div>""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# MAPA DEPARTAMENTOS
# ─────────────────────────────────────────────────────────────────────────────
def render_mapa_departamentos(anio: int):
    geo = load_geojson(GEOJSON)
    if not geo:
        st.error("GeoJSON de departamentos no encontrado.")
        return

    df_secop = get_departamentos(anio)
    if df_secop.empty:
        st.warning("Sin datos de departamentos para el año seleccionado.")
        return

    # dep_raw = nombre tal como viene de la API (con acentos, para WHERE)
    # dep_norm = sin acentos, para cruzar con GeoJSON
    df_secop["dep_raw"]  = df_secop["departamento"]           # original de API
    df_secop["dep_norm"] = df_secop["departamento"].apply(normalizar)

    # Base: todos los departamentos del GeoJSON
    base = pd.DataFrame([{
        "codigo":   f["properties"]["DPTO"],
        "nombre":   f["properties"]["NOMBRE_DPT"],
        "dep_norm": normalizar(f["properties"]["NOMBRE_DPT"]),
    } for f in geo["features"]])

    df = base.merge(df_secop[["dep_norm", "dep_raw", "valor", "contratos"]], on="dep_norm", how="left")
    df["valor"]     = df["valor"].fillna(0)
    df["contratos"] = df["contratos"].fillna(0)
    # dep_raw faltante: usar nombre normalizado del GeoJSON como fallback de display
    df["dep_raw"]   = df["dep_raw"].fillna(df["dep_norm"])
    df["tooltip"]   = df["nombre"].str.title()

    fig = px.choropleth_mapbox(
        df, geojson=geo,
        locations="codigo", featureidkey="properties.DPTO",
        color="valor",
        hover_name="tooltip",
        hover_data={"contratos": True, "valor": True, "codigo": False},
        color_continuous_scale=[
            [0, "#060B14"], [0.1, "#0D2145"], [0.4, "#1E3A6E"],
            [0.7, C["blue"]], [1, "#93C5FD"]
        ],
        mapbox_style="carto-darkmatter",
        zoom=4.2, center={"lat": 4.57, "lon": -74.30},
        opacity=0.8,
        labels={"valor": "Valor COP", "contratos": "Contratos"},
        # custom_data[0]=dep_norm, custom_data[1]=dep_raw (nombre original API)
        custom_data=["dep_norm", "dep_raw"],
    )
    fig.update_layout(
        paper_bgcolor=C["bg"], font_color=C["text"],
        margin=dict(l=0, r=0, t=0, b=0), height=520,
        coloraxis_colorbar=dict(tickfont=dict(color=C["muted"]), title="Valor"),
    )

    event = st.plotly_chart(fig, use_container_width=True, on_select="rerun", key="map_dep", selection_mode="points")
    pts = (event or {}).get("selection", {}).get("points", [])
    if pts:
        cd = pts[0].get("customdata", [])
        if len(cd) >= 2:
            dep_norm_click = cd[0]   # para display y GeoJSON
            dep_raw_click  = cd[1]   # para WHERE en API (con acentos)
            set_dep(dep_norm=dep_norm_click, dep_raw=dep_raw_click)
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# MAPA MUNICIPIOS
# ─────────────────────────────────────────────────────────────────────────────
def render_mapa_municipios(anio: int, dep_norm: str, dep_raw: str):
    """dep_norm = sin acentos (para filtrar GeoJSON). dep_raw = nombre API (para WHERE)."""
    geo = load_geojson(GEOJSON_MUN)
    if not geo:
        st.error("GeoJSON de municipios no encontrado.")
        return

    # Consultar municipios usando dep_raw (con acentos) para que el WHERE haga match
    df_secop = get_municipios(anio, dep_raw)
    # mun_raw = ciudad tal como viene de la API (uppercase, con acentos)
    df_secop["mun_raw"]  = df_secop["ciudad"]
    df_secop["mun_norm"] = df_secop["ciudad"].apply(normalizar)

    # Filtrar GeoJSON al departamento usando dep_norm
    base_muns = [f for f in geo["features"] if normalizar(f["properties"].get("NOMBRE_DPT", "")) == dep_norm]

    if not base_muns:
        st.warning(f"Sin geometrías para municipios de {dep_norm}.")
        return

    base = pd.DataFrame([{
        "codigo":   f["properties"]["MPIOS"],
        "nombre":   f["properties"]["NOMBRE_MPI"],
        "mun_norm": normalizar(f["properties"]["NOMBRE_MPI"]),
    } for f in base_muns])

    df = base.merge(df_secop[["mun_norm", "mun_raw", "valor", "contratos"]], on="mun_norm", how="left")
    df["valor"]     = df["valor"].fillna(0)
    df["contratos"] = df["contratos"].fillna(0)
    df["mun_raw"]   = df["mun_raw"].fillna(df["mun_norm"])  # fallback
    df["tooltip"]   = df["nombre"].str.title()

    # Calcular centro dinámico del departamento
    geo_dep = {**geo, "features": base_muns}
    all_coords = []
    for f in base_muns:
        geom = f["geometry"]
        if geom["type"] == "Polygon":
            all_coords.extend(geom["coordinates"][0])
        elif geom["type"] == "MultiPolygon":
            for poly in geom["coordinates"]:
                all_coords.extend(poly[0])
    if all_coords:
        lons = [c[0] for c in all_coords]
        lats = [c[1] for c in all_coords]
        center_lat = (min(lats) + max(lats)) / 2
        center_lon = (min(lons) + max(lons)) / 2
        span       = max(max(lats)-min(lats), max(lons)-min(lons))
        zoom       = max(5.0, 8.5 - span * 0.8)
    else:
        center_lat, center_lon, zoom = 4.57, -74.30, 5.5

    fig = px.choropleth_mapbox(
        df, geojson=geo_dep,
        locations="codigo", featureidkey="properties.MPIOS",
        color="valor",
        hover_name="tooltip",
        hover_data={"contratos": True, "valor": True, "codigo": False},
        color_continuous_scale=[
            [0, "#051A0E"], [0.1, "#0A3D1E"], [0.4, "#166534"],
            [0.7, C["green"]], [1, "#86EFAC"]
        ],
        mapbox_style="carto-darkmatter",
        zoom=zoom, center={"lat": center_lat, "lon": center_lon},
        opacity=0.8,
        labels={"valor": "Valor COP", "contratos": "Contratos"},
        # custom_data[0]=mun_norm, custom_data[1]=mun_raw (nombre original API)
        custom_data=["mun_norm", "mun_raw"],
    )
    fig.update_layout(
        paper_bgcolor=C["bg"], font_color=C["text"],
        margin=dict(l=0, r=0, t=0, b=0), height=520,
        coloraxis_colorbar=dict(tickfont=dict(color=C["muted"]), title="Valor"),
    )

    event = st.plotly_chart(fig, use_container_width=True, on_select="rerun", key="map_mun", selection_mode="points")
    pts = (event or {}).get("selection", {}).get("points", [])
    if pts:
        cd = pts[0].get("customdata", [])
        if len(cd) >= 2:
            mun_norm_click = cd[0]   # para display y GeoJSON
            mun_raw_click  = cd[1]   # para WHERE en API (con acentos)
            set_mun(mun_norm=mun_norm_click, mun_raw=mun_raw_click)
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# TABLA ENTIDADES
# ─────────────────────────────────────────────────────────────────────────────
def render_tabla_entidades(anio: int, mun_raw: str, mun_norm: str):
    """mun_raw = nombre ciudad tal como viene de la API (para WHERE). mun_norm = para display."""
    if not mun_raw:
        return

    st.markdown("<hr style='border:none;border-top:1px solid #1A2336;margin:24px 0;'>", unsafe_allow_html=True)
    st.markdown(f"<div class='sec-ttl'>🏛️ Entidades Contratantes en {mun_norm.title()}</div>", unsafe_allow_html=True)

    df = get_entidades(anio, mun_raw)
    if df.empty:
        st.info("Sin entidades para este municipio en el año seleccionado.")
        return

    col_t, col_b = st.columns([3, 2])
    with col_t:
        df_disp = df[["nombre_entidad", "contratos", "valor"]].copy()
        df_disp["presupuesto_fmt"] = df_disp["valor"].apply(fmt_b)
        df_disp["contratos_fmt"]   = df_disp["contratos"].apply(fmt_n)
        df_disp = df_disp[["nombre_entidad", "contratos_fmt", "presupuesto_fmt"]]
        df_disp.columns = ["Entidad", "Contratos", "Presupuesto"]
        st.write(
            df_disp.style
            .set_properties(**{"background-color": C["card"], "color": C["text"], "border": f"1px solid {C['border']}", "font-size": ".79rem"})
            .set_table_styles([{"selector": "th", "props": [("background-color", C["border"]), ("color", "#fff"), ("font-size", ".73rem"), ("padding", "8px 10px")]}])
            .to_html(), unsafe_allow_html=True
        )

    with col_b:
        df_plot = df.head(10).copy()
        df_plot["nombre_corto"] = df_plot["nombre_entidad"].str[:30] + "…"
        fig = px.bar(
            df_plot.sort_values("valor"), x="valor", y="nombre_corto", orientation="h",
            text=df_plot.sort_values("valor")["valor"].apply(fmt_b),
            color="valor",
            color_continuous_scale=[[0, C["blue"]], [1, "#93C5FD"]],
        )
        fig.update_traces(textposition="outside", textfont_size=9)
        fig.update_layout(
            paper_bgcolor=C["bg"], plot_bgcolor=C["bg"], font_color=C["text"],
            yaxis=dict(showgrid=False, title=""),
            xaxis=dict(showgrid=True, gridcolor=C["border"], title=""),
            showlegend=False, coloraxis_showscale=False,
            margin=dict(l=0, r=0, t=0, b=0), height=360,
        )
        st.plotly_chart(fig, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# CAPA GLOBAL — Ranking nacional y por departamento
# ─────────────────────────────────────────────────────────────────────────────

def _ranking_html(df: pd.DataFrame, badge_class: str, titulo: str, subtitulo: str) -> str:
    """Genera HTML del panel de ranking — usar con st.markdown(..., unsafe_allow_html=True)."""
    if df.empty:
        return (
            "<div class='panel'>"
            "<p style='color:" + C['muted'] + ";font-size:.8rem;padding:8px 0;'>Sin datos disponibles.</p>"
            "</div>"
        )

    val_max = float(df["valor"].max() or 1)
    filas = []
    for i, row in enumerate(df.itertuples(), 1):
        pct  = int(float(getattr(row, 'valor', 0)) / val_max * 100)
        raw  = str(getattr(row, 'nombre_entidad', ''))
        nombre = (raw[:45] + '\u2026') if len(raw) > 45 else raw
        val_str = fmt_b(float(getattr(row, 'valor', 0)))
        filas.append(
            "<div class='ranking-row'>"
            "<div class='ranking-num'>" + f"#{i}" + "</div>"
            "<div class='ranking-name'>" + nombre + "</div>"
            "<div class='ranking-bar-wrap'>"
            "<div class='ranking-bar-fill' style='width:" + str(pct) + "%;'></div>"
            "</div>"
            "<div class='ranking-val'>" + val_str + "</div>"
            "</div>"
        )

    return (
        "<div class='panel'>"
        "<div class='panel-hdr'>"
        "<span class='panel-badge " + badge_class + "'>" + subtitulo + "</span>"
        "<span style='font-size:.9rem;font-weight:700;color:#fff;'>" + titulo + "</span>"
        "</div>"
        + "".join(filas) +
        "</div>"
    )


def render_ranking_global(anio: int, dep_raw: str, dep_norm: str, mun_raw: str, mun_norm: str):
    """Siempre visible: ranking global + ranking del nivel activo.
    Usa *_raw para consultas API y *_norm para display."""
    st.markdown(
        f"<div style='font-size:.62rem;font-weight:700;letter-spacing:1.2px;"
        f"text-transform:uppercase;color:{C['muted']};margin-bottom:10px;'>Rankings</div>",
        unsafe_allow_html=True,
    )

    col_g, col_l = st.columns(2)

    # ── Columna izquierda: Ranking GLOBAL (siempre) ───────────────────────────
    with col_g:
        df_g = get_top_entidades_global(anio, n=15)
        st.markdown(
            _ranking_html(df_g, "badge-global", f"Top Nacional {anio}", "🌎 GLOBAL"),
            unsafe_allow_html=True,
        )

    # ── Columna derecha: Ranking FILTRADO según nivel ─────────────────────────
    with col_l:
        if mun_raw:
            df_l      = get_entidades(anio, mun_raw)       # usa raw para WHERE
            titulo    = f"Top en {mun_norm.title()}"
            subtitulo = "📍 LOCAL"
            badge     = "badge-local"
        elif dep_raw:
            df_l      = get_top_entidades_dep(anio, dep_raw)  # usa raw para WHERE
            titulo    = f"Top en {dep_norm.title()}"
            subtitulo = "📍 DEP"
            badge     = "badge-local"
        else:
            df_l      = get_top_entidades_global(anio, n=15)
            titulo    = f"Top por Presupuesto {anio}"
            subtitulo = "🏆 NACIONAL"
            badge     = "badge-global"

        st.markdown(
            _ranking_html(df_l, badge, titulo, subtitulo),
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# BREADCRUMB
# ─────────────────────────────────────────────────────────────────────────────
def render_breadcrumb(dep_norm: str, mun_norm: str, anio: int):
    partes = [f"🇨🇴 Colombia ({anio})"]
    if dep_norm: partes.append(f"📍 {dep_norm.title()}")
    if mun_norm: partes.append(f"🏩️ {mun_norm.title()}")
    st.markdown(f"<div class='breadcrumb'>{' › '.join(partes)}</div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    init_state()
    dep_norm = st.session_state["dep_norm"]
    dep_raw  = st.session_state["dep_raw"]
    mun_norm = st.session_state["mun_norm"]
    mun_raw  = st.session_state["mun_raw"]

    # ── Sidebar ─────────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown(f"""
        <div style='text-align:center;padding:14px 0;'>
            <div style='font-size:2rem;'>🇨🇴</div>
            <div style='font-weight:900;font-size:1.1rem;color:#fff;'>SECOP II</div>
            <div style='font-size:.7rem;color:{C['muted']};'>Monitor de Contratación</div>
            <div style='margin-top:8px;'><span class='tag-api'>API EN VIVO</span></div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("<hr style='border:none;border-top:1px solid #1A2336;margin:8px 0;'>", unsafe_allow_html=True)

        anios = get_anios()

        # Sincronizar año por defecto con el primer año real de la API
        if not st.session_state.get("anio_inicializado") and anios:
            st.session_state["anio"] = anios[0]
            st.session_state["anio_inicializado"] = True

        anio_idx = anios.index(st.session_state["anio"]) if st.session_state["anio"] in anios else 0
        anio_sel = st.selectbox("📅 Año", anios, index=anio_idx)
        if anio_sel != st.session_state["anio"]:
            st.session_state["anio"] = anio_sel
            reset()
            st.rerun()
        anio = st.session_state["anio"]

        st.markdown("<hr style='border:none;border-top:1px solid #1A2336;margin:12px 0;'>", unsafe_allow_html=True)

        nivel = "Nacional"
        if dep_norm and mun_norm: nivel = f"Municipio · {mun_norm.title()}"
        elif dep_norm:            nivel = f"Departamento · {dep_norm.title()}"

        st.markdown(f"""
        <div style='background:rgba(79,142,247,.07);border:1px solid rgba(79,142,247,.15);border-radius:10px;padding:12px;font-size:.78rem;'>
            <div style='color:{C['blue']};font-weight:700;margin-bottom:4px;'>📍 Nivel</div>
            <div style='color:#fff;font-weight:600;'>{nivel}</div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("")
        if dep_norm:
            if st.button("⬆️ Volver a Colombia", use_container_width=True): reset(); st.rerun()
        if mun_norm:
            if st.button(f"⬆️ Volver a {dep_norm.title()}", use_container_width=True):
                set_dep(dep_norm=dep_norm, dep_raw=dep_raw); st.rerun()

        st.markdown("<hr style='border:none;border-top:1px solid #1A2336;margin:12px 0;'>", unsafe_allow_html=True)
        if st.button("🗑️ Limpiar caché", use_container_width=True):
            st.cache_data.clear(); st.rerun()

        st.markdown(f"<div style='font-size:.68rem;color:{C['muted']};margin-top:8px;line-height:1.8;'>Fuente: <b>SECOP II</b><br>Colombia Compra Eficiente<br>Recurso: <code>jbjy-vk9h</code></div>", unsafe_allow_html=True)

    # ── Header ─────────────────────────────────────────────────────────────────
    anio = st.session_state["anio"]
    st.markdown(f"""
    <div class='hero'>
        <div style='font-size:.7rem;font-weight:700;color:{C['blue']};letter-spacing:1px;margin-bottom:6px;'>SECOP II · CONTRATOS ELECTRÓNICOS</div>
        <h1 style='font-size:1.9rem;font-weight:900;margin:0 0 8px;color:#fff;'>Monitor de Contratación Pública</h1>
        <p style='margin:0;color:{C['muted']};font-size:.88rem;'>
            Transparencia · Nivel: <span style='color:#fff;font-weight:600;'>{nivel}</span>
            &nbsp;|&nbsp; Año: <span style='color:{C['amber']};font-weight:700;'>{anio}</span>
            &nbsp;|&nbsp; <span class='tag-api'>SoQL nativo — sin timeout</span>
        </p>
    </div>
    """, unsafe_allow_html=True)

    render_breadcrumb(dep_norm, mun_norm, anio)

    # ── KPIs (usando raw para WHERE) globales a todas las pestañas ────────────
    actor = st.session_state.get("actor_seleccionado", "")
    if actor:
        st.markdown(f"<div style='background:rgba(244, 63, 94, .1); color:#F43F5E; padding:8px; border-radius:8px; font-weight:700; margin-bottom:10px; font-size:0.8rem;'>🔎 Filtrando KPIs por Actor: {actor}</div>", unsafe_allow_html=True)
    render_kpis(anio, dep_raw, mun_raw, actor)
    st.markdown("<br>", unsafe_allow_html=True)

    # ── Tabs de Navegación ────────────────────────────────────────────────────
    tab_geo, tab_red = st.tabs(["🌍 Explorador Territorial", "🕸️ Análisis de Redes & Anomalías"])
    
    with tab_geo:
        # ── Mapa Interactivo (Ancho completo) ─────────────────────────────────────
        if not dep_norm:
            hint = "👆 Haz clic en un departamento para explorar sus municipios"
            ttl  = f"🗺️ Colombia · Departamentos — {anio}"
        else:
            hint = "👆 Haz clic en un municipio para ver sus entidades"
            ttl  = f"🗺️ Municipios de {dep_norm.title()} — {anio}"
    
        st.markdown(
            f"<div style='font-size:.62rem;font-weight:700;letter-spacing:1.2px;"
            f"text-transform:uppercase;color:{C['muted']};'>Mapa Interactivo</div>",
            unsafe_allow_html=True,
        )
        st.markdown(f"<div class='sec-ttl'>{ttl}</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div style='background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.2);"
            f"border-radius:8px;padding:6px 12px;font-size:.74rem;color:{C['amber']};"
            f"margin-bottom:12px;'>{hint}</div>",
            unsafe_allow_html=True,
        )
        
        if not dep_norm:
            render_mapa_departamentos(anio)
        else:
            # Pasa dep_norm (para filtrar GeoJSON) y dep_raw (para WHERE en API)
            render_mapa_municipios(anio, dep_norm=dep_norm, dep_raw=dep_raw)
    
        st.markdown("<br>", unsafe_allow_html=True)
    
        # ── Rankings (Debajo del mapa) ────────────────────────────────────────────
        render_ranking_global(anio, dep_raw=dep_raw, dep_norm=dep_norm,
                              mun_raw=mun_raw, mun_norm=mun_norm)
    
        # ── Tabla entidades (solo al seleccionar municipio) ───────────────────────
        if mun_raw:
            render_tabla_entidades(anio, mun_raw=mun_raw, mun_norm=mun_norm)

    with tab_red:
        render_network_tab(anio, dep_raw, mun_raw)


if __name__ == "__main__":
    main()
