#!/usr/bin/env python3
"""
test_performance.py
===================

Script para medir y comparar rendimiento antes/después de optimizaciones.

Uso:
    python test_performance.py --baseline    # Medir estado actual
    python test_performance.py --optimized   # Medir después de optimizaciones
    python test_performance.py --compare     # Comparar ambos
"""

import time
import json
import os
import sys
import argparse
from datetime import datetime
from typing import Dict, List, Tuple
import pandas as pd

# Importar funciones de la app
try:
    from app import (
        get_anios, get_departamentos, get_municipios, 
        get_entidades, get_kpis, get_top_entidades_global
    )
    from network_analysis import get_network_raw_data, process_metrics_and_risk
except ImportError as e:
    print(f"Error importando módulos: {e}")
    print("Asegúrate de estar en el directorio correcto y que app.py existe")
    sys.exit(1)


class PerformanceTester:
    """Clase para medir y registrar rendimiento."""
    
    def __init__(self, results_dir: str = ".performance"):
        self.results_dir = results_dir
        os.makedirs(results_dir, exist_ok=True)
        self.results: Dict[str, List[float]] = {}
    
    def measure(self, func_name: str, func, *args, **kwargs) -> Tuple[float, any]:
        """Mide el tiempo de ejecución de una función."""
        t0 = time.time()
        try:
            result = func(*args, **kwargs)
            elapsed = time.time() - t0
            return elapsed, result
        except Exception as e:
            elapsed = time.time() - t0
            print(f"❌ Error en {func_name}: {e}")
            return elapsed, None
    
    def record(self, test_name: str, elapsed: float, status: str = "✅"):
        """Registra un resultado."""
        if test_name not in self.results:
            self.results[test_name] = []
        self.results[test_name].append(elapsed)
        print(f"{status} {test_name}: {elapsed:.3f}s")
    
    def save_results(self, filename: str = "results.json"):
        """Guarda resultados en JSON."""
        filepath = os.path.join(self.results_dir, filename)
        
        # Calcular estadísticas
        stats = {}
        for test_name, times in self.results.items():
            stats[test_name] = {
                "count": len(times),
                "min": min(times),
                "max": max(times),
                "avg": sum(times) / len(times),
                "total": sum(times),
            }
        
        data = {
            "timestamp": datetime.now().isoformat(),
            "results": self.results,
            "stats": stats,
        }
        
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        
        print(f"\n📊 Resultados guardados en: {filepath}")
        return filepath
    
    def print_summary(self):
        """Imprime resumen de resultados."""
        print("\n" + "="*60)
        print("  RESUMEN DE RENDIMIENTO")
        print("="*60)
        
        for test_name, times in self.results.items():
            avg = sum(times) / len(times)
            min_t = min(times)
            max_t = max(times)
            print(f"\n{test_name}:")
            print(f"  Promedio: {avg:.3f}s")
            print(f"  Mínimo:   {min_t:.3f}s")
            print(f"  Máximo:   {max_t:.3f}s")
            print(f"  Llamadas: {len(times)}")


