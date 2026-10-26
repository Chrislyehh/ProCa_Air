"""
Actualiza los CSV horarios de Datos/Datos_Historicos/ con datos del SINCA.

Cada archivo se llama <Estacion>.csv (ej: Parque_OHiggins.csv) y guarda, como
columnas, todas las variables de VARIABLES que la estación mida:
  - Contaminantes (MP2.5, MP10, NO, NO2, NOx, O3, CO, SO2): tres columnas cada
    uno (registros validados, preliminares y no validados). MP2.5 mantiene los
    nombres de columna originales ("Registros validados", ...); el resto lleva
    el contaminante como prefijo (ej: "PM10 - Registros validados").
  - Meteorología (temperatura, humedad relativa, velocidad y dirección del
    viento): una columna cada una. El SINCA no publica presión atmosférica
    para las estaciones de la Región Metropolitana.

El script:
  1. Lee la página de la Región Metropolitana del SINCA para obtener el id de
     cada estación y luego la página de cada estación, donde están el código de
     cada serie (ej: ./RM/D14/Cal/PM25) y su rango de fechas disponible.
  2. Por cada variable, descarga los datos horarios desde (última fecha con
     datos de esa variable en el archivo - DIAS_REVISION) hasta hoy. Si la
     variable aún no tiene datos en el archivo, descarga todo su historial.
     Se vuelve a bajar una ventana de días ya guardados porque el SINCA va
     cambiando los registros de "no validados" a "preliminares" y "validados".
  3. Combina: los valores descargados reemplazan a los existentes con misma
     fecha y hora; las filas nuevas se agregan.

Para no saturar al SINCA se hace una sola llamada a la vez, con una pausa de
PAUSA segundos entre llamadas (su robots.txt no impone restricciones).

Uso:
    python3 Extractores/Extraer_Datos_MP25.py                 # actualiza todo
    python3 Extractores/Extraer_Datos_MP25.py --dias-revision 120
    python3 Extractores/Extraer_Datos_MP25.py --completo      # re-descarga todo el historial
"""

import argparse
import html
import re
import time
import unicodedata
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

CARPETA_DATOS = Path(__file__).resolve().parent.parent / "Datos" / "Datos_SINCA"
URL_SINCA = "https://sinca.mma.gob.cl"
URL_REGION = URL_SINCA + "/index.php/region/index/id/M"
URL_ESTACION = URL_SINCA + "/index.php/estacion/index/id/{id}"
URL_DESCARGA = (
    URL_SINCA + "/cgi-bin/APUB-MMA/apub.tsindico2.cgi"
    "?outtype=xcl&macro={macropath}//{macro}.ic"
    "&from={desde}&to={hasta}&path=/usr/airviro/data/CONAMA/&lang=esp&rsrc=&macropath="
)
DIAS_REVISION = 60
PAUSA = 5  # segundos mínimos entre llamadas al SINCA

COLUMNAS_FECHA = ["FECHA (YYMMDD)", "HORA (HHMM)"]
ESTADOS = ["Registros validados", "Registros preliminares", "Registros no validados"]

# código SINCA -> columnas en el CSV.
# Contaminantes (Cal): validados, preliminares, no validados. Meteorología (Met): un valor.
VARIABLES = {
    "PM25": ESTADOS,                                        # µg/m3
    "PM10": [f"PM10 - {e}" for e in ESTADOS],               # µg/m3N
    "0002": [f"NO - {e}" for e in ESTADOS],                 # ppb
    "0003": [f"NO2 - {e}" for e in ESTADOS],                # ppb
    "0NOX": [f"NOx - {e}" for e in ESTADOS],                # ppb
    "0008": [f"O3 - {e}" for e in ESTADOS],                 # ppb
    "0004": [f"CO - {e}" for e in ESTADOS],                 # ppm
    "0001": [f"SO2 - {e}" for e in ESTADOS],                # µg/m3N
    "TEMP": ["Temperatura (C)"],
    "RHUM": ["Humedad relativa (%)"],
    "WSPD": ["Velocidad viento (m/s)"],
    "WDIR": ["Direccion viento (grados)"],
}
COLUMNAS = COLUMNAS_FECHA + [c for columnas in VARIABLES.values() for c in columnas]

