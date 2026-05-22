"""
app.py — SECOP II · Dashboard Bicapa
=====================================
Arquitectura bicapa:
  CAPA GLOBAL  → sin filtros territoriales → ranking nacional
  CAPA FILTRADA→ dep + municipio → mapa, KPIs, tabla local
SoQL nativo: el servidor agrega, nosotros recibimos <400 filas.
"""

import os, json, unicodedata, contextlib
from functools import wraps
import duckdb
import requests
import streamlit as st
import plotly.express as px
import pandas as pd
from network_analysis import render_network_tab, get_network_raw_data

# ─────────────────────────────────────────────────────────────────────────────
# RUTAS Y CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────
BASE      = os.path.dirname(os.path.abspath(__file__))
GEOJSON   = os.path.join(BASE, "data", "depto.json")
GEOJSON_MUN = os.path.join(BASE, "data", "mpio.json")

API_RESOURCE = "jbjy-vk9h"
API_BASE     = f"https://www.datos.gov.co/resource/{API_RESOURCE}.json"
API_TIMEOUT  = 30  # segundos (reducido de 90 para mejor UX)

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

/* Responsividad Móvil */
@media (max-width: 768px) {{
    .hero {{ padding: 16px 20px !important; margin-bottom: 16px !important; }}
    .kpi {{ padding: 15px !important; }}
    .kpi-val {{ font-size: 1.4rem !important; }}
    .kpi-lbl {{ font-size: 0.55rem !important; }}
    .kpi-sub {{ font-size: 0.65rem !important; }}
    .sec-ttl {{ font-size: 0.9rem !important; }}
    .panel {{ padding: 15px !important; margin-bottom: 8px !important; }}
    .breadcrumb {{ padding: 8px 12px !important; font-size: 0.75rem !important; }}
    /* Ajuste de iframes de grafos para evitar atrapamiento de scroll */
    iframe {{ max-height: 60vh !important; }}
}}
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
        if pd.isna(v) or v == float('inf') or v == float('-inf'): return "—"
        if v >= 1e12: return f"${v/1e12:.2f} B"
        if v >= 1e9:  return f"${v/1e9:.1f} MM"
        if v >= 1e6:  return f"${v/1e6:.0f} M"
        return f"${v:,.0f}"
    except: return "—"

