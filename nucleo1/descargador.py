# -*- coding: utf-8 -*-
"""
Descarga de audio con yt-dlp, validación del mp3 resultante, cálculo de BPM
con librosa, tags ID3 con mutagen y renombrado/organización final de cada
tema dentro de la carpeta del álbum.

Cada descarga trabaja en su propia carpeta temporal (que siempre se borra),
y el archivo final solo se publica si pasó la validación (tamaño y duración).
"""

import logging
import os
import shutil
import uuid

import anti_bloqueo
from utils import (
    MAX_RUTA, formatear_nombre_tema, limpiar_titulo_tema, ruta_carpeta_album,
)

log = logging.getLogger("albumes.descargador")


class ErrorDescarga(Exception):
    """Falla al descargar/convertir/validar un tema."""


def _opciones_descarga(carpeta_temp, ffmpeg_path, registro):
    opts = anti_bloqueo.opciones_antibloqueo()
    opts.update({
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(carpeta_temp, "%(id)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "noplaylist": True,
        "ignoreerrors": False,
        "logger": registro,
    })
    if ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path
    return opts


def descargar_audio(url_video, carpeta_temp, ffmpeg_path=None):
    """Descarga el audio de un video y lo deja como <id>.mp3 en carpeta_temp."""
    os.makedirs(carpeta_temp, exist_ok=True)
    registro = anti_bloqueo.RegistroErrores()
    opts = _opciones_descarga(carpeta_temp, ffmpeg_path, registro)
    try:
        with anti_bloqueo.crear_ydl(opts) as ydl:
            info = ydl.extract_info(url_video, download=True)
    except Exception as exc:
        msg = str(exc)
        anti_bloqueo.registrar_resultado(exito=False, mensaje_error=f"{msg} | {registro.texto()}")
        raise ErrorDescarga(msg) from exc

    if not info:
        msg = registro.texto() or "yt-dlp no devolvió información del video"
        anti_bloqueo.registrar_resultado(exito=False, mensaje_error=msg)
        raise ErrorDescarga(msg)
    anti_bloqueo.registrar_resultado(exito=True)

    ruta_mp3 = os.path.join(carpeta_temp, f"{info.get('id')}.mp3")
    if not os.path.isfile(ruta_mp3):
        raise ErrorDescarga("FFmpeg no generó el mp3 (¿falta FFmpeg o falló la conversión?)")
    return ruta_mp3


def validar_mp3(ruta_mp3, duracion_esperada=None):
    """
    Devuelve (ok, motivo, duracion_segundos). Rechaza archivos diminutos,
    ilegibles o cuya duración no coincide con la esperada (otro video).
    """
    try:
        tam = os.path.getsize(ruta_mp3)
    except OSError:
        return False, "no se pudo leer el archivo", None
    if tam < 100_000:
        return False, f"archivo demasiado chico ({tam} bytes)", None

    dur = None
    try:
        from mutagen.mp3 import MP3
        dur = float(MP3(ruta_mp3).info.length)
    except Exception:
        log.warning("No se pudo medir la duración de %s", ruta_mp3, exc_info=True)

    if dur is not None:
        if dur < 15:
            return False, f"duración sospechosa ({dur:.0f}s)", dur
        if duracion_esperada:
            tolerancia = max(10, 0.08 * duracion_esperada)
            if abs(dur - duracion_esperada) > tolerancia:
                return False, f"duración {dur:.0f}s distinta de la esperada ({duracion_esperada:.0f}s)", dur
    return True, "", dur


def calcular_bpm(ruta_mp3, duracion_total=None, notas=None):
    """
    BPM aproximado con librosa. Analiza un tramo de 60 s del MEDIO del tema
    (las intros engañan al detector) y corrige errores de octava llevando el
    resultado al rango 70-180 BPM. Devuelve None si no se pudo calcular.
    Si se pasa una lista en "notas", se agregan ahí las correcciones hechas.
    """
    try:
        import librosa
        import numpy as np

        ventana = 60.0
        offset = 0.0
        if duracion_total:
            offset = max(0.0, min(30.0, duracion_total / 2 - ventana / 2))
        y, sr = librosa.load(ruta_mp3, sr=22050, mono=True, offset=offset, duration=ventana)
        if y.size == 0:
            return None
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        valor = float(np.atleast_1d(tempo)[0])
        if valor <= 0:
            return None
        crudo = valor
        while valor < 70:
            valor *= 2
        while valor > 180:
            valor /= 2
        if notas is not None and round(crudo) != round(valor):
            notas.append(f"BPM corregido de octava ({round(crudo)} → {round(valor)})")
        return round(valor)
    except Exception:
        log.warning("No se pudo calcular el BPM de %s", ruta_mp3, exc_info=True)
        return None


def escribir_tags(ruta_mp3, titulo, artista, album, numero_tema, bpm):
    """Escribe metadatos ID3. Devuelve True/False (un fallo no corta la descarga)."""
    try:
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import ID3NoHeaderError

        try:
            tags = EasyID3(ruta_mp3)
        except ID3NoHeaderError:
            from mutagen.mp3 import MP3
            audio = MP3(ruta_mp3)
            audio.add_tags()
            audio.save()
            tags = EasyID3(ruta_mp3)

        tags["title"] = titulo
        tags["artist"] = artista
        tags["album"] = album
        tags["tracknumber"] = str(numero_tema)
        if bpm:
            tags["bpm"] = str(round(bpm))
        tags.save()
        return True
    except Exception:
        log.warning("No se pudieron escribir los tags de %s", ruta_mp3, exc_info=True)
        return False


def procesar_tema(url_video, artista, nombre_tema, numero_tema, nombre_album,
                  ffmpeg_path=None, duracion_esperada=None, on_etapa=None, arreglos=None):
    """
    Descarga, valida, calcula BPM, taggea y deja el archivo final en la
    carpeta del álbum como "[BPM BPM] Álbum (Artista). NN-Tema.mp3".
    Devuelve la ruta final. Si algo falla, lanza ErrorDescarga (u otra
    excepción) y no deja basura: la carpeta temporal se borra siempre.

    on_etapa(nombre): se llama al empezar cada etapa (para mostrarla en la lista).
    arreglos: lista donde se agregan los arreglos aplicados al tema.
    """
    def etapa(nombre):
        if on_etapa:
            try:
                on_etapa(nombre)
            except Exception:
                pass

    if arreglos is None:
        arreglos = []
    carpeta_album = ruta_carpeta_album(artista, nombre_album)
    base_tmp = os.path.join(carpeta_album, "_tmp")
    carpeta_temp = os.path.join(base_tmp, uuid.uuid4().hex[:8])
    os.makedirs(carpeta_temp, exist_ok=True)

    try:
        etapa("Descargando")
        ruta_descargada = descargar_audio(url_video, carpeta_temp, ffmpeg_path)

        etapa("Validando")
        ok, motivo, dur = validar_mp3(ruta_descargada, duracion_esperada)
        if not ok:
            raise ErrorDescarga(f"Archivo inválido: {motivo}")
        if dur is not None and duracion_esperada:
            arreglos.append(f"duración verificada ({dur:.0f}s)")

        etapa("Analizando BPM")
        bpm = calcular_bpm(ruta_descargada, dur, notas=arreglos)
        if bpm is None:
            arreglos.append("BPM no detectado (SD)")

        etapa("Etiquetas")
        titulo_limpio = limpiar_titulo_tema(nombre_tema, artista, nombre_album)
        if titulo_limpio != (nombre_tema or "").strip():
            arreglos.append(f"título limpiado: «{titulo_limpio}»")
        if not escribir_tags(ruta_descargada, titulo_limpio, artista, nombre_album, numero_tema, bpm):
            arreglos.append("no se pudieron escribir las etiquetas")

        etapa("Renombrando")

        # Largo máximo del nombre para que la ruta completa no pase el límite.
        max_nombre = max(40, MAX_RUTA - len(carpeta_album) - 1 - len(".mp3") - 5)
        nombre_final = formatear_nombre_tema(
            bpm, numero_tema, artista, nombre_tema, nombre_album, max_largo=max_nombre)

        completo = formatear_nombre_tema(bpm, numero_tema, artista, nombre_tema, nombre_album)
        if completo != nombre_final:
            arreglos.append("nombre acortado (límite de ruta de Windows)")

        ruta_candidata = os.path.join(carpeta_album, f"{nombre_final}.mp3")
        contador = 1
        while os.path.exists(ruta_candidata):
            ruta_candidata = os.path.join(carpeta_album, f"{nombre_final} ({contador}).mp3")
            contador += 1
        if contador > 1:
            arreglos.append(f"nombre repetido, se agregó ({contador - 1})")

        os.replace(ruta_descargada, ruta_candidata)
        return ruta_candidata
    finally:
        shutil.rmtree(carpeta_temp, ignore_errors=True)
        try:
            os.rmdir(base_tmp)  # solo si quedó vacía
        except OSError:
            pass
