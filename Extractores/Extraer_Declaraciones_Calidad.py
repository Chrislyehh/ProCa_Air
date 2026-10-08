#!/usr/bin/env python3
"""
Extractor de las Declaraciones diarias de Calidad del Aire (GEC) - Región Metropolitana
Fuente: https://airerm.mma.gob.cl/calidad-del-aire/

Recorre las páginas anuales de "Consolidados de Calidad del Aire", descarga los PDF
(con caché local, así que se puede re-ejecutar sin volver a bajar todo), extrae los
campos de cada declaración y los consolida en un CSV.

Uso:
    pip install requests beautifulsoup4 pdfplumber
    python Extractores/Extraer_Declaraciones_Calidad.py                     # todos los años
    python Extractores/Extraer_Declaraciones_Calidad.py --anios 2023 2024   # solo algunos
    python Extractores/Extraer_Declaraciones_Calidad.py --solo-parsear      # sin red, usa la caché

Formatos detectados (el diseño del PDF cambió con los años):
    A  2015-2017  "Consolidado declaración de Calidad del Aire", texto lineal,
                  incluye dígitos de restricción vehicular.
    B  2018-2021  "DECLARACIÓN DE CALIDAD DEL AIRE" con letras capitulares
                  (la extracción parte palabras: "R MP2,5 / EGULAR").
    C  2022-      Lámina horizontal con columnas (hoy / mañana / medidas);
                  se extrae por coordenadas.
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, unquote

import pdfplumber
import requests
from bs4 import BeautifulSoup

BASE = "https://airerm.mma.gob.cl/"
PAGINA_PRINCIPAL = BASE + "calidad-del-aire/"
CARPETA_DATOS = Path(__file__).resolve().parent.parent / "Datos"
UA = "Mozilla/5.0 (extractor-declaraciones-gec; datos abiertos calidad del aire)"

log = logging.getLogger("gec")

MESES = {m: i for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
     "septiembre", "octubre", "noviembre", "diciembre"], start=1)}
MESES["setiembre"] = 9

# Niveles de calidad del aire (orden de severidad)
NIVELES = ["BUENO", "REGULAR", "ALERTA", "PREEMERGENCIA", "EMERGENCIA"]
RANGO_NIVEL = {n: i for i, n in enumerate(NIVELES)}
RE_NIVEL = re.compile(r"\b(PREEMERGENCIA|EMERGENCIA|ALERTA|REGULAR|BUENO)\b")

# Niveles del PMCA (Potencial Meteorológico de Contaminación Atmosférica)
PMCA_ORDEN = ["BAJO", "REGULAR/BAJO", "REGULAR", "REGULAR/ALTO", "ALTO"]
RANGO_PMCA = {n: i for i, n in enumerate(PMCA_ORDEN)}
RE_PMCA = re.compile(r"\b(REGULAR\s*/\s*BAJO|REGULAR\s*/\s*ALTO|REGULAR|BAJO|ALTO)\b", re.I)

COLUMNAS = [
    "fecha_pronostico", "dia_semana", "fecha_emision", "hora_emision",
    "condicion", "condicion_mp10", "condicion_mp25",
    "pmca_hoy", "pmca_manana", "pmca_manana_max",
    "meteo_hoy", "meteo_manana",
    "restriccion_sin_sello_verde", "restriccion_con_sello_verde",
    "medidas", "formato", "anio_pagina", "archivo", "url", "advertencias",
]


@dataclass
class Registro:
    fecha_pronostico: str = ""
    dia_semana: str = ""
    fecha_emision: str = ""
    hora_emision: str = ""
    condicion: str = ""
    condicion_mp10: str = ""
    condicion_mp25: str = ""
    pmca_hoy: str = ""
    pmca_manana: str = ""
    pmca_manana_max: str = ""
    meteo_hoy: str = ""
    meteo_manana: str = ""
    restriccion_sin_sello_verde: str = ""
    restriccion_con_sello_verde: str = ""
    medidas: str = ""
    formato: str = ""
    anio_pagina: int | None = None
    archivo: str = ""
    url: str = ""
    advertencias: list[str] = field(default_factory=list)

    def fila(self) -> dict:
        d = asdict(self)
        d["advertencias"] = "; ".join(self.advertencias)
        return d


# --------------------------------------------------------------------------
# 1. Descubrimiento de enlaces
# --------------------------------------------------------------------------
def sesion() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def get_con_reintentos(s: requests.Session, url: str, intentos: int = 4, **kw) -> requests.Response:
    ultimo = None
    for i in range(intentos):
        try:
            r = s.get(url, timeout=60, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            ultimo = e
            time.sleep(2 * (i + 1))
    raise ultimo  # type: ignore[misc]


def paginas_anuales(s: requests.Session) -> dict[int, str]:
    """Lee la tabla 'Historial Calidad del Aire' de la página principal."""
    html = get_con_reintentos(s, PAGINA_PRINCIPAL).text
    soup = BeautifulSoup(html, "html.parser")
    out: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"consolidados-de-calidad-del-aire-(?:ano-)?(\d{4})", a["href"])
        if m:
            out.setdefault(int(m.group(1)), urljoin(BASE, a["href"]))
    return dict(sorted(out.items()))


def enlaces_pdf(s: requests.Session, url_anio: str) -> list[tuple[str, str]]:
    """Devuelve [(url_pdf, texto_del_enlace)] de una página anual."""
    soup = BeautifulSoup(get_con_reintentos(s, url_anio).text, "html.parser")
    vistos, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().split("?")[0].endswith(".pdf"):
            continue
        u = urljoin(BASE, href)
        if u not in vistos:
            vistos.add(u)
            out.append((u, a.get_text(" ", strip=True)))
    return out


# --------------------------------------------------------------------------
# 2. Descarga con caché
# --------------------------------------------------------------------------
def ruta_local(cache: Path, anio: int, url: str) -> Path:
    nombre = unquote(url.rstrip("/").split("/")[-1])
    nombre = re.sub(r"[^\w.\-]+", "_", nombre)
    return cache / str(anio) / nombre


def pdf_completo(datos: bytes) -> bool:
    return datos.startswith(b"%PDF") and b"%%EOF" in datos[-2048:]


def descargar(s: requests.Session, url: str, destino: Path) -> str | None:
    """Descarga si no existe (o si el archivo en caché está truncado).
    Devuelve None si ok, o un mensaje de error."""
    if destino.exists() and pdf_completo(destino.read_bytes()):
        return None
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = get_con_reintentos(s, url)
    except Exception as e:  # noqa: BLE001
        return f"error de red: {e}"
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    if not r.content.startswith(b"%PDF"):
        return "la respuesta no es un PDF"
    if not pdf_completo(r.content):
        return "PDF truncado (descarga incompleta)"
    destino.write_bytes(r.content)
    return None


# --------------------------------------------------------------------------
# 3. Utilidades de texto
# --------------------------------------------------------------------------
def sin_tildes(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def limpiar(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def fecha_es(dia: str, mes: str, anio: str) -> date | None:
    try:
        return date(int(re.sub(r"\D", "", anio)), MESES[sin_tildes(mes.lower())], int(dia))
    except (KeyError, ValueError):
        return None


RE_FECHA_LARGA = r"(\d{1,2})\s*(?:de\s+)?([A-Za-zÁÉÍÓÚáéíóú]+)\s+(?:de\s*)?(\d\s?\d\s?\d\s?\d)"


def niveles_pmca(texto: str) -> list[str]:
    """Niveles de PMCA mencionados en un párrafo, en orden y sin repetir."""
    vistos: list[str] = []
    texto = texto or ""
    i = texto.find("PMCA")  # solo lo que sigue a la mención del PMCA
    for m in RE_PMCA.finditer(texto[i:] if i >= 0 else ""):
        n = re.sub(r"\s+", "", m.group(1)).upper()
        if n not in vistos:
            vistos.append(n)
    return vistos


def peor(niveles: list[str], rango: dict[str, int]) -> str:
    validos = [n for n in niveles if n in rango]
    return max(validos, key=rango.__getitem__) if validos else ""


def dividir_hoy_manana(meteo: str) -> tuple[str, str]:
    """Separa el bloque meteorológico en párrafo de hoy y de mañana."""
    m = re.search(r"\n\s*(?:Para\s+(?:el\s+d[ií]a\s+de\s+)?)?[Mm]a\s?[ñn]\s?a\s?na\b", meteo)
    if not m:
        m = re.search(r"Para\s+(?:el\s+d[ií]a\s+de\s+)?ma\s?[ñn]\s?a\s?na\b", meteo)
    if not m:
        return limpiar(meteo), ""
    return limpiar(meteo[: m.start()]), limpiar(meteo[m.start():])


def completar_pmca(r: Registro) -> None:
    hoy, man = niveles_pmca(r.meteo_hoy), niveles_pmca(r.meteo_manana)
    r.pmca_hoy = ";".join(hoy)
    r.pmca_manana = ";".join(man)
    r.pmca_manana_max = peor(man, RANGO_PMCA)


# --------------------------------------------------------------------------
# 4. Parsers por formato
# --------------------------------------------------------------------------
def parse_lineal(texto: str, r: Registro) -> None:
    """Formatos A (2015-2017) y B (2018-2021): texto en una sola columna."""
    # Letras capitulares sueltas en su propia línea: "M\nIÉRCOLES" -> "MIÉRCOLES"
    t = re.sub(r"(?m)^([A-ZÁÉÍÓÚ])\n([A-ZÁÉÍÓÚ]{2,})", r"\1\2", texto)

    # Fecha pronosticada: "para el viernes 17 de abril de 2015" (en B viene partida en líneas)
    m = re.search(r"PARA\s+EL\s+([A-ZÁÉÍÓÚa-záéíóú]+)\s+" + RE_FECHA_LARGA, t, re.I)
    if not m:  # 2020: "MP10 - MP2,5 \n 1 2020 \n PARA EL MIÉRCOLES DE JULIO DE"
        m2 = re.search(r"(\d{1,2})\s+(\d{4})\s*\n\s*PARA\s+EL\s+(\S+)\s+DE\s+(\S+)\s+DE", t, re.I)
        if m2:
            f = fecha_es(m2.group(1), m2.group(4), m2.group(2))
            r.dia_semana = m2.group(3).lower()
            r.fecha_pronostico = f.isoformat() if f else ""
    else:
        f = fecha_es(m.group(2), m.group(3), m.group(4))
        r.dia_semana = m.group(1).lower()
        r.fecha_pronostico = f.isoformat() if f else ""

    # Condición prevista
    bloque = re.search(r"PREVISTA\s*PAR\s*A\s*MA[ÑN]ANA(.*?)CONDICI[ÓO]N\s*METEOROL", t, re.S | re.I)
    if bloque:
        b = bloque.group(1)
        # Letras capitulares: "R MP2,5\nEGULAR" -> "REGULAR MP2,5"
        b = re.sub(r"(?m)^\s*([A-ZÁÉÍÓÚ])\s+(MP\s*2,5|MP\s*10)\s*\n\s*([A-ZÁÉÍÓÚ]+)", r"\1\3 \2", b)
        b = re.sub(r"(?m)^\s*([A-ZÁÉÍÓÚ])\s*\n\s*([A-ZÁÉÍÓÚ]{3,})", r"\1\2", b)
        encontrados = []
        for mm in re.finditer(r"\b(PREEMERGENCIA|EMERGENCIA|ALERTA|REGULAR|BUENO)\b\s*(MP\s*2,5|MP\s*10)?", b):
            nivel, mp = mm.group(1), (mm.group(2) or "").replace(" ", "")
            encontrados.append(nivel)
            if mp == "MP10":
                r.condicion_mp10 = nivel
            elif mp == "MP2,5":
                r.condicion_mp25 = nivel
        r.condicion = peor(encontrados, RANGO_NIVEL)

    # Bloque meteorológico
    mm = re.search(r"CONDICI[ÓO]N\s*METEOROL[ÓO]GICA(.*?)(?:\*\s*PMCA|Fuente\s*:|RESTRICCI)", t, re.S | re.I)
    if mm:
        r.meteo_hoy, r.meteo_manana = dividir_hoy_manana(mm.group(1))

    # Restricción vehicular (solo formato A)
    rv = re.search(r"RESTRICCI[ÓO]N\s+VEHICULAR(.*?)(?:Índice|Indice|$)", t, re.S | re.I)
    if rv:
        txt = rv.group(1)
        sin = re.search(r"SIN\s+SELLO\s+VERDE\s*:?\s*([\d\s\-–]+|NO\s+HAY)", txt, re.I)
        con = re.search(r"CON\s+SELLO\s+VERDE\s*:?\s*([\d\s\-–]+|NO\s+HAY)", txt, re.I)
        if sin:
            r.restriccion_sin_sello_verde = normalizar_digitos(sin.group(1))
        elif re.search(r"NO\s+HAY", txt, re.I):
            r.restriccion_sin_sello_verde = "NO HAY"
        if con:
            r.restriccion_con_sello_verde = normalizar_digitos(con.group(1))
        r.medidas = limpiar(txt)


def normalizar_digitos(s: str) -> str:
    s = s.strip()
    if re.match(r"NO\s+HAY", s, re.I):
        return "NO HAY"
    return "-".join(re.findall(r"\d", s))


def parse_columnas(pdf: pdfplumber.PDF, texto: str, r: Registro) -> None:
    """Formato C (2022-): lámina horizontal; se recorta por coordenadas."""
    pg = pdf.pages[0]
    W, H = pg.width, pg.height
    palabras = pg.extract_words(keep_blank_chars=False, use_text_flow=False)

    def buscar(pred, default=None):
        for w in palabras:
            if pred(w):
                return w
        return default

    # Encabezado: "Condición de la calidad del aire prevista: VIERNES 01/07/2022 BUENO"
    cab = pg.crop((0, 0, W, H * 0.22)).extract_text() or ""
    m = re.search(r"prevista\s*:?\s*([A-ZÁÉÍÓÚa-záéíóú]+)\s+(\d{1,2})/(\d{1,2})/(\d{4})", cab)
    if m:
        try:
            r.fecha_pronostico = date(int(m.group(4)), int(m.group(3)), int(m.group(2))).isoformat()
        except ValueError:
            r.advertencias.append("fecha de encabezado inválida")
        r.dia_semana = m.group(1).lower()
    niveles = RE_NIVEL.findall(cab)
    r.condicion = peor(niveles, RANGO_NIVEL)
    if len(set(niveles)) > 1:
        r.advertencias.append(f"varios niveles en encabezado: {niveles}")

    # Columnas: límites desde las palabras ancla, con valores por defecto proporcionales
    y_ini = H * 0.24
    w_hoy = buscar(lambda w: w["text"].startswith("Hoy") and w["top"] > H * 0.2 and w["x0"] < W * 0.25)
    w_man = buscar(lambda w: w["text"] == "Para" and w["top"] > H * 0.2 and W * 0.2 < w["x0"] < W * 0.45)
    w_fte = buscar(lambda w: w["text"].startswith("Fuente") and w["top"] > H * 0.5)
    w_med = buscar(lambda w: w["text"] == "M" and w["top"] < H * 0.35 and w["x0"] > W * 0.5)
    x_hoy = (w_hoy["x0"] if w_hoy else W * 0.063) - 4
    x_man = (w_man["x0"] if w_man else W * 0.298) - 4
    x_med = (w_med["x0"] if w_med else W * 0.55) - 8
    y_ini = (min(w["top"] for w in (w_hoy, w_man) if w) - 3) if (w_hoy or w_man) else y_ini
    y_fin = (w_fte["top"] - 1) if w_fte else H * 0.69

    r.meteo_hoy = limpiar(pg.crop((x_hoy, y_ini, x_man, y_fin)).extract_text())
    r.meteo_manana = limpiar(pg.crop((x_man, y_ini, x_med, y_fin)).extract_text())
    r.medidas = limpiar(pg.crop((x_med, y_ini + 15, W, H * 0.95)).extract_text())


# --------------------------------------------------------------------------
# 5. Campos comunes y orquestación
# --------------------------------------------------------------------------
def emision(texto: str, r: Registro) -> None:
    m = re.search(r"Fe\s*cha\s+de\s+emisi[óo]n\s*:?\s*(?:[A-Za-záéíóúÁÉÍÓÚ]+,?\s*)?" + RE_FECHA_LARGA
                  + r"(?:\s*,?\s*(\d{1,2}[:.]\d{2})\s*(?:hrs?|horas)?)?", texto, re.I)
    if m:
        f = fecha_es(m.group(1), m.group(2), m.group(3))
        r.fecha_emision = f.isoformat() if f else ""
        r.hora_emision = (m.group(4) or "").replace(".", ":")


def fecha_desde_nombre(nombre: str) -> str:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", nombre)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})", nombre)
        if not m:
            return ""
        d, mo, y = m.groups()
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return ""


def detectar_formato(texto: str) -> str:
    if not texto.strip():
        return "sin_texto"
    if re.search(r"Condici[óo]n de la calidad del aire prevista", texto, re.I):
        return "C"
    if re.search(r"Consolidado\s+declaraci", texto, re.I):
        return "A"
    return "B"


def ruta_relativa(ruta: Path) -> str:
    """Ruta del PDF relativa a la carpeta Datos (ej: pdfs_gec/2015/archivo.pdf)."""
    try:
        return ruta.resolve().relative_to(CARPETA_DATOS).as_posix()
    except ValueError:
        return ruta.as_posix()


def procesar_pdf(ruta: Path, url: str, anio: int) -> Registro:
    r = Registro(archivo=ruta_relativa(ruta), url=url, anio_pagina=anio)
    try:
        with pdfplumber.open(ruta) as pdf:
            texto = pdf.pages[0].extract_text() or ""
            r.formato = detectar_formato(texto)
            if r.formato == "C":
                parse_columnas(pdf, texto, r)
            elif r.formato in ("A", "B"):
                parse_lineal(texto, r)
            emision(texto, r)
    except Exception as e:  # noqa: BLE001
        r.formato = "error"
        r.advertencias.append(f"no se pudo leer el PDF: {e}")
        return r

    completar_pmca(r)

    # Controles de calidad
    por_nombre = fecha_desde_nombre(ruta.name)
    if not r.fecha_pronostico:
        if por_nombre:
            r.fecha_pronostico = por_nombre
            r.advertencias.append("fecha tomada del nombre de archivo")
        else:
            r.advertencias.append("sin fecha de pronóstico")
    elif por_nombre and por_nombre != r.fecha_pronostico:
        r.advertencias.append(f"fecha del nombre de archivo ({por_nombre}) distinta a la del contenido")
    if not r.condicion:
        r.advertencias.append("sin condición de calidad del aire")
    if r.formato == "sin_texto":
        r.advertencias.append("PDF sin capa de texto (requiere OCR)")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anios", type=int, nargs="*", help="años a procesar (por defecto todos)")
    ap.add_argument("--cache", default=CARPETA_DATOS / "pdfs_gec", type=Path,
                    help="carpeta donde guardar los PDF")
    ap.add_argument("--salida", default=CARPETA_DATOS / "declaraciones_calidad_aire_rm.csv", type=Path)
    ap.add_argument("--hilos", type=int, default=4, help="descargas en paralelo (sea amable con el servidor)")
    ap.add_argument("--solo-parsear", action="store_true", help="no usar la red; procesar la caché")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cache = args.cache

    trabajos: list[tuple[int, str, Path]] = []
    fallidas: list[dict] = []

    if args.solo_parsear:
        for p in sorted(cache.glob("*/*.pdf")):
            anio = int(p.parent.name)
            if not args.anios or anio in args.anios:
                trabajos.append((anio, "", p))
    else:
        s = sesion()
        anuales = paginas_anuales(s)
        if args.anios:
            anuales = {a: u for a, u in anuales.items() if a in args.anios}
        log.info("Años encontrados: %s", ", ".join(map(str, anuales)))
        for anio, u in anuales.items():
            links = enlaces_pdf(s, u)
            log.info("  %d: %d enlaces a PDF", anio, len(links))
            trabajos += [(anio, url, ruta_local(cache, anio, url)) for url, _ in links]

        def tarea(t):
            anio, url, dest = t
            return t, descargar(s, url, dest)

        with ThreadPoolExecutor(max_workers=args.hilos) as ex:
            for (anio, url, dest), err in ex.map(tarea, trabajos):
                if err:
                    fallidas.append({"anio_pagina": anio, "url": url, "error": err})
        log.info("Descargas fallidas: %d", len(fallidas))
        for f in fallidas:
            log.info("  %s (%s)", f["url"], f["error"])

    registros = []
    for anio, url, ruta in trabajos:
        if ruta.exists():
            registros.append(procesar_pdf(ruta, url, anio))

    # Duplicados: misma fecha publicada más de una vez
    vistos: dict[str, Registro] = {}
    for r in registros:
        if r.fecha_pronostico in vistos and r.fecha_pronostico:
            r.advertencias.append("fecha duplicada")
            vistos[r.fecha_pronostico].advertencias.append("fecha duplicada")
        vistos.setdefault(r.fecha_pronostico, r)

    registros.sort(key=lambda r: (r.fecha_pronostico, r.archivo))
    args.salida.parent.mkdir(parents=True, exist_ok=True)
    with open(args.salida, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS)
        w.writeheader()
        for r in registros:
            w.writerow(r.fila())

    con_adv = sum(1 for r in registros if r.advertencias)
    log.info("Listo: %d declaraciones -> %s (%d con advertencias)", len(registros), args.salida, con_adv)
    return 0


if __name__ == "__main__":
    sys.exit(main())