def test_baseline(tester: PerformanceTester):
    """Pruebas de rendimiento ANTES de optimizaciones."""
    print("\n" + "="*60)
    print("  PRUEBAS DE RENDIMIENTO — ESTADO ACTUAL")
    print("="*60)
    
    # Test 1: Obtener años
    print("\n[1/7] Obteniendo años disponibles...")
    elapsed, anios = tester.measure("get_anios", get_anios)
    tester.record("get_anios", elapsed)
    
    if not anios:
        print("❌ No se obtuvieron años. Abortando pruebas.")
        return
    
    anio = anios[0]
    print(f"    Años disponibles: {anios}")
    
    # Test 2: Obtener departamentos (3 veces)
    print("\n[2/7] Obteniendo departamentos (3 llamadas)...")
    for i in range(3):
        elapsed, df = tester.measure(
            f"get_departamentos (llamada {i+1})",
            get_departamentos,
            anio
        )
        tester.record(f"get_departamentos", elapsed)
        
        if df is not None and not df.empty:
            print(f"    Departamentos obtenidos: {len(df)}")
            dep_raw = df.iloc[0]["departamento"]
        else:
            print("    ❌ No se obtuvieron departamentos")
            return
    
    # Test 3: Obtener municipios
    print("\n[3/7] Obteniendo municipios...")
    elapsed, df = tester.measure(
        "get_municipios",
        get_municipios,
        anio,
        dep_raw
    )
    tester.record("get_municipios", elapsed)
    
    if df is not None and not df.empty:
        print(f"    Municipios obtenidos: {len(df)}")
        mun_raw = df.iloc[0]["ciudad"]
    else:
        print("    ❌ No se obtuvieron municipios")
        return
    
    # Test 4: Obtener entidades
    print("\n[4/7] Obteniendo entidades...")
    elapsed, df = tester.measure(
        "get_entidades",
        get_entidades,
        anio,
        mun_raw
    )
    tester.record("get_entidades", elapsed)
    
    if df is not None:
        print(f"    Entidades obtenidas: {len(df)}")
    
    # Test 5: Calcular KPIs (global)
    print("\n[5/7] Calculando KPIs (global)...")
    elapsed, kpis = tester.measure(
        "get_kpis (global)",
        get_kpis,
        anio,
        "",
        ""
    )
    tester.record("get_kpis (global)", elapsed)
    
    if kpis:
        print(f"    KPIs: {kpis}")
    
    # Test 6: Calcular KPIs (por municipio)
    print("\n[6/7] Calculando KPIs (por municipio)...")
    elapsed, kpis = tester.measure(
        "get_kpis (municipio)",
        get_kpis,
        anio,
        dep_raw,
        mun_raw
    )
    tester.record("get_kpis (municipio)", elapsed)
    
    if kpis:
        print(f"    KPIs: {kpis}")
    
    # Test 7: Obtener datos de red
    print("\n[7/7] Obteniendo datos de red...")
    elapsed, df = tester.measure(
        "get_network_raw_data",
        get_network_raw_data,
        anio,
        dep_raw,
        mun_raw
    )
    tester.record("get_network_raw_data", elapsed)
    
    if df is not None and not df.empty:
        print(f"    Registros de red: {len(df)}")
        
        # Procesar métricas
        print("    Procesando métricas de riesgo...")
        elapsed, result = tester.measure(
            "process_metrics_and_risk",
            process_metrics_and_risk,
            df
        )
        tester.record("process_metrics_and_risk", elapsed)
        
        if result:
            edges_df, prov_df, ent_df = result
            print(f"    Proveedores: {len(prov_df)}, Entidades: {len(ent_df)}")


def test_optimized(tester: PerformanceTester):
    """Pruebas de rendimiento DESPUÉS de optimizaciones."""
    print("\n" + "="*60)
    print("  PRUEBAS DE RENDIMIENTO — DESPUÉS DE OPTIMIZACIONES")
    print("="*60)
    
    try:
        from OPTIMIZACIONES_INMEDIATAS import (
            soql_get_optimized,
            get_departamentos_y_municipios_batch,
            get_kpis_from_parquet,
            calcular_riesgo_vectorizado,
        )
        from app import API_BASE, API_TIMEOUT
    except ImportError as e:
        print(f"❌ Error importando optimizaciones: {e}")
        print("Asegúrate de que OPTIMIZACIONES_INMEDIATAS.py existe")
        return
    
    # Test 1: Obtener años
    print("\n[1/5] Obteniendo años disponibles...")
    elapsed, anios = tester.measure("get_anios (optimizado)", get_anios)
    tester.record("get_anios (optimizado)", elapsed)
    
    if not anios:
        print("❌ No se obtuvieron años. Abortando pruebas.")
        return
    
    anio = anios[0]
    
    # Test 2: Batch queries (departamentos + municipios)
    print("\n[2/5] Obteniendo departamentos y municipios (batch)...")
    elapsed, result = tester.measure(
        "get_departamentos_y_municipios_batch",
        get_departamentos_y_municipios_batch,
        anio,
        API_BASE,
        API_TIMEOUT
    )
    tester.record("get_departamentos_y_municipios_batch", elapsed)
    
    if result:
        df_deps, muns_por_dep = result
        if not df_deps.empty:
            print(f"    Departamentos: {len(df_deps)}")
            dep_raw = df_deps.iloc[0]["departamento"]
        else:
            print("    ❌ No se obtuvieron departamentos")
            return
    
    # Test 3: KPIs desde Parquet
    print("\n[3/5] Calculando KPIs desde Parquet...")
    elapsed, kpis = tester.measure(
        "get_kpis_from_parquet (global)",
        get_kpis_from_parquet,
        anio,
        "",
        "",
        "data/secop.parquet"
    )
    tester.record("get_kpis_from_parquet (global)", elapsed)
    
    if kpis:
        print(f"    KPIs: {kpis}")
    
    # Test 4: KPIs desde Parquet (por municipio)
    print("\n[4/5] Calculando KPIs desde Parquet (por municipio)...")
    
    # Obtener un municipio
    if dep_raw in muns_por_dep and not muns_por_dep[dep_raw].empty:
        mun_raw = muns_por_dep[dep_raw].iloc[0]["ciudad"]
        
        elapsed, kpis = tester.measure(
            "get_kpis_from_parquet (municipio)",
            get_kpis_from_parquet,
            anio,
            dep_raw,
            mun_raw,
            "data/secop.parquet"
        )
        tester.record("get_kpis_from_parquet (municipio)", elapsed)
        
        if kpis:
            print(f"    KPIs: {kpis}")
    
    # Test 5: Vectorizar cálculos de riesgo
    print("\n[5/5] Calculando riesgos vectorizados...")
    
    # Obtener datos de red
    elapsed, df_raw = tester.measure(
        "get_network_raw_data (optimizado)",
        get_network_raw_data,
        anio,
        dep_raw,
        ""
    )
    
    if df_raw is not None and not df_raw.empty:
        elapsed, result = tester.measure(
            "calcular_riesgo_vectorizado",
            calcular_riesgo_vectorizado,
            df_raw
        )
        tester.record("calcular_riesgo_vectorizado", elapsed)
        
        if result is not None:
            print(f"    Proveedores procesados: {len(result)}")


