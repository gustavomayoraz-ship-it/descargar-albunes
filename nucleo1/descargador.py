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
import random
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import anti_bloqueo
from utils import (
    MAX_RUTA, formatear_nombre_tema, limpiar_titulo_tema, ruta_carpeta_album,
)

log = logging.getLogger("albumes.descargador")

# Volumen parejo para todos los temas (estándar de streaming, EBU R128).
VOLUMEN_OBJETIVO_I = -14.0    # LUFS integrado
VOLUMEN_OBJETIVO_TP = -1.5    # true peak máximo (evita saturar)
VOLUMEN_OBJETIVO_LRA = 11.0


class ErrorDescarga(Exception):
    """Falla al descargar/convertir/validar un tema."""


def _opciones_descarga(carpeta_temp, ffmpeg_path, registro):
    opts = anti_bloqueo.opciones_antibloqueo()
    opts.update({
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        # Máxima calidad: entre los formatos de audio se elige el de MAYOR bitrate.
        "format_sort": ["abr", "asr", "acodec"],
        "outtmpl": os.path.join(carpeta_temp, "%(id)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",  # mp3 VBR V0 (~245 kbps, la máxima del códec)
        }],
        "noplaylist": True,
        "ignoreerrors": False,
        "logger": registro,
    })
    if ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path
    return opts


def descargar_audio(url_video, carpeta_temp, ffmpeg_path=None, notas=None):
    """
    Descarga el audio de un video y lo deja como <id>.mp3 en carpeta_temp.

    Si YouTube bloquea el intento, prueba solo la "escalera" de métodos
    (ver anti_bloqueo.plan_de_intentos): con/sin cookies, otros clientes de
    YouTube y modo permisivo, con una pausa corta y al azar entre uno y otro.
    Un video borrado/privado corta la escalera enseguida (insistir no sirve).
    """
    os.makedirs(carpeta_temp, exist_ok=True)
    plan = anti_bloqueo.plan_de_intentos()
    ultimo_error = ""
    hubo_bloqueo = False

    for n, intento in enumerate(plan):
        if n > 0:
            time.sleep(random.uniform(2.0, 5.0))
        registro = anti_bloqueo.RegistroErrores()
        opts = _opciones_descarga(carpeta_temp, ffmpeg_path, registro)
        especificas = anti_bloqueo.opciones_para_intento(intento)
        if "cookiesfrombrowser" not in especificas:
            opts.pop("cookiesfrombrowser", None)  # este intento va SIN cookies
        opts.update(especificas)
        if intento.get("permisivo"):
            opts["format"] = "bestaudio/best/best"
        info = None
        try:
            with anti_bloqueo.crear_ydl(opts) as ydl:
                info = ydl.extract_info(url_video, download=True)
        except Exception as exc:
            ultimo_error = f"{exc} | {registro.texto()}"
        else:
            if not info:
                ultimo_error = registro.texto() or "yt-dlp no devolvió información del video"

        if info:
            ruta_mp3 = os.path.join(carpeta_temp, f"{info.get('id')}.mp3")
            if os.path.isfile(ruta_mp3):
                anti_bloqueo.registrar_resultado(exito=True)
                if intento.get("clientes") and not intento.get("permisivo"):
                    anti_bloqueo.registrar_cliente(intento["clientes"], True)
                if n > 0 and hubo_bloqueo and notas is not None:
                    notas.append(f"bloqueo esquivado cambiando de método ({intento['nombre']})")
                return ruta_mp3
            ultimo_error = "FFmpeg no generó el mp3 (¿falta FFmpeg o falló la conversión?)"
            break  # no es un bloqueo: cambiar de cliente no lo arregla

        log.warning("Intento '%s' falló: %s", intento["nombre"], ultimo_error[:200])
        if anti_bloqueo.es_error_no_disponible(ultimo_error):
            break  # borrado/privado/país: no tiene sentido seguir probando
        if anti_bloqueo.es_error_de_bloqueo(ultimo_error):
            hubo_bloqueo = True
            if intento.get("clientes") and not intento.get("permisivo"):
                anti_bloqueo.registrar_cliente(intento["clientes"], False)
            continue
        # Error que no parece bloqueo (red caída, etc.): tampoco lo arregla otro método.
        break

    anti_bloqueo.registrar_resultado(exito=False, mensaje_error=ultimo_error)
    raise ErrorDescarga(ultimo_error or "no se pudo descargar")


def _exe_ffmpeg(ffmpeg_path):
    nombre = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    if ffmpeg_path:
        candidato = os.path.join(ffmpeg_path, nombre)
        if os.path.isfile(candidato):
            return candidato
    return shutil.which("ffmpeg")


def normalizar_volumen(ruta_mp3, ffmpeg_path=None):
    """
    Pareja el volumen a -14 LUFS con el filtro loudnorm de ffmpeg (una sola
    pasada), recodificando en mp3 VBR V0 para no perder calidad y conservando
    los metadatos. Reemplaza el archivo solo si ffmpeg terminó bien; si falla,
    el original queda intacto y se lanza ErrorDescarga.
    """
    exe = _exe_ffmpeg(ffmpeg_path)
    if not exe:
        raise ErrorDescarga("no se encontró FFmpeg para normalizar el volumen")
    fd, temporal = tempfile.mkstemp(prefix="norm_", suffix=".mp3", dir=os.path.dirname(ruta_mp3))
    os.close(fd)
    try:
        cmd = [
            exe, "-y", "-hide_banner", "-loglevel", "error", "-i", ruta_mp3,
            "-af", f"loudnorm=I={VOLUMEN_OBJETIVO_I}:TP={VOLUMEN_OBJETIVO_TP}:LRA={VOLUMEN_OBJETIVO_LRA}",
            "-codec:a", "libmp3lame", "-q:a", "0", "-map_metadata", "0", "-id3v2_version", "3",
            temporal,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if sys.platform.startswith("win") else 0)
        if r.returncode != 0 or not os.path.isfile(temporal) or os.path.getsize(temporal) < 100_000:
            raise ErrorDescarga(f"ffmpeg falló al normalizar: {(r.stderr or '')[-200:]}")
        os.replace(temporal, ruta_mp3)
    finally:
        if os.path.exists(temporal):
            try:
                os.remove(temporal)
            except OSError:
                pass


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
                  ffmpeg_path=None, duracion_esperada=None, on_etapa=None, arreglos=None, normalizar=True):
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
        ruta_descargada = descargar_audio(url_video, carpeta_temp, ffmpeg_path, notas=arreglos)

        etapa("Validando")
        ok, motivo, dur = validar_mp3(ruta_descargada, duracion_esperada)
        if not ok:
            raise ErrorDescarga(f"Archivo inválido: {motivo}")
        if dur is not None and duracion_esperada:
            arreglos.append(f"duración verificada ({dur:.0f}s)")

        if normalizar:
            etapa("Normalizando volumen")
            try:
                normalizar_volumen(ruta_descargada, ffmpeg_path)
                arreglos.append("volumen normalizado a -14 LUFS")
            except Exception as exc:
                log.warning("No se pudo normalizar %s: %s", ruta_descargada, exc)
                arreglos.append("no se pudo normalizar el volumen (queda el original)")

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
