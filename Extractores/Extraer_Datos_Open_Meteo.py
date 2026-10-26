"""
Descarga datos horarios de Open-Meteo para las estaciones SINCA de la Región Metropolitana.

Fuentes (cada una se guarda en Datos/Open_Meteo/<Carpeta>/<Estacion>.csv):
  - historical_forecast: Historical Forecast API. Meteorología de superficie y
    niveles de presión 1000/925/850/700 hPa, modelo best_match. Desde 2022.
  - previous_runs: Previous Runs API. Las variables de superficie tal como se
    pronosticaron con 1 a 7 días de anticipación (columnas <variable>_previous_dayN).
    Esta API no ofrece niveles de presión, y con best_match no entrega la altura
    de capa límite. Desde 2024.
  - air_quality: Air Quality API. MP2.5 y MP10 del dominio CAMS global. Desde agosto de 2022.

Los archivos usan la hora UTC (columna fecha_hora_utc), coma como separador y
punto decimal. El script es incremental: si el archivo ya existe, vuelve a
descargar desde (último dato - DIAS_REVISION) hasta hoy; si no, descarga desde
el inicio de la fuente.

Uso responsable de la API gratuita (términos de uso de open-meteo.com):
  - Solo uso no comercial. Los datos son CC BY 4.0: hay que citar a Open-Meteo.
  - Límites: 600 llamadas/minuto, 5.000/hora, 10.000/día y 300.000/mes. Una
    petición con más de 10 variables o más de 2 semanas cuenta como varias
    llamadas (fraccionales). El script estima ese peso, espera SEGUNDOS_POR_LLAMADA
    por cada llamada que consume y se detiene al alcanzar --presupuesto llamadas
    en el día (UTC). El consumo del día queda en Datos/Open_Meteo/uso_api.json,
    así que basta con volver a ejecutarlo otro día para continuar.
  - Los robots.txt de los subdominios de la API prohíben el rastreo a buscadores
    (Disallow: /). Este script no rastrea: hace peticiones puntuales a la API,
    que es el uso documentado.

Uso:
    python3 Extractores/Extraer_Datos_Open_Meteo.py                          # todo
    python3 Extractores/Extraer_Datos_Open_Meteo.py --fuente air_quality
    python3 Extractores/Extraer_Datos_Open_Meteo.py --estacion Parque_OHiggins --completo
"""

import argparse
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CARPETA_DATOS = Path(__file__).resolve().parent.parent / "Datos" / "Open_Meteo"
ARCHIVO_USO = CARPETA_DATOS / "uso_api.json"

DIAS_REVISION = 7
PRESUPUESTO_DIARIO = 8000      # llamadas por día; deja margen bajo el límite de 10.000
SEGUNDOS_POR_LLAMADA = 1.0     # pausa por llamada consumida: <= 3.600/hora y 60/minuto
PESO_MAX_PETICION = 60         # llamadas máximas que consume una sola petición

# Coordenadas publicadas en la página de cada estación del SINCA.
# Los nombres coinciden con los CSV de Datos/Datos_Historicos.
ESTACIONES = {
    "Cerrillos": (-33.4958, -70.7043),
    "Cerrillos_I": (-33.4928, -70.7194),
    "Cerrillos_II": (-33.4946, -70.6933),
    "Cerro_Navia": (-33.4331, -70.7321),
    "El_Bosque": (-33.5499, -70.6765),
    "Independencia": (-33.4223, -70.6512),
    "La_Florida": (-33.5166, -70.5881),
    "Las_Condes": (-33.3768, -70.5233),
    "Parque_OHiggins": (-33.4641, -70.6608),
    "Pudahuel": (-33.4378, -70.7502),
    "Puente_Alto": (-33.5914, -70.5948),
    "Quilicura": (-33.3496, -70.7238),
    "Quilicura_I": (-33.3658, -70.7483),
    "Talagante": (-33.6738, -70.9531),
}

