# -*- coding: utf-8 -*-
"""
Medidas para reducir la chance de que YouTube bloquee las búsquedas y
descargas, y un ajuste adaptativo simple basado en el historial reciente:
cuantos más bloqueos seguidos, más se espacian las requests (y, si siguen,
se hace una pausa larga o se frena); cuantos más éxitos, más se acortan.

Aclaración honesta: no es machine learning, es una heurística que sube/baja
un número según lo que pasó últimamente, guardada en
config/estado_antibloqueo.json.

Detalles importantes:
  - Los errores de yt-dlp se capturan con un logger propio (RegistroErrores),
    así el detector ve el mensaje real aunque yt-dlp "se los trague".
  - Solo se cuenta como bloqueo lo que realmente lo es (no un video bloqueado
    por país ni un ID que casualmente contiene "429").
  - Los archivos de estado se escriben de forma atómica y con lock.
  - No se fuerzan clientes ni User-Agent: yt-dlp elige lo que mejor funciona
    en cada versión (y se mantiene actualizado al iniciar).
"""

import logging
import os
import re
import threading

from utils import escribir_json_atomico, leer_json_seguro
from ventana_dependencias import RUTA_CONFIG_DIR, asegurar_deno_en_path, ruta_firefox_ejecutable

log = logging.getLogger("albumes.antibloqueo")

RUTA_ESTADO = os.path.join(RUTA_CONFIG_DIR, "estado_antibloqueo.json")

SLEEP_BASE = 2          # segundos mínimos entre descargas en condiciones normales
SLEEP_MAX = 25          # techo del intervalo entre temas
INCREMENTO_POR_BLOQUEO = 3
DECREMENTO_TRAS_EXITOS = 1
EXITOS_PARA_DECAER = 5
VENTANA_HISTORIAL = 30
BLOQUEOS_PARA_PAUSA_LARGA = 3
BLOQUEOS_PARA_ABORTAR = 5
PAUSA_LARGA_BASE = 60   # segundos por bloqueo extra (tope 300)

_LOCK = threading.RLock()

_PATRONES_BLOQUEO = [re.compile(p, re.IGNORECASE) for p in [
    r"sign in to confirm",
    r"not a bot",
    r"confirm that you",
    r"(?:http error|status code|error:?)\s*429",
    r"too many requests",
    r"http error 403",
    r"unusual traffic",
    r"captcha",
    r"rate.?limit",
    r"content isn't available, try again later",
]]
# Mensajes que parecen bloqueo pero son otra cosa (video bloqueado por país).
_PATRON_GEO = re.compile(r"in your country|geo.?restrict|not made this video available", re.IGNORECASE)
_PATRON_NO_DISPONIBLE = re.compile(
    r"video unavailable|private video|has been removed|has been terminated|"
    r"no longer available|copyright|members-only|in your country|"
    r"this video is not available",
    re.IGNORECASE,
)

ESTADO_POR_DEFECTO = {
    "sleep_interval_actual": SLEEP_BASE,
    "bloqueos_consecutivos": 0,
    "exitos_consecutivos": 0,
    "historial": [],  # True = ok, False = bloqueo detectado
}

_cookies_desactivadas = False
_firefox_cache = None


class RegistroErrores:
    """Logger para yt-dlp que guarda los mensajes de error/advertencia."""

    def __init__(self):
        self.errores = []
        self.advertencias = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.advertencias.append(str(msg))

    def error(self, msg):
        self.errores.append(str(msg))

    def texto(self):
        partes = self.errores[-3:] + self.advertencias[-3:]
        return " | ".join(partes) if partes else ""


# ----------------------------------------------------------------------
# Estado persistente
# ----------------------------------------------------------------------
def cargar_estado():
    with _LOCK:
        data = leer_json_seguro(RUTA_ESTADO, {})
        estado = dict(ESTADO_POR_DEFECTO)
        if isinstance(data, dict):
            estado.update(data)
        return estado


def guardar_estado(estado):
    with _LOCK:
        try:
            escribir_json_atomico(RUTA_ESTADO, estado)
        except Exception:
            log.warning("No se pudo guardar el estado anti-bloqueo", exc_info=True)


def es_error_de_bloqueo(mensaje):
    if not mensaje:
        return False
    m = str(mensaje)
    if _PATRON_GEO.search(m):
        return False
    return any(p.search(m) for p in _PATRONES_BLOQUEO)


