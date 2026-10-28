# Descargar Álbumes

App de escritorio (Python 3.14 + Tkinter) para buscar álbumes de un artista en YouTube,
ver sus temas y descargarlos como mp3 con BPM y tags ID3.

## Uso
Ejecutar `Iniciar.bat` (o `py -3.14 main.py`). La primera vez, la ventana de verificación
instala sola las dependencias (yt-dlp, librosa, numpy, mutagen, requests) y ofrece instalar
FFmpeg, Deno y Firefox.

## Estructura
- `main.py` – punto de entrada, registro de errores en `config/app.log`
- `nucleo1/` – ventanas, búsqueda, descarga, anti-bloqueo y utilidades
- `Desc_Albunes/` – descargas (no se versiona)

## Notas
- Si ya hay un tema en la carpeta del álbum, se omite: volver a tocar *Descargar* completa lo que falta.
- `ffmpeg/` y `deno/` se descargan desde la propia app y no se versionan.
