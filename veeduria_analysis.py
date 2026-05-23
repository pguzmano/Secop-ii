import streamlit as st
import pandas as pd
import plotly.express as px
import json

@st.cache_data(ttl=3600, show_spinner=False)
def get_local_data():
    # ETL Local: Usar el archivo parquet pre-procesado en lugar de la API en vivo
    # Esto evita el ReadTimeout (Error 504) de Socrata al agrupar millones de procesos.
    try:
        df = pd.read_parquet("data/secop_veeduria.parquet")
        # Asegurarnos de tener las columnas necesarias casteadas
        df['numero_de_oferentes'] = pd.to_numeric(df['numero_de_oferentes'], errors='coerce').fillna(0)
        df['valor_del_contrato'] = pd.to_numeric(df['valor_del_contrato'], errors='coerce').fillna(0)
        
        # Filtro de cordura (Sanity check): Eliminar errores de digitación absurdos de SECOP
        # Contratos > 10 billones de pesos (1e13) suelen ser errores tipográficos
        df = df[df['valor_del_contrato'] < 1e13]
        
        df['anio'] = pd.to_numeric(df['anio'], errors='coerce').fillna(0).astype(int)
        # Limpiar strings
        for col in ['departamento_entidad', 'modalidad_de_contratacion', 'proveedor_adjudicado', 'nombre_entidad']:
            if col in df.columns:
                df[col] = df[col].fillna("No Definido")
                
        # Crear la marca de baja competencia basada en la lógica forense
        # Baja competencia = 1 oferente o Contratación directa
        df['es_baja_comp'] = (df['numero_de_oferentes'] <= 1) | (df['modalidad_de_contratacion'] == 'Contratación directa')
        return df
    except Exception as e:
        st.error(f"Error cargando parquet local: {e}")
        return pd.DataFrame()

