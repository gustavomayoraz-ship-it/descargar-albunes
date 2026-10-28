# -*- coding: utf-8 -*-
"""
Búsqueda en YouTube:
  - Playlists/álbumes de un artista (para que el usuario elija cuáles bajar).
  - Tracklist de una playlist elegida (con URL exacta y duración de cada tema).
  - "Mejor" resultado alternativo para un tema puntual (respaldo, o modo
    "mejor versión"), con filtros de coincidencia, duración y palabras
    descartables (cover, karaoke, etc.).

Los errores de YouTube NO se esconden: si no hay respuesta se lanza
ErrorYouTube con el mensaje real, para poder mostrarlo en pantalla.
"""

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote

import anti_bloqueo

log = logging.getLogger("albumes.buscador")

# Filtro de YouTube "Tipo: Lista de reproducción" en la URL de resultados.
FILTRO_SOLO_PLAYLISTS = "EgIQAw%253D%253D"

# Se aplican sobre texto normalizado (minúsculas, sin acentos) y con límites
# de palabra: "live" NO coincide con "alive" ni "cover" con "discover".
_NEGATIVAS_RE = re.compile(
    r"\b(?:cover|karaoke|instrumental|reaction|reaccion|tutorial|how to play|"
    r"backing track|8d audio|slowed|sped up|nightcore|remix|en vivo|live|acustico)\b"
)
_NO_DISPONIBLE_RE = re.compile(r"^\[?\s*(?:deleted|private)\s+video\s*\]?$", re.IGNORECASE)

UMBRAL_MINIMO = 0.45


class ErrorYouTube(Exception):
    """YouTube no respondió o bloqueó la consulta."""