_ultima_llamada = 0.0


def descargar(url: str, intentos: int = 3) -> str:
    global _ultima_llamada
    for intento in range(1, intentos + 1):
        espera = PAUSA - (time.monotonic() - _ultima_llamada)
        if espera > 0:
            time.sleep(espera)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.read().decode("latin-1")
        except OSError as e:
            if intento == intentos:
                raise
            print(f"    Error ({e}), reintentando...")
            time.sleep(5 * intento)
        finally:
            _ultima_llamada = time.monotonic()


def normalizar(nombre: str) -> str:
    """'Parque O'Higgins' -> 'parque_ohiggins'; 'El Bosque (Acreditada)' -> 'el_bosque'."""
    nombre = re.sub(r"\(.*?\)", "", nombre)
    nombre = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    nombre = re.sub(r"[^A-Za-z0-9 _]", "", nombre)
    return re.sub(r"[ _]+", "_", nombre.strip()).lower()


def a_fecha(yymmdd: str) -> date:
    return datetime.strptime(yymmdd, "%y%m%d").date()


def obtener_ids_estaciones() -> dict:
    """Devuelve {nombre_normalizado: id} de las estaciones de la Región Metropolitana."""
    pagina = html.unescape(descargar(URL_REGION))
    ids = {}
    for id_, nombre in re.findall(r"estacion/index/id/(\d+)[^>]*>\s*([^<]*?)\s*<", pagina):
        if nombre:
            ids[normalizar(nombre)] = id_
    if not ids:
        raise RuntimeError("No se encontraron estaciones en la página del SINCA.")
    return ids


def obtener_series(id_estacion: str) -> dict:
    """Devuelve {codigo: {'macropath', 'macro', 'desde', 'hasta'}} con las series horarias de la estación."""
    pagina = html.unescape(descargar(URL_ESTACION.format(id=id_estacion)))
    patron = re.compile(
        r"macropath=(?P<macropath>\./\w+/\w+/(?P<tipo>Cal|Met)/(?P<codigo>\w+))"
        r"&macro=(?P<macro>[^&]+)&from=(?P<desde>\d{6})&to=(?P<hasta>\d{6})"
    )
    series = {}
    for m in patron.finditer(pagina):
        codigo, macro = m["codigo"], m["macro"]
        if codigo not in VARIABLES:
            continue
        if m["tipo"] == "Cal" and macro != f"{codigo}.horario.horario":
            continue  # promedios diarios y muestreos discretos
        if m["tipo"] == "Met":
            if not macro.startswith("horario_"):
                continue
            if codigo == "WDIR":
                macro += "_spec"  # sin "_spec" el SINCA entrega la rosa de los vientos
        serie = {"macropath": m["macropath"], "macro": macro, "desde": m["desde"], "hasta": m["hasta"]}
        # La meteorología puede venir a varias alturas (ej: horario_000 y horario_003);
        # se usa la serie con el registro más reciente.
        anterior = series.get(codigo)
        if anterior is None or (a_fecha(serie["hasta"]), macro) > (a_fecha(anterior["hasta"]), anterior["macro"]):
            series[codigo] = serie
    return series


def leer_archivo(ruta: Path) -> dict:
    """Lee un CSV propio en {(fecha, hora): {columna: valor}}; acepta archivos con solo algunas columnas."""
    filas = {}
    lineas = ruta.read_text(encoding="latin-1").splitlines()
    if not lineas:
        return filas
    encabezado = lineas[0].rstrip(";").split(";")
    for linea in lineas[1:]:
        valores = linea.strip().rstrip(";").split(";")
        if len(valores) < 2:
            continue
        fila = dict(zip(encabezado, valores))
        filas[(fila[COLUMNAS_FECHA[0]], fila[COLUMNAS_FECHA[1]])] = fila
    return filas


def leer_descarga(texto: str, n_valores: int) -> dict:
    """Convierte el CSV del SINCA en {(fecha, hora): [valores]}."""
    filas = {}
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("FECHA"):
            continue
        partes = linea.split(";")
        if len(partes) < 3 or not re.fullmatch(r"\d{6}", partes[0]):
            continue
        valores = partes[2:2 + n_valores]
        filas[(partes[0], partes[1])] = valores + [""] * (n_valores - len(valores))
    return filas