def render_veeduria_tab(anio_seleccionado, soql_func, normalizar_func, geo_dep_path):
    C = dict(
        bg="#060B14", card="#0D1421", border="#1A2336",
        blue="#4F8EF7", green="#22C55E", amber="#F59E0B", red="#F43F5E",
        purple="#A78BFA", text="#F1F5F9", muted="#64748B",
    )
    
    st.markdown("""
    <div style='background:rgba(244, 63, 94, 0.1); border-left: 4px solid #F43F5E; padding:15px; border-radius:4px; margin-bottom: 20px;'>
    <strong>Mensaje Central:</strong> "La contratación directa y los procesos de oferente único concentran la mayor parte del presupuesto público adjudicado, aislando la competencia y consolidando contratistas específicos como los principales receptores de fondos."
    </div>
    """, unsafe_allow_html=True)

    if "veeduria_dep" not in st.session_state:
        st.session_state["veeduria_dep"] = None
    if "veeduria_dep_raw" not in st.session_state:
        st.session_state["veeduria_dep_raw"] = None

    # Cargar datos por ETL Local
    with st.spinner("Cargando ETL Local..."):
        df_base = get_local_data()
    
    if df_base.empty:
        st.warning("⚠️ No se encontró el archivo de datos local (ETL).")
        return
        
    # Filtrar por año global
    df_anio = df_base[df_base['anio'] == anio_seleccionado]

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("<div class='panel'>", unsafe_allow_html=True)
        st.subheader(f"📍 1. Situación (Presupuesto Total en {anio_seleccionado})")
        st.write("Haz clic en un departamento para filtrar el resto de los análisis.")
        
        # Agrupar mapa en Pandas
        df_map = df_anio.groupby('departamento_entidad')['valor_del_contrato'].sum().reset_index()
        df_map.rename(columns={'departamento_entidad': 'departamento', 'valor_del_contrato': 'total_valor'}, inplace=True)
        df_map['valor_billones'] = df_map['total_valor'] / 1e12  # Para la escala de color
        df_map['texto_etiqueta'] = df_map['total_valor'].apply(lambda x: f"${x:,.0f}")
        df_map['departamento'] = df_map['departamento'].str.upper()
        
        if not df_map.empty:
            df_map['departamento_raw'] = df_map['departamento']
            df_map['departamento'] = df_map['departamento'].replace({"BOGOTA D.C.": "BOGOTÁ", "BOGOTA, D.C.": "BOGOTÁ"})
            df_map["dep_norm"] = df_map["departamento"].apply(normalizar_func)
            
            with open(geo_dep_path, encoding="utf-8") as f: geo_dep = json.load(f)
            
            fig_map = px.choropleth_mapbox(
                df_map, geojson=geo_dep, locations="dep_norm", featureidkey="properties.NOMBRE_DPT",
                color="valor_billones", mapbox_style="carto-darkmatter", center={"lat": 4.57, "lon": -74.3}, zoom=4,
                color_continuous_scale="Reds", custom_data=["departamento_raw", "dep_norm", "texto_etiqueta"]
            )
            fig_map.update_traces(hovertemplate="<b>%{location}</b><br>Presupuesto: %{customdata[2]}<extra></extra>")
            fig_map.update_layout(margin={"r":0,"t":0,"l":0,"b":0}, paper_bgcolor="rgba(0,0,0,0)", font_color="#fff", coloraxis_colorbar_title="Billones")
            
            event = st.plotly_chart(fig_map, use_container_width=True, on_select="rerun", selection_mode="points", key="map_veeduria")
            pts = (event or {}).get("selection", {}).get("points", [])
            
            if pts:
                cd = pts[0].get("customdata", [])
                if len(cd) >= 2:
                    selected_raw = cd[0]
                    selected_norm = cd[1]
                    if st.session_state["veeduria_dep_raw"] != selected_raw:
                        st.session_state["veeduria_dep_raw"] = selected_raw
                        st.session_state["veeduria_dep"] = selected_norm
                        st.rerun()
            else:
                if st.session_state["veeduria_dep_raw"] is not None:
                    st.session_state["veeduria_dep_raw"] = None
                    st.session_state["veeduria_dep"] = None
                    st.rerun()
                    
        st.markdown("</div>", unsafe_allow_html=True)

    # Filtrar territorialmente para el resto de gráficos si hay selección
    if st.session_state["veeduria_dep_raw"]:
        dep_filter = st.session_state["veeduria_dep_raw"].upper()
        df_filtered = df_anio[df_anio['departamento_entidad'].str.upper() == dep_filter]
    else:
        df_filtered = df_anio

    with col2:
        st.markdown("<div class='panel'>", unsafe_allow_html=True)
        ttl_dep = f" en {st.session_state['veeduria_dep'].title()}" if st.session_state["veeduria_dep"] else " Nacional"
        st.subheader(f"⚠️ 2. Complicación (Competencia por Modalidad{ttl_dep})")
        st.write("¿Qué modalidades de contratación presentan menor cantidad de oferentes?")
        
        # Pandas Groupby para Modalidades (Baja vs Alta)
        df_baja = df_filtered[df_filtered['es_baja_comp']].groupby('modalidad_de_contratacion')['valor_del_contrato'].sum()
        df_alta = df_filtered[~df_filtered['es_baja_comp']].groupby('modalidad_de_contratacion')['valor_del_contrato'].sum()
        
        df_mod = pd.DataFrame({'Baja (≤1)': df_baja, 'Alta (>1)': df_alta}).fillna(0).reset_index()
        df_mod.rename(columns={'modalidad_de_contratacion': 'modalidad'}, inplace=True)
        df_mod['total'] = df_mod['Baja (≤1)'] + df_mod['Alta (>1)']
        df_mod = df_mod[df_mod['total'] > 0].sort_values("total", ascending=True).tail(10) # Top 10 modalidades
        
        if not df_mod.empty:
            df_melt = pd.melt(df_mod, id_vars=['modalidad'], value_vars=['Baja (≤1)', 'Alta (>1)'], 
                              var_name='Nivel', value_name='Valor')
            
            fig_mod = px.bar(df_melt, y='modalidad', x='Valor', color='Nivel', orientation='h', barmode='relative',
                             color_discrete_map={'Baja (≤1)': C['red'], 'Alta (>1)': C['blue']})
            fig_mod.update_layout(barnorm='percent', xaxis_title="% Presupuesto Adjudicado", yaxis_title="", 
                                  paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#fff", margin={"t":0})
            st.plotly_chart(fig_mod, use_container_width=True, key="bar_mod")
        st.markdown("</div>", unsafe_allow_html=True)

    col3, col4 = st.columns(2)

    with col3:
        st.markdown("<div class='panel'>", unsafe_allow_html=True)
        st.subheader(f"🎯 3. Resolución (Proveedores en Baja Competencia{ttl_dep})")
        st.write("¿Quiénes absorben mayor presupuesto sin competir?")
        
        # Filtramos solo la porción de baja competencia
        df_baja_comp = df_filtered[df_filtered['es_baja_comp'] & (df_filtered['proveedor_adjudicado'] != 'No Definido')]
        df_prov = df_baja_comp.groupby('proveedor_adjudicado')['valor_del_contrato'].sum().reset_index()
        df_prov['valor_billones'] = df_prov['valor_del_contrato'] / 1e12
        df_prov['texto_etiqueta'] = df_prov['valor_del_contrato'].apply(lambda x: f"${x:,.0f}")
        df_prov = df_prov.sort_values('valor_del_contrato', ascending=False).head(30)
        
        if not df_prov.empty:
            fig_prov = px.treemap(df_prov, path=[px.Constant("Proveedores"), 'proveedor_adjudicado'], values='valor_del_contrato',
                                  color='valor_billones', color_continuous_scale="Reds", custom_data=['texto_etiqueta'])
            fig_prov.update_layout(margin={"r":0,"t":0,"l":0,"b":0}, paper_bgcolor="rgba(0,0,0,0)", coloraxis_colorbar_title="Billones")
            fig_prov.update_traces(textinfo="none", texttemplate="%{label}<br>%{customdata[0]}", hovertemplate='<b>%{label}</b><br>Valor: %{customdata[0]}<extra></extra>')
            st.plotly_chart(fig_prov, use_container_width=True, key="tree_prov")
        else:
            st.info("No hay suficientes datos para este territorio.")
        st.markdown("</div>", unsafe_allow_html=True)

    with col4:
        st.markdown("<div class='panel'>", unsafe_allow_html=True)
        st.subheader(f"🏛️ 4. Acción (Entidades Prioritarias{ttl_dep})")
        st.write("¿Cuáles son las entidades con mayor monto adjudicado bajo alertas?")
        
        # Entidades con baja competencia
        df_ent = df_baja_comp.groupby('nombre_entidad')['valor_del_contrato'].sum().reset_index()
        df_ent['valor_billones'] = df_ent['valor_del_contrato'] / 1e12
        df_ent['texto_etiqueta'] = df_ent['valor_del_contrato'].apply(lambda x: f"${x:,.0f}")
        df_ent = df_ent.sort_values('valor_del_contrato', ascending=False).head(15)
        df_ent = df_ent.sort_values('valor_del_contrato', ascending=True) # Para que en barras horizontales queden arriba
        
        if not df_ent.empty:
            fig_ent = px.bar(df_ent, y='nombre_entidad', x='valor_billones', orientation='h', text='texto_etiqueta', custom_data=['valor_del_contrato'])
            fig_ent.update_traces(marker_color=C['amber'], textposition='outside', hovertemplate='<b>%{y}</b><br>Valor: %{text}<extra></extra>')
            fig_ent.update_layout(xaxis_title="Valor en Riesgo (Billones COP)", yaxis_title="", 
                                  paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#fff", margin={"t":0})
            st.plotly_chart(fig_ent, use_container_width=True, key="bar_ent")
        else:
            st.info("No hay suficientes datos para este territorio.")
        st.markdown("</div>", unsafe_allow_html=True)
