# Descargar Álbumes

App de escritorio (Python 3.14 + Tkinter) para buscar álbumes de un artista en YouTube,
ver sus temas y descargarlos como mp3 con BPM y tags ID3.

## Uso
Ejecutar `Iniciar.bat` (o `py -3.14 descargar_albunes.py`). La primera vez, la ventana de verificación
instala sola las dependencias (yt-dlp, librosa, numpy, mutagen, requests) y ofrece instalar
FFmpeg, Deno y Firefox.

## Python 3.14 garantizado
El programa siempre corre con Python 3.14: si se abre con otra versión, `asegurar_python.py` lo relanza con la 3.14
y, si no está instalada, la instala sola (winget o instalador oficial de python.org, sin administrador).
Ese módulo se puede copiar a otros proyectos.

## Estructura
- `descargar_albunes.py` – punto de entrada, registro de errores en `config/app.log`
- `asegurar_python.py` – regla general: fuerza Python 3.14 (relanza o instala)
- `nucleo1/` – ventanas, búsqueda, descarga, anti-bloqueo y utilidades
- `Desc_Albunes/` – descargas (no se versiona)

## Notas
- Si ya hay un tema en la carpeta del álbum, se omite: volver a tocar *Descargar* completa lo que falta.
- `ffmpeg/` y `deno/` se descargan desde la propia app y no se versionan.


## Instalar (Windows)

1. Bajá **DescargarAlbunes-Setup.exe** desde la sección [Releases](../../releases/latest).
2. Abrilo: prepara todo solo (Python 3.14 portable, FFmpeg, Deno y las librerías) y crea los accesos directos.
3. Usá el acceso directo **Descargar Álbumes**.

Para correr desde el código: `Iniciar.bat` (usa Python 3.14 y lo instala si falta).

> Lee [AVISO_LEGAL.md](AVISO_LEGAL.md) antes de usarlo.