def _normalizar(texto):
    if not texto:
        return ""
    texto = texto.lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^a-z0-9 ]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def _extraer(url, extract_flat=True):
    """
    Llama a yt-dlp y devuelve (info, mensaje_de_error). Registra el resultado
    en el anti-bloqueo usando el mensaje REAL que dio YouTube.
    """
    reg = anti_bloqueo.RegistroErrores()
    opts = anti_bloqueo.opciones_antibloqueo()
    opts.update({
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": extract_flat,
        "noplaylist": not extract_flat,
        "ignoreerrors": True,
        "logger": reg,
    })
    info = None
    msg = None
    try:
        with anti_bloqueo.crear_ydl(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        msg = str(exc)
    if info is None and not msg:
        msg = reg.texto() or "YouTube no devolvió datos"
    anti_bloqueo.registrar_resultado(exito=info is not None, mensaje_error=msg)
    if info is None:
        log.warning("Fallo al consultar %s: %s", url[:80], msg)
    return info, msg


# ----------------------------------------------------------------------
def buscar_playlists_artista(artista, max_resultados=25):
    """
    Busca playlists/álbumes de un artista.
    Devuelve [{"titulo","url","id","cantidad"}]. Lista vacía = no hay
    resultados. Si YouTube falla, lanza ErrorYouTube.
    """
    query = quote(f"{artista} album")
    url_busqueda = f"https://www.youtube.com/results?search_query={query}&sp={FILTRO_SOLO_PLAYLISTS}"

    info, msg = _extraer(url_busqueda, extract_flat=True)
    if info is None:
        raise ErrorYouTube(msg)

    resultados = []
    vistos = set()
    for entrada in info.get("entries") or []:
        if not entrada:
            continue
        if entrada.get("_type", "") not in ("playlist", "url", None):
            continue
        pid = entrada.get("id")
        webpage_url = entrada.get("url") or (
            f"https://www.youtube.com/playlist?list={pid}" if pid else None
        )
        if not webpage_url:
            continue
        clave = pid or webpage_url
        if clave in vistos:
            continue
        vistos.add(clave)
        resultados.append({
            "titulo": entrada.get("title") or "(sin título)",
            "url": webpage_url,
            "id": pid,
            "cantidad": entrada.get("playlist_count") or entrada.get("n_entries"),
        })
        if len(resultados) >= max_resultados:
            break
    return resultados


def obtener_tracklist_playlist(url_playlist):
    """
    Lista de temas de una playlist, en orden:
    [{"numero","titulo","url","duracion","disponible"}]
    "url" es el video EXACTO de la playlist; "disponible" es False para
    videos borrados/privados. Si YouTube falla, lanza ErrorYouTube.
    """
    info, msg = _extraer(url_playlist, extract_flat=True)
    if info is None:
        raise ErrorYouTube(msg)

    temas = []
    for idx, entrada in enumerate(info.get("entries") or [], start=1):
        if not entrada:
            temas.append({"numero": idx, "titulo": f"Tema {idx}", "url": None,
                          "duracion": None, "disponible": False})
            continue
        titulo = entrada.get("title") or f"Tema {idx}"
        video_id = entrada.get("id")
        url_video = entrada.get("url")
        if url_video and not str(url_video).startswith("http"):
            url_video = f"https://www.youtube.com/watch?v={url_video}"
        if not url_video and video_id:
            url_video = f"https://www.youtube.com/watch?v={video_id}"
        disponible = bool(url_video) and not _NO_DISPONIBLE_RE.match(titulo.strip()) \
            and entrada.get("availability") not in ("private", "needs_auth")
        temas.append({
            "numero": idx,
            "titulo": titulo,
            "url": url_video,
            "duracion": entrada.get("duration"),
            "disponible": disponible,
        })
    return temas


# ----------------------------------------------------------------------
# Búsqueda de "mejor versión" (respaldo / modo opcional)
# ----------------------------------------------------------------------
def _tiene_palabra_negativa(titulo_norm, tema_norm):
    en_titulo = set(_NEGATIVAS_RE.findall(titulo_norm))
    en_tema = set(_NEGATIVAS_RE.findall(tema_norm))
    return bool(en_titulo - en_tema)  # si el tema ya dice "live", no penalizar


def _score_candidato(entrada, artista_norm, tema_norm):
    titulo = entrada.get("title") or ""
    canal = entrada.get("channel") or entrada.get("uploader") or ""
    titulo_norm = _normalizar(titulo)
    canal_norm = _normalizar(canal)

    objetivo = f"{artista_norm} {tema_norm}".strip()
    score = max(
        SequenceMatcher(None, titulo_norm, objetivo).ratio(),
        SequenceMatcher(None, titulo_norm, tema_norm).ratio(),
    )
    if artista_norm and artista_norm in titulo_norm:
        score += 0.15
    if artista_norm and artista_norm in canal_norm:
        score += 0.20
    if tema_norm and tema_norm in titulo_norm:
        score += 0.15
    if "official" in titulo_norm or "oficial" in titulo_norm:
        score += 0.05
    if "topic" in canal_norm:
        score += 0.10  # canales "- Topic": auto-generados por el sello
    if _tiene_palabra_negativa(titulo_norm, tema_norm):
        score -= 0.5
    return score


def buscar_mejor_resultado(artista, tema, max_resultados=25, duracion_esperada=None):
    """
    Busca "artista tema" y devuelve el mejor candidato confiable
    {"url","titulo","canal","duracion"} o None. NO devuelve un "mejor
    intento" dudoso: si nada coincide bien, es preferible no descargar a
    bajar el tema equivocado. Si hay duración esperada, se descartan los
    videos cuya duración difiere demasiado.
    """
    artista_norm = _normalizar(artista)
    tema_norm = _normalizar(tema)
    info, msg = _extraer(f"ytsearch{max_resultados}:{artista} {tema}", extract_flat=True)
    if info is None:
        raise ErrorYouTube(msg)

    candidatos = []
    for entrada in info.get("entries") or []:
        if not entrada:
            continue
        score = _score_candidato(entrada, artista_norm, tema_norm)
        dur = entrada.get("duration")
        if duracion_esperada and dur:
            diff = abs(dur - duracion_esperada)
            if diff > max(15, 0.15 * duracion_esperada):
                continue  # duración incompatible: otro video
            if diff <= 5:
                score += 0.10
            elif diff <= 15:
                score += 0.03
        candidatos.append((score, entrada))

    candidatos.sort(key=lambda par: par[0], reverse=True)
    mejores = [c for c in candidatos if c[0] >= UMBRAL_MINIMO][:3]
    if not mejores:
        return None

    # Entre los mejores por coincidencia, desempata por calidad de audio real.
    mejor_final = None
    mejor_abr = -1
    mejor_score = -1
    for score, entrada in mejores:
        url = entrada.get("url") or entrada.get("webpage_url") or entrada.get("id")
        if url and not str(url).startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        if not url:
            continue
        full_info, _ = _extraer(url, extract_flat=False)
        if not full_info:
            continue
        formatos_audio = [
            f for f in (full_info.get("formats") or [])
            if f.get("acodec") not in (None, "none")
        ]
        abr = max((f.get("abr") or 0) for f in formatos_audio) if formatos_audio else 0
        gana = (
            mejor_final is None
            or score > mejor_score + 0.05
            or (abs(score - mejor_score) <= 0.05 and abr > mejor_abr)
        )
        if gana:
            mejor_final, mejor_abr, mejor_score = full_info, abr, score

    if not mejor_final:
        return None
    return {
        "url": mejor_final.get("webpage_url"),
        "titulo": mejor_final.get("title"),
        "canal": mejor_final.get("channel") or mejor_final.get("uploader"),
        "duracion": mejor_final.get("duration"),
    }