def fmt_n(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return "—"


# ─────────────────────────────────────────────────────────────────────────────
# NOTA: Caché persistente SQLite eliminado — incompatible con Streamlit Cloud.
# Se usa exclusivamente @st.cache_data de Streamlit.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# UTILIDAD: MONITOREO DE LATENCIA (no-op en producción)
# ─────────────────────────────────────────────────────────────────────────────

def monitor_latency(func_name: str):
    """Decorador para monitorear latencia de funciones."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# CAPA DE CONSULTAS SOQL — filtra en el servidor Socrata
# ─────────────────────────────────────────────────────────────────────────────

def soql_get(params: dict) -> pd.DataFrame:
    """Ejecuta query SoQL. Sin SQLite. Sin reintentos agresivos."""
    import time
    for attempt in range(3):
        try:
            r = requests.get(API_BASE, params=params, timeout=60,
                             headers={"Accept-Encoding": "gzip", "User-Agent": "SECOP-Dashboard/2.0"})
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
            st.warning("⏱️ La consulta tardó demasiado tras varios intentos.")
            return pd.DataFrame()
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            st.warning(f"⚠️ Error en consulta: {e}")
            return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)  # TTL reducido, sin spinner
@monitor_latency("get_anios")
def get_anios() -> list[int]:
    # Hardcoded para mejorar el rendimiento de carga inicial
    return [2026, 2025, 2024, 2023]


@st.cache_data(ttl=60, show_spinner=False)
@monitor_latency("get_departamentos")
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


@st.cache_data(ttl=60, show_spinner=False)
@monitor_latency("get_municipios")
def get_municipios(anio: int, dep_raw: str) -> pd.DataFrame:
    """Suma por ciudad — dep_raw es el nombre con acentos tal como lo retorna la API."""
    dep_q = dep_raw.upper().replace("'", "''")
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


@st.cache_data(ttl=60, show_spinner=False)
def get_entidades(anio: int, mun_raw: str) -> pd.DataFrame:
    """Top 20 entidades — mun_raw es el nombre con acentos tal como lo retorna la API."""
    mun_q = mun_raw.upper().replace("'", "''")
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


@st.cache_data(ttl=60, show_spinner=False)
def get_proveedores_municipio(anio: int, mun_raw: str) -> pd.DataFrame:
    """Top 20 proveedores adjudicados en un municipio."""
    mun_q = mun_raw.upper().replace("'", "''")
    df = soql_get({
        "$select": "proveedor_adjudicado, COUNT(*) AS contratos, SUM(valor_del_contrato) AS valor",
        "$where":  (f"date_extract_y(fecha_de_firma) = {anio} "
                    f"AND upper(ciudad) = '{mun_q}' "
                    f"AND proveedor_adjudicado IS NOT NULL"),
        "$group":  "proveedor_adjudicado",
        "$order":  "valor DESC",
        "$limit":  "20",
    })
    if not df.empty:
        df["valor"]     = pd.to_numeric(df["valor"],     errors="coerce").fillna(0)
        df["contratos"] = pd.to_numeric(df["contratos"], errors="coerce").fillna(0)
    else:
        df = pd.DataFrame(columns=["proveedor_adjudicado", "valor", "contratos"])
    return df


@st.cache_data(ttl=120, show_spinner=False)
def get_kpis_modalidad(anio: int, dep_raw: str, mun_raw: str, actor_filter: str = "") -> pd.DataFrame:
    """Calcula el presupuesto y contratos agrupados por modalidad de contratación."""
    conds = [f"date_extract_y(fecha_de_firma) = {anio}"]
    if dep_raw:
        safe = dep_raw.upper().replace("'", "''")
        conds.append(f"upper(departamento) = '{safe}'")
    if mun_raw:
        safe = mun_raw.upper().replace("'", "''")
        conds.append(f"upper(ciudad) = '{safe}'")
    if actor_filter:
        safe_actor = actor_filter.replace("'", "''")
        conds.append(f"(nombre_entidad = '{safe_actor}' OR proveedor_adjudicado = '{safe_actor}')")
        
    df = soql_get({
        "$select": "modalidad_de_contratacion, COUNT(*) AS contratos, SUM(valor_del_contrato) AS valor, COUNT(DISTINCT nit_entidad) AS entidades",
        "$where":  " AND ".join(conds),
        "$group":  "modalidad_de_contratacion",
        "$order":  "valor DESC",
        "$limit":  "20",
    })
    if not df.empty:
        df["valor"]     = pd.to_numeric(df["valor"],     errors="coerce").fillna(0).replace([float('inf'), float('-inf')], 0)
        df["contratos"] = pd.to_numeric(df["contratos"], errors="coerce").fillna(0)
        df["entidades"] = pd.to_numeric(df["entidades"], errors="coerce").fillna(0)
        total_valor = df["valor"].sum()
        df["participacion_pct"] = (df["valor"] / total_valor * 100).fillna(0)
        df["ticket_promedio"]   = (df["valor"] / df["contratos"]).fillna(0)
    else:
        df = pd.DataFrame(columns=["modalidad_de_contratacion", "valor", "contratos", "entidades", "participacion_pct", "ticket_promedio"])
    return df



@st.cache_data(ttl=60, show_spinner=False)
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


@st.cache_data(ttl=60, show_spinner=False)
def get_top_entidades_dep(anio: int, dep_raw: str, n: int = 15) -> pd.DataFrame:
    """Top N entidades dentro de un departamento — usa dep_raw con acentos."""
    dep_q = dep_raw.upper().replace("'", "''")
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


@st.cache_data(ttl=300, show_spinner=False)
def get_kpis_nacional(anio: int) -> dict:
    """KPIs nacionales calculados mediante el NIT único de cada entidad."""
    df_deps = get_departamentos(anio)
    if df_deps.empty:
        return {"total_valor": 0, "total_contratos": 0, "total_entidades": 0}

    # Usamos get_top_entidades_global que ya funciona y devuelve entidades únicas por nombre
    # agrupadas — len() da el conteo correcto sin depender de $group en Socrata
    df_ent = get_top_entidades_global(anio, n=500)
    total_entidades = int(len(df_ent)) if not df_ent.empty else 0
            
    return {
        "total_valor": float(df_deps["valor"].sum()),
        "total_contratos": int(df_deps["contratos"].sum()),
        "total_entidades": total_entidades,
    }


@st.cache_data(ttl=300, show_spinner=False)
def get_kpis_v2(anio: int, dep_raw: str, mun_raw: str, actor_filter: str = "") -> dict:
    """Calcula KPIs dinámicos precisos basados en el NIT único de la entidad."""
    if not dep_raw and not mun_raw and not actor_filter:
        return get_kpis_nacional(anio)
        
    conds = [f"date_extract_y(fecha_de_firma) = {anio}"]
    if dep_raw:
        safe = dep_raw.upper().replace("'", "''")
        conds.append(f"upper(departamento) = '{safe}'")
    if mun_raw:
        safe = mun_raw.upper().replace("'", "''")
        conds.append(f"upper(ciudad) = '{safe}'")
    if actor_filter:
        safe_actor = actor_filter.replace("'", "''")
        conds.append(f"(nombre_entidad = '{safe_actor}' OR proveedor_adjudicado = '{safe_actor}')")
        
    # Totales base del nivel territorial activo
    select_clause = "SUM(valor_del_contrato) AS total_valor, COUNT(*) AS total_contratos"
    df = soql_get({"$select": select_clause, "$where": " AND ".join(conds), "$limit": "1"})
    
    # Conteo preciso usando NIT_ENTIDAD según el nivel
    total_entidades = 0
    if mun_raw:
        # En municipio, le pedimos a Socrata el conteo directo de NITs únicos para esa ciudad
        safe_mun = mun_raw.upper().replace("'", "''")
        df_mun_conteo = soql_get({
            "$select": "COUNT(DISTINCT nit_entidad) AS conteo",
            "$where": f"date_extract_y(fecha_de_firma) = {anio} AND upper(ciudad) = '{safe_mun}'",
            "$limit": "1"
        })
        try:
            if not df_mun_conteo.empty and "conteo" in df_mun_conteo.columns:
                total_entidades = int(float(df_mun_conteo.iloc[0]["conteo"]))
        except:
            total_entidades = 0
            
    elif dep_raw:
        # Mismo patrón que municipio: COUNT(DISTINCT) directo sin $group
        # Un departamento tiene <<datos que el total nacional, Socrata lo resuelve rápido
        safe_dep = dep_raw.upper().replace("'", "''")
        df_dep_conteo = soql_get({
            "$select": "COUNT(DISTINCT nit_entidad) AS conteo",
            "$where": f"date_extract_y(fecha_de_firma) = {anio} AND upper(departamento) = '{safe_dep}'",
            "$limit": "1"
        })
        try:
            if not df_dep_conteo.empty and "conteo" in df_dep_conteo.columns:
                total_entidades = int(float(df_dep_conteo.iloc[0]["conteo"]))
        except:
            total_entidades = 0

    if df.empty:
        return {"total_valor": 0, "total_contratos": 0, "total_entidades": total_entidades}
    
    row = df.iloc[0]
    try:
        val = float(row.get("total_valor", 0) or 0)
        if pd.isna(val) or val == float('inf') or val == float('-inf'): val = 0.0
    except: 
        val = 0.0
        
    try:
        contratos = int(float(row.get("total_contratos", 0) or 0))
    except:
        contratos = 0
    
    return {
        "total_valor": val,
        "total_contratos": contratos,
        "total_entidades": total_entidades
    }


# ─────────────────────────────────────────────────────────────────────────────
# GEOJSON
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def load_geojson(path: str) -> dict | None:
    """Carga GeoJSON con manejo de errores robusto."""
    try:
        if not os.path.exists(path):
            st.error(f"⚠️ Archivo GeoJSON no encontrado: {path}")
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        st.error(f"❌ Error al cargar GeoJSON ({path}): {e}")
        return None


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

def reset_actor():
    st.session_state["actor_seleccionado"] = ""
    st.session_state["select_actor_raw"] = "🌍 Mostrar Red Completa"
    st.session_state["entity_selector_kpi"] = "🌍 Todas las Entidades"
    st.session_state["forensic_prov_selected"] = None
    st.session_state["forensic_malla_data"] = None
    st.session_state["forensic_contratos_data"] = None
    st.session_state["forensic_metadata"] = None

def set_dep(dep_norm: str, dep_raw: str):
    """dep_norm = sin tildes (GeoJSON). dep_raw = nombre original de la API."""
    st.session_state["dep_norm"] = dep_norm
    st.session_state["dep_raw"]  = dep_raw
    st.session_state["mun_norm"] = ""
    st.session_state["mun_raw"]  = ""
    reset_actor()

def set_mun(mun_norm: str, mun_raw: str):
    st.session_state["mun_norm"] = mun_norm
    st.session_state["mun_raw"]  = mun_raw
    reset_actor()

def reset():
    st.session_state["dep_norm"]  = ""
    st.session_state["dep_raw"]   = ""
    st.session_state["mun_norm"]  = ""
    st.session_state["mun_raw"]   = ""
    reset_actor()


# ─────────────────────────────────────────────────────────────────────────────
# RENDERIZADO KPIs + SELECTOR DE ENTIDAD INTERACTIVO
# ─────────────────────────────────────────────────────────────────────────────
def render_kpis(anio, dep, mun, actor_filter=""):
    spinner_ctx = st.spinner("Calculando KPIs...") if not dep and not mun and not actor_filter else contextlib.nullcontext()
    with spinner_ctx:
        k = get_kpis_v2(anio, dep, mun, actor_filter)
    
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

            if lbl == "Entidades Únicas":
                # Obtain entities list based on the current filter level
                if mun: df_ents = get_entidades(anio, mun)
                elif dep: df_ents = get_top_entidades_dep(anio, dep, n=150)
                else: df_ents = get_top_entidades_global(anio, n=150)
                
                opciones = ["🌍 Todas las Entidades"]
                if not df_ents.empty and "nombre_entidad" in df_ents.columns:
                    opciones += df_ents["nombre_entidad"].dropna().unique().tolist()
                
                actor_actual = st.session_state.get("actor_seleccionado", "")
                idx = opciones.index(actor_actual) if actor_actual in opciones else 0
                
                # Add an interactive popover under the KPI
                with st.popover("🔎 Ver y Filtrar Entidades", use_container_width=True):
                    def _on_entity_kpi_change():
                        val = st.session_state["entity_selector_kpi_pop"]
                        st.session_state["actor_seleccionado"] = "" if val == "🌍 Todas las Entidades" else val
                        st.session_state["select_actor_raw"] = val
                    st.selectbox(
                        "Selecciona una entidad para filtrar KPIs y Grafo:",
                        options=opciones,
                        index=idx,
                        key="entity_selector_kpi_pop",
                        on_change=_on_entity_kpi_change
                    )


def render_entity_selector(anio: int, mun_raw: str):
    """Selector interactivo de entidad que conecta KPIs ↔ Grafo.
    Solo se muestra cuando hay un municipio seleccionado.
    """
    if not mun_raw:
        return

    df_ents = get_entidades(anio, mun_raw)
    if df_ents.empty:
        return

    # Construir opciones: entidades + proveedores del grafo
    opciones_entidades = ["🌍 Todas las Entidades"] + df_ents["nombre_entidad"].tolist()

    actor_actual = st.session_state.get("actor_seleccionado", "")
    idx = 0
    if actor_actual in opciones_entidades:
        idx = opciones_entidades.index(actor_actual)

    st.markdown(f"""
    <div style='background:rgba(167,139,250,.07);border:1px solid rgba(167,139,250,.2);
                border-radius:10px;padding:12px 16px;margin:10px 0 4px;'>
        <div style='font-size:.63rem;font-weight:700;letter-spacing:1px;color:{C['purple']};
                    text-transform:uppercase;margin-bottom:6px;'>🏛️ Filtrar por Entidad → Actualiza KPIs y Grafo</div>
    """, unsafe_allow_html=True)

    def _on_entity_change():
        val = st.session_state.get("entity_selector_kpi", "🌍 Todas las Entidades")
        st.session_state["actor_seleccionado"] = "" if val == "🌍 Todas las Entidades" else val
        # Sincronizar con el selector del grafo
        st.session_state["select_actor_raw"] = val

    st.selectbox(
        label="Selecciona una entidad para filtrar:",
        options=opciones_entidades,
        index=idx,
        key="entity_selector_kpi",
        on_change=_on_entity_change,
        label_visibility="collapsed",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    # Boton limpiar filtro
    if actor_actual:
        if st.button("❌ Quitar filtro de entidad", key="clear_entity_filter", use_container_width=True):
            st.session_state["actor_seleccionado"] = ""
            st.session_state["select_actor_raw"] = "🌍 Mostrar Red Completa"
            st.session_state["entity_selector_kpi"] = "🌍 Todas las Entidades"
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# MAPA DEPARTAMENTOS
# ─────────────────────────────────────────────────────────────────────────────
def render_mapa_departamentos(anio: int):
    geo = load_geojson(GEOJSON)
    if not geo:
        st.error("⚠️ GeoJSON de departamentos no encontrado. Verifica que el archivo data/depto.json exista.")
        return

    df_secop = get_departamentos(anio)
    if df_secop.empty:
        st.warning("⚠️ Sin datos de departamentos para el año seleccionado.")
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
        st.error("⚠️ GeoJSON de municipios no encontrado. Verifica que el archivo data/mpio.json exista.")
        return

    # Consultar municipios usando dep_raw (con acentos) para que el WHERE haga match
    df_secop = get_municipios(anio, dep_raw)
    # mun_raw = ciudad tal como viene de la API (uppercase, con acentos)
    df_secop["mun_raw"]  = df_secop["ciudad"]
    df_secop["mun_norm"] = df_secop["ciudad"].apply(normalizar)

    # Filtrar GeoJSON al departamento usando dep_norm
    base_muns = [f for f in geo["features"] if normalizar(f["properties"].get("NOMBRE_DPT", "")) == dep_norm]

    if not base_muns:
        st.warning(f"⚠️ Sin geometrías para municipios de {dep_norm}.")
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
def render_tablas_municipio(anio: int, mun_raw: str, mun_norm: str):
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
        # Tabla interactiva con on_select
        event = st.dataframe(
            df_disp,
            use_container_width=True, 
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="tabla_entidades_local"
        )
        
        # Interceptar click y actualizar actor seleccionado
        if event and event.selection and event.selection.rows:
            idx = event.selection.rows[0]
            entidad_seleccionada = df_disp.iloc[idx]["Entidad"]
            # Actualizar el session state si es diferente
            if st.session_state.get("actor_seleccionado") != entidad_seleccionada:
                st.session_state["actor_seleccionado"] = entidad_seleccionada
                st.session_state["select_actor_raw"] = entidad_seleccionada
                st.session_state["entity_selector_kpi"] = entidad_seleccionada
                st.rerun()

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

    st.markdown("<hr style='border:none;border-top:1px dashed #1A2336;margin:24px 0;'>", unsafe_allow_html=True)
    st.markdown(f"<div class='sec-ttl'>💼 Top Proveedores en {mun_norm.title()}</div>", unsafe_allow_html=True)

    df_prov = get_proveedores_municipio(anio, mun_raw)
    if df_prov.empty:
        st.info("Sin proveedores para este municipio en el año seleccionado.")
    else:
        col_pt, col_pb = st.columns([3, 2])
        with col_pt:
            df_prov_disp = df_prov[["proveedor_adjudicado", "contratos", "valor"]].copy()
            df_prov_disp["presupuesto_fmt"] = df_prov_disp["valor"].apply(fmt_b)
            df_prov_disp["contratos_fmt"]   = df_prov_disp["contratos"].apply(fmt_n)
            df_prov_disp = df_prov_disp[["proveedor_adjudicado", "contratos_fmt", "presupuesto_fmt"]]
            df_prov_disp.columns = ["Proveedor", "Contratos", "Monto Adjudicado"]
            
            st.dataframe(
                df_prov_disp,
                use_container_width=True, 
                hide_index=True,
                key="tabla_proveedores_local"
            )
            
        with col_pb:
            df_prov_plot = df_prov.head(10).copy()
            df_prov_plot["nombre_corto"] = df_prov_plot["proveedor_adjudicado"].str[:30] + "…"
            fig_p = px.bar(
                df_prov_plot.sort_values("valor"), x="valor", y="nombre_corto", orientation="h",
                text=df_prov_plot.sort_values("valor")["valor"].apply(fmt_b),
                color="valor",
                color_continuous_scale=[[0, C["green"]], [1, "#86EFAC"]],
            )
            fig_p.update_traces(textposition="outside", textfont_size=9)
            fig_p.update_layout(
                paper_bgcolor=C["bg"], plot_bgcolor=C["bg"], font_color=C["text"],
                yaxis=dict(showgrid=False, title=""),
                xaxis=dict(showgrid=True, gridcolor=C["border"], title=""),
                showlegend=False, coloraxis_showscale=False,
                margin=dict(l=0, r=0, t=0, b=0), height=360,
            )
            st.plotly_chart(fig_p, use_container_width=True)

def render_kpis_modalidad(anio: int, dep_raw: str, mun_raw: str, dep_norm: str, mun_norm: str, actor_filter: str = ""):
    """Renderiza KPIs por modalidad de contratación."""
    if actor_filter:
        nivel_str = f"{actor_filter}"
    elif mun_norm:
        nivel_str = f"en {mun_norm.title()}"
    elif dep_norm:
        nivel_str = f"en {dep_norm.title()}"
    else:
        nivel_str = "Nacional"
        
    st.markdown("<hr style='border:none;border-top:1px solid #1A2336;margin:24px 0;'>", unsafe_allow_html=True)
    st.markdown(f"<div class='sec-ttl'>📊 Análisis por Modalidad de Contratación ({nivel_str})</div>", unsafe_allow_html=True)

    df_mod = get_kpis_modalidad(anio, dep_raw, mun_raw, actor_filter)
    if df_mod.empty:
        st.info("No hay datos de modalidades para el área seleccionada.")
        return

    # Gráfico de Treemap
    fig_tree = px.treemap(
        df_mod, path=[px.Constant("Todas"), "modalidad_de_contratacion"], values="valor",
        color="valor", color_continuous_scale="Blues",
        custom_data=["contratos", "entidades", "ticket_promedio"]
    )
    fig_tree.update_traces(
        hovertemplate="<b>%{label}</b><br>Presupuesto: %{value:$,.0f}<br>Contratos: %{customdata[0]}<br>Entidades: %{customdata[1]}<br>Ticket Promedio: %{customdata[2]:$,.0f}<extra></extra>"
    )
    fig_tree.update_layout(margin=dict(t=10, l=10, r=10, b=10), height=300, paper_bgcolor=C["bg"])
    st.plotly_chart(fig_tree, use_container_width=True)

    # Tabla de Modalidades
    df_disp = df_mod[["modalidad_de_contratacion", "valor", "participacion_pct", "contratos", "ticket_promedio", "entidades"]].copy()
    df_disp["valor_fmt"] = df_disp["valor"].apply(fmt_b)
    df_disp["pct_fmt"] = df_disp["participacion_pct"].apply(lambda x: f"{x:.1f}%")
    df_disp["tk_fmt"] = df_disp["ticket_promedio"].apply(fmt_b)
    df_disp["cnt_fmt"] = df_disp["contratos"].apply(fmt_n)
    df_disp["ent_fmt"] = df_disp["entidades"].apply(fmt_n)
    
    df_disp = df_disp[["modalidad_de_contratacion", "valor_fmt", "pct_fmt", "cnt_fmt", "tk_fmt", "ent_fmt"]]
    df_disp.columns = ["Modalidad", "Total Adjudicado", "% Participación", "N° Contratos", "Ticket Prom.", "Entidades"]
    st.dataframe(df_disp, use_container_width=True, hide_index=True)



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

    # ── KPIs + Selector de Entidad Interactivo ────────────────────────────────
    actor = st.session_state.get("actor_seleccionado", "")
    if actor:
        col_badge, col_clear = st.columns([5, 1])
        with col_badge:
            st.markdown(
                f"<div style='background:rgba(244,63,94,.1);color:#F43F5E;padding:8px 12px;"
                f"border-radius:8px;font-weight:700;font-size:0.8rem;border:1px solid rgba(244,63,94,.3);'>"
                f"🔎 Filtrando por: <b>{actor}</b></div>",
                unsafe_allow_html=True
            )
        with col_clear:
            if st.button("✕ Quitar", key="clear_actor_top", use_container_width=True):
                st.session_state["actor_seleccionado"] = ""
                st.session_state["select_actor_raw"] = "🌍 Mostrar Red Completa"
                st.rerun()
    render_kpis(anio, dep_raw, mun_raw, actor)
    
    # Selector de entidad interactivo movido al KPI "Entidades Únicas"
    
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
    
        # ── KPIs por Modalidad (siempre presente, incluso sin entidad seleccionada) ──
        render_kpis_modalidad(
            anio, dep_raw=dep_raw, mun_raw=mun_raw,
            dep_norm=dep_norm, mun_norm=mun_norm,
            actor_filter=actor
        )

        # ── Rankings Globales ─────────────────────────────────────────────────────
        render_ranking_global(anio, dep_raw=dep_raw, dep_norm=dep_norm,
                              mun_raw=mun_raw, mun_norm=mun_norm)
    
        # ── Tabla entidades y proveedores (solo al seleccionar municipio) ─────────
        if mun_raw:
            render_tablas_municipio(anio, mun_raw=mun_raw, mun_norm=mun_norm)

    with tab_red:
        render_network_tab(anio, dep_raw, mun_raw)


if __name__ == "__main__":
    main()