def clave_orden(clave: tuple) -> tuple:
    # YYMMDD no ordena bien como texto: hay series desde 1997.
    return a_fecha(clave[0]), clave[1]


def actualizar_variable(filas: dict, codigo: str, serie: dict, dias_revision: int, completo: bool) -> bool:
    """Descarga una variable y la mezcla en `filas`. Devuelve True si hubo cambios."""
    columnas = VARIABLES[codigo]
    con_datos = [k for k, fila in filas.items() if any(fila.get(c) for c in columnas)]
    hoy = date.today()
    inicio_sinca = a_fecha(serie["desde"])

    # Series cerradas: el SINCA informa una fecha "hasta" antigua.
    fin_sinca = a_fecha(serie["hasta"])
    activa = fin_sinca >= hoy - timedelta(days=7)
    hasta = hoy if activa else fin_sinca

    if completo or not con_datos:
        desde = inicio_sinca
    else:
        fin_archivo = a_fecha(max(con_datos, key=clave_orden)[0])
        if not activa and fin_archivo >= fin_sinca:
            print(f"    {columnas[0]}: serie cerrada y completa, se omite.")
            return False
        desde = max(fin_archivo - timedelta(days=dias_revision), inicio_sinca)

    url = URL_DESCARGA.format(
        macropath=serie["macropath"],
        macro=serie["macro"],
        desde=desde.strftime("%y%m%d"),
        hasta=hasta.strftime("%y%m%d"),
    )
    print(f"    {columnas[0]}: descargando {desde} -> {hasta}")
    nuevas = leer_descarga(descargar(url), len(columnas))
    if not nuevas:
        print("      El SINCA no devolvió datos.")
        return False

    agregadas = cambiadas = 0
    for (fecha, hora), valores in nuevas.items():
        fila = filas.get((fecha, hora))
        if fila is None:
            fila = filas[(fecha, hora)] = {COLUMNAS_FECHA[0]: fecha, COLUMNAS_FECHA[1]: hora}
            agregadas += 1
        elif any(fila.get(c, "") != v for c, v in zip(columnas, valores)):
            cambiadas += 1
        fila.update(zip(columnas, valores))
    print(f"      {agregadas} filas nuevas, {cambiadas} filas actualizadas")
    return True


def actualizar_archivo(ruta: Path, series: dict, dias_revision: int, completo: bool) -> None:
    print(f"  {ruta.name}")
    filas = leer_archivo(ruta)
    hubo_cambios = False
    for codigo in VARIABLES:
        if codigo not in series:
            continue  # la estación no mide esta variable
        try:
            hubo_cambios |= actualizar_variable(filas, codigo, series[codigo], dias_revision, completo)
        except Exception as e:  # que una variable con problemas no detenga al resto
            print(f"      ERROR en {VARIABLES[codigo][0]}: {e}")
    if not hubo_cambios:
        return

    claves = sorted(filas, key=clave_orden)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(
        ";".join(COLUMNAS) + ";\n"
        + "\n".join(";".join(filas[k].get(c, "") for c in COLUMNAS) + ";" for k in claves)
        + "\n",
        encoding="latin-1",
    )
    temporal.replace(ruta)
    print(f"    Guardado (hasta {claves[-1][0]})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dias-revision", type=int, default=DIAS_REVISION,
                        help=f"días hacia atrás que se vuelven a descargar (por defecto {DIAS_REVISION})")
    parser.add_argument("--completo", action="store_true",
                        help="re-descarga todo el historial de cada estación")
    args = parser.parse_args()

    print("Obteniendo estaciones desde el SINCA...")
    ids = obtener_ids_estaciones()

    for ruta in sorted(CARPETA_DATOS.glob("*.csv")):
        id_estacion = ids.get(normalizar(ruta.stem))
        if id_estacion is None:
            print(f"  {ruta.name}: estación no encontrada en el SINCA, se omite.")
            continue
        try:
            series = obtener_series(id_estacion)
            actualizar_archivo(ruta, series, args.dias_revision, args.completo)
        except Exception as e:  # que una estación con problemas no detenga al resto
            print(f"    ERROR en {ruta.name}: {e}")


if __name__ == "__main__":
    main()
