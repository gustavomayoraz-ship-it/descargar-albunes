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
  - Por defecto no se fuerzan clientes ni User-Agent: yt-dlp elige lo que mejor
    funciona en cada versión (y se mantiene actualizado al iniciar). Solo si
    un intento se bloquea se pasa a una "escalera" de métodos alternativos
    (sin cookies, otro cliente de YouTube, modo permisivo) y el programa
    aprende qué cliente le funciona mejor.
  - Si YouTube igual bloquea, se hacen pausas largas con cuenta regresiva y se
    reintenta solo; recién tras varias pausas seguidas sin éxito se frena la
    tanda (queda guardada para retomarla).
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
COOLDOWNS_PARA_ABORTAR = 3   # pausas largas seguidas sin lograr ni una descarga
PAUSA_LARGA_BASE = 90   # segundos por bloqueo extra
PAUSA_LARGA_MAX = 600
# Clientes de YouTube a probar cuando el método estándar se bloquea. Si alguno
# ya no existe en la versión de yt-dlp instalada, falla rápido y el ranking lo hunde.
CLIENTES_YOUTUBE = ["tv", "web_safari", "mweb", "android_vr", "ios", "web_creator", "web"]

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
    "cooldowns_seguidos": 0,
    "clientes": {},   # cliente -> {"ok": n, "fail": n}
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
            estado["cooldowns_seguidos"] = 0
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
        estado["cooldowns_seguidos"] = 0
        guardar_estado(estado)


def segundos_de_pausa_por_bloqueos():
    """Pausa larga recomendada según bloqueos seguidos (0 si todavía no hace falta)."""
    n = bloqueos_consecutivos()
    if n < BLOQUEOS_PARA_PAUSA_LARGA:
        return 0
    return min(PAUSA_LARGA_MAX, PAUSA_LARGA_BASE * (n - BLOQUEOS_PARA_PAUSA_LARGA + 1))


def registrar_cooldown():
    """Anota que se hizo una pausa larga. Devuelve cuántas van seguidas sin éxito."""
    with _LOCK:
        estado = cargar_estado()
        estado["cooldowns_seguidos"] = estado.get("cooldowns_seguidos", 0) + 1
        guardar_estado(estado)
        return estado["cooldowns_seguidos"]


def debe_abortar():
    """
    True solo si, además de muchos bloqueos seguidos, ya se hicieron varias
    pausas largas sin lograr ni una descarga: ahí insistir empeora el bloqueo.
    """
    estado = cargar_estado()
    return (estado.get("bloqueos_consecutivos", 0) >= BLOQUEOS_PARA_ABORTAR
            and estado.get("cooldowns_seguidos", 0) >= COOLDOWNS_PARA_ABORTAR)


# ----------------------------------------------------------------------
# Escalera de métodos y aprendizaje de clientes
# ----------------------------------------------------------------------
def ranking_clientes():
    """Clientes ordenados por tasa de éxito aprendida (los desconocidos, al medio)."""
    stats = cargar_estado().get("clientes", {})

    def puntaje(c):
        d = stats.get(c) or {}
        ok, fail = d.get("ok", 0), d.get("fail", 0)
        return (ok + 1) / (ok + fail + 2)

    return sorted(CLIENTES_YOUTUBE, key=puntaje, reverse=True)


def registrar_cliente(clientes, exito):
    if not clientes:
        return
    with _LOCK:
        estado = cargar_estado()
        stats = estado.setdefault("clientes", {})
        for c in clientes:
            d = stats.setdefault(c, {"ok": 0, "fail": 0})
            d["ok" if exito else "fail"] += 1
            # Memoria corta: evita que un mal día de hace semanas pese para siempre.
            if d["ok"] + d["fail"] > 40:
                d["ok"], d["fail"] = d["ok"] // 2, d["fail"] // 2
        guardar_estado(estado)


def plan_de_intentos():
    """
    Métodos a probar EN ORDEN para un mismo video. El primero es el normal;
    los siguientes solo se usan si el anterior se bloqueó.
    Cada intento: {"nombre", "clientes" (lista o None), "cookies" (bool), "permisivo" (bool)}.
    """
    plan = []
    if not _cookies_desactivadas and firefox_disponible():
        plan.append({"nombre": "estándar con cookies", "clientes": None, "cookies": True, "permisivo": False})
    plan.append({"nombre": "estándar sin cookies", "clientes": None, "cookies": False, "permisivo": False})
    ranking = ranking_clientes()
    for c in ranking[:2]:
        plan.append({"nombre": f"cliente {c}", "clientes": [c], "cookies": False, "permisivo": False})
    plan.append({"nombre": "modo permisivo", "clientes": ranking, "cookies": False, "permisivo": True})
    return plan


def opciones_para_intento(intento):
    """Opciones de yt-dlp para un intento del plan."""
    opts = opciones_antibloqueo()
    if not intento.get("cookies"):
        opts.pop("cookiesfrombrowser", None)
    elif "cookiesfrombrowser" not in opts and firefox_disponible():
        opts["cookiesfrombrowser"] = ("firefox",)
    if intento.get("clientes"):
        opts["extractor_args"] = {"youtube": {"player_client": list(intento["clientes"])}}
    return opts


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