def es_error_no_disponible(mensaje):
    """True si el video no existe/está privado/bloqueado por país (reintentar no sirve)."""
    return bool(mensaje) and bool(_PATRON_NO_DISPONIBLE.search(str(mensaje)))


def registrar_resultado(exito, mensaje_error=None):
    """
    Actualiza el estado adaptativo. Solo un bloqueo real sube el intervalo;
    un fallo cualquiera (video privado, red caída) no cuenta ni como éxito
    ni como bloqueo. Devuelve el sleep_interval vigente.
    """
    with _LOCK:
        estado = cargar_estado()
        fue_bloqueo = (not exito) and es_error_de_bloqueo(mensaje_error)
        actual = estado.get("sleep_interval_actual", SLEEP_BASE)
        historial = list(estado.get("historial", []))

        if fue_bloqueo:
            historial.append(False)
            estado["bloqueos_consecutivos"] = estado.get("bloqueos_consecutivos", 0) + 1
            estado["exitos_consecutivos"] = 0
            actual = min(SLEEP_MAX, actual + INCREMENTO_POR_BLOQUEO)
            log.warning("Bloqueo detectado (%s). Intervalo -> %ss", str(mensaje_error)[:120], actual)
        elif exito:
            historial.append(True)
            estado["bloqueos_consecutivos"] = 0
            estado["exitos_consecutivos"] = estado.get("exitos_consecutivos", 0) + 1
            if estado["exitos_consecutivos"] % EXITOS_PARA_DECAER == 0:
                actual = max(SLEEP_BASE, actual - DECREMENTO_TRAS_EXITOS)

        estado["historial"] = historial[-VENTANA_HISTORIAL:]
        estado["sleep_interval_actual"] = actual
        guardar_estado(estado)
        return actual


def sleep_interval_actual():
    return cargar_estado().get("sleep_interval_actual", SLEEP_BASE)


def bloqueos_consecutivos():
    return cargar_estado().get("bloqueos_consecutivos", 0)


def reiniciar_bloqueos_consecutivos():
    """Al empezar una nueva tanda se da una nueva oportunidad (el intervalo se conserva)."""
    with _LOCK:
        estado = cargar_estado()
        estado["bloqueos_consecutivos"] = 0
        guardar_estado(estado)


def segundos_de_pausa_por_bloqueos():
    """Pausa larga recomendada según bloqueos seguidos (0 si todavía no hace falta)."""
    n = bloqueos_consecutivos()
    if n < BLOQUEOS_PARA_PAUSA_LARGA:
        return 0
    return min(300, PAUSA_LARGA_BASE * (n - BLOQUEOS_PARA_PAUSA_LARGA + 1))


def debe_abortar():
    """True si hay tantos bloqueos seguidos que conviene frenar la descarga."""
    return bloqueos_consecutivos() >= BLOQUEOS_PARA_ABORTAR


# ----------------------------------------------------------------------
# Opciones de yt-dlp
# ----------------------------------------------------------------------
def firefox_disponible():
    global _firefox_cache
    if _firefox_cache is None:
        try:
            _firefox_cache = ruta_firefox_ejecutable() is not None
        except Exception:
            _firefox_cache = False
    return _firefox_cache


def opciones_antibloqueo():
    """
    Opciones comunes de yt-dlp: sleep adaptativo, reintentos y, si Firefox
    está instalado, sus cookies (las requests parecen de un navegador real).
    Si las cookies fallan alguna vez, se desactivan para el resto de la sesión.
    """
    asegurar_deno_en_path()
    sleep_actual = sleep_interval_actual()
    opts = {
        "sleep_interval": sleep_actual,
        "max_sleep_interval": sleep_actual + 3,
        "sleep_interval_requests": 1,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "socket_timeout": 20,
    }
    if not _cookies_desactivadas and firefox_disponible():
        opts["cookiesfrombrowser"] = ("firefox",)
    return opts


def crear_ydl(opts):
    """
    Crea un YoutubeDL. Si falla por las cookies de Firefox (sin perfil, base
    bloqueada...), reintenta sin cookies en vez de dejar todo inutilizable.
    """
    global _cookies_desactivadas
    from yt_dlp import YoutubeDL

    try:
        return YoutubeDL(opts)
    except Exception:
        if "cookiesfrombrowser" in opts:
            log.warning("No se pudieron leer las cookies de Firefox; sigo sin cookies", exc_info=True)
            _cookies_desactivadas = True
            sin_cookies = dict(opts)
            sin_cookies.pop("cookiesfrombrowser", None)
            return YoutubeDL(sin_cookies)
        raise