SUPERFICIE = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation",
    "surface_pressure", "pressure_msl", "cloud_cover", "wind_speed_10m",
    "wind_direction_10m", "wind_gusts_10m", "shortwave_radiation",
]
NIVELES_PRESION = [1000, 925, 850, 700]
VARIABLES_NIVEL = ["temperature", "relative_humidity", "wind_speed", "wind_direction", "geopotential_height"]

FUENTES = {
    "historical_forecast": {
        "url": "https://historical-forecast-api.open-meteo.com/v1/forecast",
        "carpeta": "Historical_Forecast",
        "inicio": date(2022, 1, 1),
        "variables": SUPERFICIE + ["boundary_layer_height"]
                     + [f"{v}_{n}hPa" for n in NIVELES_PRESION for v in VARIABLES_NIVEL],
        "parametros": {"models": "best_match", "wind_speed_unit": "ms"},
    },
    "previous_runs": {
        "url": "https://previous-runs-api.open-meteo.com/v1/forecast",
        "carpeta": "Previous_Runs",
        "inicio": date(2024, 1, 1),
        "variables": [f"{v}_previous_day{d}" for v in SUPERFICIE for d in range(1, 8)],
        "parametros": {"models": "best_match", "wind_speed_unit": "ms"},
    },
    "air_quality": {
        "url": "https://air-quality-api.open-meteo.com/v1/air-quality",
        "carpeta": "Air_Quality",
        "inicio": date(2022, 8, 1),
        "variables": ["pm2_5", "pm10"],
        "parametros": {"domains": "cams_global"},
    },
}
COLUMNA_FECHA = "fecha_hora_utc"


class PresupuestoAgotado(Exception):
    pass


def hoy_utc() -> date:
    return datetime.now(timezone.utc).date()


def leer_uso() -> float:
    try:
        uso = json.loads(ARCHIVO_USO.read_text())
    except (OSError, ValueError):
        return 0.0
    return uso["llamadas"] if uso.get("fecha") == hoy_utc().isoformat() else 0.0


def registrar_uso(llamadas: float) -> None:
    ARCHIVO_USO.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVO_USO.write_text(json.dumps({"fecha": hoy_utc().isoformat(), "llamadas": round(llamadas, 2)}))


def peso(n_variables: int, dias: int) -> float:
    """Llamadas que Open-Meteo cobra por una petición (conteo fraccional)."""
    return max(1.0, n_variables / 10) * max(1.0, dias / 14)


def dias_por_bloque(n_variables: int) -> int:
    """Largo de cada petición para que no consuma más de PESO_MAX_PETICION llamadas."""
    return 14 * max(1, int(PESO_MAX_PETICION / max(1.0, n_variables / 10)))