def compare_results(baseline_file: str, optimized_file: str):
    """Compara resultados de baseline vs optimizado."""
    print("\n" + "="*60)
    print("  COMPARACIÓN DE RENDIMIENTO")
    print("="*60)
    
    try:
        with open(baseline_file) as f:
            baseline = json.load(f)
        with open(optimized_file) as f:
            optimized = json.load(f)
    except FileNotFoundError as e:
        print(f"❌ Archivo no encontrado: {e}")
        return
    
    baseline_stats = baseline.get("stats", {})
    optimized_stats = optimized.get("stats", {})
    
    print("\n" + "-"*60)
    print(f"{'Test':<40} {'Antes':<12} {'Después':<12} {'Mejora':<10}")
    print("-"*60)
    
    total_improvement = 0
    count = 0
    
    for test_name in baseline_stats:
        if test_name in optimized_stats:
            before = baseline_stats[test_name]["avg"]
            after = optimized_stats[test_name]["avg"]
            improvement = before / after if after > 0 else 0
            
            print(f"{test_name:<40} {before:<12.3f}s {after:<12.3f}s {improvement:<10.1f}x")
            
            total_improvement += improvement
            count += 1
    
    if count > 0:
        avg_improvement = total_improvement / count
        print("-"*60)
        print(f"{'PROMEDIO':<40} {'':<12} {'':<12} {avg_improvement:<10.1f}x")
        print("="*60)
        
        if avg_improvement >= 5:
            print("✅ EXCELENTE: Mejora de 5x o más")
        elif avg_improvement >= 3:
            print("✅ BUENO: Mejora de 3x o más")
        elif avg_improvement >= 2:
            print("⚠️  ACEPTABLE: Mejora de 2x o más")
        else:
            print("❌ INSUFICIENTE: Mejora menor a 2x")


def main():
    parser = argparse.ArgumentParser(
        description="Medir y comparar rendimiento del dashboard"
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Medir rendimiento actual (antes de optimizaciones)"
    )
    parser.add_argument(
        "--optimized",
        action="store_true",
        help="Medir rendimiento después de optimizaciones"
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Comparar resultados de baseline vs optimizado"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Ejecutar todas las pruebas (baseline + optimized + compare)"
    )
    
    args = parser.parse_args()
    
    # Si no se especifica nada, mostrar ayuda
    if not any([args.baseline, args.optimized, args.compare, args.all]):
        parser.print_help()
        return
    
    tester = PerformanceTester()
    
    if args.baseline or args.all:
        test_baseline(tester)
        baseline_file = tester.save_results("baseline.json")
        tester.print_summary()
    
    if args.optimized or args.all:
        tester = PerformanceTester()  # Reset
        test_optimized(tester)
        optimized_file = tester.save_results("optimized.json")
        tester.print_summary()
    
    if args.compare or args.all:
        baseline_file = os.path.join(".performance", "baseline.json")
        optimized_file = os.path.join(".performance", "optimized.json")
        
        if os.path.exists(baseline_file) and os.path.exists(optimized_file):
            compare_results(baseline_file, optimized_file)
        else:
            print("❌ Archivos de resultados no encontrados")
            print("Ejecuta primero: python test_performance.py --all")


if __name__ == "__main__":
    main()

