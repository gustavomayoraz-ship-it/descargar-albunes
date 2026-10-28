# -*- coding: utf-8 -*-
"""
Funciones auxiliares compartidas: sanitizado de nombres de archivo/carpeta,
limpieza y formato final del nombre de cada tema, rutas de descargas y
lectura/escritura segura (atómica) de archivos JSON.
"""

import json
import os
import re
import tempfile
import unicodedata

# Largo máximo que se permite para la ruta completa de un archivo. Windows
# clásico corta en 260; dejamos margen para el sufijo " (n)" y la extensión.
MAX_RUTA = 240

NOMBRES_RESERVADOS_WINDOWS = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitizar_nombre(texto):
    """Deja un texto apto para nombre de archivo/carpeta en Windows/Linux/Mac."""
    if texto is None:
        texto = ""
    limpio = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(texto))
    limpio = re.sub(r"\s+", " ", limpio).strip()
    limpio = limpio.rstrip(". ")
    if limpio.split(".")[0].strip().lower() in NOMBRES_RESERVADOS_WINDOWS:
        limpio += "_"
    return limpio or "Sin_nombre"


def _normalizar_simple(texto):
    """Normaliza un texto para comparar (minúsculas, sin acentos, solo alfanumérico)."""
    if not texto:
        return ""
    texto = texto.lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^a-z0-9 ]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


# Etiquetas de "ruido" típicas de YouTube entre paréntesis/corchetes.
_RUIDO_RE = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"official\s+(?:music\s+)?(?:audio|video|lyric\s+video|lyrics?)"
    r"|audio\s+oficial|video\s+oficial|(?:lyric|lyrics)\s+video"
    r"|full\s+album|[aá]lbum\s+completo"
    r"|hd|hq|audio"
    r"|remaster(?:ed|izado)?(?:\s+\d{4})?"
    r")\s*[\)\]]",
    re.IGNORECASE,
)
# Separador " - " (guion con espacio de al menos un lado). NO parte palabras
# con guion interno como "Pre-Texto".
_SEPARADOR_RE = re.compile(r"\s+[-–—]\s*|\s*[-–—]\s+")


def limpiar_titulo_tema(titulo_crudo, artista=None, nombre_album=None):
    """
    Limpia el título de un tema tal como viene de una playlist de YouTube:
      - saca el número de pista del principio ("12- ", "07 - ");
      - saca etiquetas "(Official Audio)", "(Full Album)", etc.;
      - saca el artista si aparece al principio ("Gary - Tema");
      - saca el álbum/artista si aparecen al final ("Tema - Álbum- Artista"),
        pero nunca deja el título vacío (si el tema se llama igual que el
        álbum, se conserva).
    Devuelve el título original si no queda nada.
    """
    if not titulo_crudo:
        return titulo_crudo

    original = titulo_crudo.strip()
    texto = re.sub(r"^\s*\d{1,3}\s*[.\-_)]\s*", "", original).strip()
    texto = _RUIDO_RE.sub("", texto).strip()

    artista_norm = _normalizar_simple(artista)
    album_norm = _normalizar_simple(nombre_album)

    partes = [p for p in _SEPARADOR_RE.split(texto) if p.strip()]

    # Artista al comienzo: "Gary - Perdóname" -> "Perdóname".
    while len(partes) > 1 and artista_norm and _normalizar_simple(partes[0]) == artista_norm:
        partes.pop(0)

    # Álbum/artista al final: "Tema - Álbum- Artista" -> "Tema".
    while len(partes) > 1:
        cand = _normalizar_simple(partes[-1])
        if not cand:
            partes.pop()
            continue
        es_artista = bool(artista_norm) and len(cand) >= 3 and (
            cand == artista_norm or cand in artista_norm or artista_norm in cand
        )
        es_album = bool(album_norm) and len(cand) >= 4 and (
            cand in album_norm or album_norm in cand
        )
        if es_artista or es_album:
            partes.pop()
        else:
            break

    limpio = " - ".join(partes).strip(" -")
    return limpio or original


def formatear_nombre_tema(bpm, numero_tema, artista, nombre_tema, nombre_album, max_largo=None):
    """
    Devuelve el nombre final del archivo, SIN extensión:
        [89 BPM] Rey de reyes (Kino el cantante). 12-Un toque distinto
    Si se indica max_largo, se acorta el nombre del tema (nunca el resto)
    para no superar el límite de ruta de Windows.
    """
    if bpm is None:
        bpm_txt = "SD BPM"  # SD = Sin Dato (no se pudo calcular el BPM)
    else:
        bpm_txt = f"{round(bpm)} BPM"
    numero_txt = f"{int(numero_tema):02d}"
    artista_limpio = sanitizar_nombre(artista)
    album_limpio = sanitizar_nombre(nombre_album)
    tema_limpio = sanitizar_nombre(limpiar_titulo_tema(nombre_tema, artista, nombre_album))

    def armar(tema):
        return sanitizar_nombre(f"[{bpm_txt}] {album_limpio} ({artista_limpio}). {numero_txt}-{tema}")

    nombre = armar(tema_limpio)
    if max_largo and len(nombre) > max_largo:
        exceso = len(nombre) - max_largo
        recortado = tema_limpio[: max(8, len(tema_limpio) - exceso)].rstrip(" .-")
        nombre = armar(recortado or "Tema")
        # Si aun así no entra (álbum/artista larguísimos), corte duro.
        if len(nombre) > max_largo:
            nombre = nombre[:max_largo].rstrip(" .-")
    return nombre


def tema_ya_descargado(carpeta_album, numero_tema):
    """
    True si en la carpeta del álbum ya hay un mp3 (no vacío) del tema con ese
    número. Sirve para reanudar: volver a descargar solo lo que falta.
    """
    patron = re.compile(rf"\)\. {int(numero_tema):02d}-")
    try:
        for nombre in os.listdir(carpeta_album):
            if nombre.lower().endswith(".mp3") and patron.search(nombre):
                if os.path.getsize(os.path.join(carpeta_album, nombre)) > 100_000:
                    return True
    except OSError:
        pass
    return False


def ruta_descargas_base():
    # Un nivel arriba de nucleo1/ (la raíz del proyecto, donde vive descargar_albunes.py),
    # para que "Desc_Albunes" quede al lado de descargar_albunes.py y no dentro de nucleo1/.
    raiz_proyecto = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(raiz_proyecto, "Desc_Albunes")
    os.makedirs(base, exist_ok=True)
    return base


def ruta_carpeta_album(artista, nombre_album):
    base = ruta_descargas_base()
    carpeta = os.path.join(base, sanitizar_nombre(artista), sanitizar_nombre(nombre_album))
    os.makedirs(carpeta, exist_ok=True)
    return carpeta


# ----------------------------------------------------------------------
# JSON seguro
# ----------------------------------------------------------------------
def leer_json_seguro(ruta, por_defecto):
    """
    Lee un JSON. Si no existe devuelve por_defecto. Si está corrupto, lo
    conserva renombrado como ".corrupto" (no se pierde) y devuelve por_defecto.
    """
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return por_defecto
    except Exception:
        try:
            os.replace(ruta, ruta + ".corrupto")
        except OSError:
            pass
        return por_defecto


def escribir_json_atomico(ruta, data):
    """Escribe en un temporal y reemplaza: nunca deja el JSON a medio escribir."""
    carpeta = os.path.dirname(ruta) or "."
    os.makedirs(carpeta, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=carpeta, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ruta)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