def pedir(url: str, presupuesto: float, costo: float, intentos: int = 4) -> dict:
    usadas = leer_uso()
    if usadas + costo > presupuesto:
        raise PresupuestoAgotado(f"{usadas:.0f} llamadas usadas hoy (presupuesto {presupuesto:.0f})")
    for intento in range(1, intentos + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ProCa_Air (uso academico)"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                datos = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            motivo = e.read().decode(errors="replace")
            if e.code == 429 and "inute" in motivo and intento < intentos:
                print("    Límite por minuto alcanzado, esperando 60 s...")
                time.sleep(60)
                continue
            if e.code == 429:
                raise PresupuestoAgotado(f"Open-Meteo rechazó la petición: {motivo}")
            raise RuntimeError(f"HTTP {e.code}: {motivo}")
        except OSError as e:
            if intento == intentos:
                raise
            print(f"    Error ({e}), reintentando...")
            time.sleep(10 * intento)
    registrar_uso(usadas + costo)
    time.sleep(max(1.0, costo * SEGUNDOS_POR_LLAMADA))
    return datos


def leer_archivo(ruta: Path) -> dict:
    """Devuelve {fecha_hora_utc: {columna: valor}}."""
    if not ruta.exists():
        return {}
    with ruta.open(newline="") as f:
        return {fila[COLUMNA_FECHA]: fila for fila in csv.DictReader(f)}


def guardar_archivo(ruta: Path, filas: dict, variables: list) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(".tmp")
    with temporal.open("w", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=[COLUMNA_FECHA] + variables, extrasaction="ignore")
        escritor.writeheader()
        for clave in sorted(filas):  # ISO 8601 ordena bien como texto
            escritor.writerow(filas[clave])
    temporal.replace(ruta)


def actualizar(nombre_fuente: str, estacion: str, args) -> None:
    fuente = FUENTES[nombre_fuente]
    variables = fuente["variables"]
    ruta = CARPETA_DATOS / fuente["carpeta"] / f"{estacion}.csv"
    filas = leer_archivo(ruta)

    hasta = hoy_utc()
    if filas and not args.completo:
        desde = max(date.fromisoformat(max(filas)[:10]) - timedelta(days=args.dias_revision), fuente["inicio"])
    else:
        desde = fuente["inicio"]

    latitud, longitud = ESTACIONES[estacion]
    bloque = dias_por_bloque(len(variables))
    print(f"  {estacion}: {desde} -> {hasta}")
    agregadas = cambiadas = 0
    try:
        inicio = desde
        while inicio <= hasta:
            fin = min(inicio + timedelta(days=bloque - 1), hasta)
            parametros = {
                "latitude": latitud,
                "longitude": longitud,
                "start_date": inicio.isoformat(),
                "end_date": fin.isoformat(),
                "hourly": ",".join(variables),
                **fuente["parametros"],
            }
            url = fuente["url"] + "?" + urllib.parse.urlencode(parametros, safe=",")
            datos = pedir(url, args.presupuesto, peso(len(variables), (fin - inicio).days + 1))["hourly"]
            for i, instante in enumerate(datos["time"]):
                valores = {v: datos[v][i] for v in variables}
                if all(x is None for x in valores.values()):
                    continue  # horas sin datos (fuera de la cobertura del modelo)
                nueva = {COLUMNA_FECHA: instante, **{v: "" if x is None else str(x) for v, x in valores.items()}}
                anterior = filas.get(instante)
                if anterior is None:
                    agregadas += 1
                elif any(anterior.get(c, "") != nueva[c] for c in variables):
                    cambiadas += 1
                filas[instante] = nueva
            inicio = fin + timedelta(days=1)
    finally:  # se guarda lo descargado aunque se acabe el presupuesto a mitad de camino
        if agregadas or cambiadas:
            guardar_archivo(ruta, filas, variables)
        print(f"    {agregadas} filas nuevas, {cambiadas} filas actualizadas")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fuente", choices=FUENTES, action="append",
                        help="fuente a descargar (se puede repetir); por defecto todas")
    parser.add_argument("--estacion", choices=ESTACIONES, action="append",
                        help="estación a descargar (se puede repetir); por defecto todas")
    parser.add_argument("--dias-revision", type=int, default=DIAS_REVISION,
                        help=f"días hacia atrás que se vuelven a descargar (por defecto {DIAS_REVISION})")
    parser.add_argument("--presupuesto", type=float, default=PRESUPUESTO_DIARIO,
                        help=f"llamadas máximas a usar en el día UTC (por defecto {PRESUPUESTO_DIARIO})")
    parser.add_argument("--completo", action="store_true",
                        help="re-descarga desde el inicio de cada fuente")
    args = parser.parse_args()

    print(f"Llamadas ya usadas hoy (UTC): {leer_uso():.1f} de {args.presupuesto:.0f}")
    try:
        for nombre_fuente in args.fuente or FUENTES:
            print(f"{nombre_fuente}:")
            for estacion in args.estacion or ESTACIONES:
                try:
                    actualizar(nombre_fuente, estacion, args)
                except PresupuestoAgotado:
                    raise
                except Exception as e:  # que una estación con problemas no detenga al resto
                    print(f"    ERROR en {estacion}: {e}")
    except PresupuestoAgotado as e:
        print(f"\nSe detiene la descarga: {e}. Vuelve a ejecutar el script otro día para continuar.")
    print(f"Llamadas usadas hoy (UTC): {leer_uso():.1f}")


if __name__ == "__main__":
    main()
