"""
Actualiza los CSV de MP2.5 horario en Datos/Datos_Historicos/ con datos del SINCA.

Cada archivo se llama <Estacion>.csv (ej: Parque_OHiggins.csv). El script:
  1. Lee la página de la Región Metropolitana del SINCA para obtener el código
     de cada estación (ej: ./RM/D14/Cal/PM25) y su rango de fechas disponible.
  2. Descarga el MP2.5 horario desde (última fecha del archivo - DIAS_REVISION)
     hasta hoy.
     Se vuelve a bajar una ventana de días ya guardados porque el SINCA va
     cambiando los registros de "no validados" a "preliminares" y "validados".
  3. Combina: las filas descargadas reemplazan a las existentes con misma
     fecha y hora; las nuevas se agregan.

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

CARPETA_DATOS = Path(__file__).resolve().parent.parent / "Datos" / "Datos_Historicos"
URL_REGION = "https://sinca.mma.gob.cl/index.php/region/index/id/M"
URL_DESCARGA = (
    "https://sinca.mma.gob.cl/cgi-bin/APUB-MMA/apub.tsindico2.cgi"
    "?outtype=xcl&macro={macropath}//PM25.horario.horario.ic"
    "&from={desde}&to={hasta}&path=/usr/airviro/data/CONAMA/&lang=esp&rsrc=&macropath="
)
ENCABEZADO = "FECHA (YYMMDD);HORA (HHMM);Registros validados;Registros preliminares;Registros no validados;"
DIAS_REVISION = 60


def descargar(url: str, intentos: int = 3) -> str:
    for intento in range(1, intentos + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read().decode("latin-1")
        except OSError as e:
            if intento == intentos:
                raise
            print(f"    Error ({e}), reintentando...")
            time.sleep(5 * intento)


def normalizar(nombre: str) -> str:
    """'Parque O'Higgins' -> 'parque_ohiggins'; 'El Bosque (Acreditada)' -> 'el_bosque'."""
    nombre = re.sub(r"\(.*?\)", "", nombre)
    nombre = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    nombre = re.sub(r"[^A-Za-z0-9 _]", "", nombre)
    return re.sub(r"[ _]+", "_", nombre.strip()).lower()


def obtener_estaciones() -> dict:
    """Devuelve {nombre_normalizado: {'macropath', 'desde', 'hasta'}} para MP2.5."""
    pagina = html.unescape(descargar(URL_REGION))
    patron = re.compile(
        r"header=(?P<nombre>[^&]+)&macropath=(?P<macropath>[^&]+/PM25)"
        r"&macro=PM25\.[^&]*&from=(?P<desde>\d{6})&to=(?P<hasta>\d{6})"
    )
    estaciones = {}
    for m in patron.finditer(pagina):
        estaciones[normalizar(m["nombre"])] = {
            "macropath": m["macropath"],
            "desde": m["desde"],
            "hasta": m["hasta"],
        }
    if not estaciones:
        raise RuntimeError("No se encontraron estaciones MP2.5 en la página del SINCA.")
    return estaciones


def leer_filas(texto: str) -> dict:
    """Convierte el CSV del SINCA en {(fecha, hora): linea}."""
    filas = {}
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("FECHA"):
            continue
        fecha, hora = linea.split(";", 2)[:2]
        filas[(fecha, hora)] = linea
    return filas


def a_fecha(yymmdd: str) -> date:
    return datetime.strptime(yymmdd, "%y%m%d").date()


def actualizar_archivo(ruta: Path, estacion: dict, dias_revision: int, completo: bool) -> None:
    filas = leer_filas(ruta.read_text(encoding="latin-1"))
    if filas:
        fin_archivo = a_fecha(max(filas)[0])
    else:  # archivo vacío: se descarga todo el historial
        fin_archivo = a_fecha(estacion["desde"])
        completo = True
    hoy = date.today()

    # Estaciones cerradas: el SINCA informa una fecha "hasta" antigua.
    fin_sinca = a_fecha(estacion["hasta"])
    activa = fin_sinca >= hoy - timedelta(days=7)
    hasta = hoy if activa else fin_sinca

    if completo:
        desde = a_fecha(estacion["desde"])
    else:
        if not activa and fin_archivo >= fin_sinca:
            print(f"  {ruta.name}: estación cerrada y archivo completo, se omite.")
            return
        desde = max(fin_archivo - timedelta(days=dias_revision), a_fecha(estacion["desde"]))

    url = URL_DESCARGA.format(
        macropath=estacion["macropath"],
        desde=desde.strftime("%y%m%d"),
        hasta=hasta.strftime("%y%m%d"),
    )
    print(f"  {ruta.name}: descargando {desde} -> {hasta}")
    nuevas = leer_filas(descargar(url))
    if not nuevas:
        print("    El SINCA no devolvió datos, se deja el archivo igual.")
        return

    agregadas = sum(1 for k in nuevas if k not in filas)
    cambiadas = sum(1 for k, v in nuevas.items() if k in filas and filas[k] != v)
    filas.update(nuevas)

    # YYMMDD/HHMM ordenan bien como texto (todos los datos son del año 2000 en adelante).
    claves = sorted(filas)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(
        ENCABEZADO + "\n" + "\n".join(filas[k] for k in claves) + "\n",
        encoding="latin-1",
    )
    temporal.replace(ruta)

    print(f"    {agregadas} filas nuevas, {cambiadas} filas actualizadas (hasta {claves[-1][0]})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dias-revision", type=int, default=DIAS_REVISION,
                        help=f"días hacia atrás que se vuelven a descargar (por defecto {DIAS_REVISION})")
    parser.add_argument("--completo", action="store_true",
                        help="re-descarga todo el historial de cada estación")
    args = parser.parse_args()

    print("Obteniendo estaciones desde el SINCA...")
    estaciones = obtener_estaciones()

    for ruta in sorted(CARPETA_DATOS.glob("*.csv")):
        estacion = estaciones.get(normalizar(ruta.stem))
        if estacion is None:
            print(f"  {ruta.name}: estación no encontrada en el SINCA, se omite.")
            continue
        try:
            actualizar_archivo(ruta, estacion, args.dias_revision, args.completo)
        except Exception as e:  # que una estación con problemas no detenga al resto
            print(f"    ERROR en {ruta.name}: {e}")


if __name__ == "__main__":
    main()
