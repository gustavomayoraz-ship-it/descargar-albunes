"""
Descargar Álbumes — Instalador (lanzador).

Prepara todo lo que el programa necesita en UNA carpeta, sin que tengas que instalar Python:
  1. Python 3.14 portable COMPLETO (con Tkinter y pip): se bajan los componentes oficiales de python.org y se descomprimen
     en la carpeta del programa (no se registra nada en Windows ni hacen falta permisos de administrador).
  2. El código de Descargar Álbumes (desde GitHub).
  3. ffmpeg y ffprobe (para convertir a MP3 y analizar el audio).
  3b. Deno (el intérprete de JavaScript que yt-dlp necesita para YouTube).
  4. Las librerías: yt-dlp, requests, librosa, numpy, mutagen.
Después crea los accesos directos y abre el programa. Si ya está todo instalado, lo abre directo (sin ventana).

Uso:
  DescargarAlbunesInstalador.py                 instala en %LOCALAPPDATA%\\DescargarAlbunes
  DescargarAlbunesInstalador.py --portable      instala en una carpeta "DescargarAlbunes" junto a este programa (también si existe portable.txt)
  DescargarAlbunesInstalador.py --carpeta RUTA  instala en esa carpeta
  DescargarAlbunesInstalador.py --actualizar    vuelve a bajar el código desde GitHub (no toca música, config ni memoria)
  DescargarAlbunesInstalador.py --ventana       muestra la ventana aunque ya esté todo instalado
"""
import os
import re
import struct
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

# ----------------------------------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------------------------------
NOMBRE_APP = "Descargar Álbumes"
CARPETA_APP = "DescargarAlbunes"
SCRIPT_APP = "descargar_albunes.py"
REPO_ZIP = "https://github.com/gustavomayoraz-ship-it/descargar-albunes/archive/refs/heads/main.zip"
PY_SERIE = "3.14"                                        # regla general de Gustavo: todos sus programas corren con 3.14
DLL_PYTHON = "python" + PY_SERIE.replace(".", "") + "*.dll"
PY_FTP = "https://www.python.org/ftp/python/"
PY_EXE_URL = PY_FTP + "{v}/python-{v}-amd64.exe"
PY_RESPALDO = [f"{PY_SERIE}.{n}" for n in range(15, 0, -1)]   # si no se puede leer el índice de python.org, se prueban estas
# Componentes que se descomprimen del instalador oficial: (nombre, texto que aparece en el nombre del producto del .msi)
MSI_NECESARIOS = [("core", b"Core Interpreter"), ("exe", b"Executables"), ("lib", b"Standard Library"), ("tcltk", b"Tcl/Tk Support")]
FFMPEG_ZIPS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]
EXCLUIR_AL_ACTUALIZAR = {"Desc_Albunes", "ffmpeg", "deno", "python", "_descargas", "config",
                         "instalador", "docs", ".git", ".github", "_backup_antes_de_cambios"}

# (nombre en pip, nombre para importar): las librerías de ESTE programa (mismas que nucleo1/requirements.txt)
DEPENDENCIAS = [("yt-dlp", "yt_dlp"), ("requests", "requests"), ("librosa", "librosa"), ("numpy", "numpy"), ("mutagen", "mutagen")]
# Si pip necesita algo distinto del nombre (extras), se indica acá: yt-dlp[default] trae curl_cffi y yt-dlp-ejs.
PIP_ESPECIFICACION = {"yt-dlp": "yt-dlp[default]"}

# Requisitos extra que no son librerías pip (fila propia en la ventana)
FILA_PYTHON = f"Python {PY_SERIE}"
FILA_CODIGO = "Código"
FILA_FFMPEG = "ffmpeg"
FILA_DENO = "Deno (JS)"
DENO_ZIP = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


# ----------------------------------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------------------------------
def entorno_limpio() -> dict:
    """Entorno sin variables PYTHON*: así ninguna otra versión de Python instalada se mete en la app."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}


def calcular_raiz(argv: list) -> Path:
    """Carpeta donde se instala el programa."""
    if "--carpeta" in argv and argv.index("--carpeta") + 1 < len(argv):
        return Path(argv[argv.index("--carpeta") + 1]).expanduser().resolve()
    aqui = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    if "--portable" in argv or (aqui / "portable.txt").exists():
        return aqui / CARPETA_APP
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / CARPETA_APP


def clave_version(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


# ----------------------------------------------------------------------------------------------------
# Lógica de instalación (sin ventana)
# ----------------------------------------------------------------------------------------------------
class Instalacion:
    """Todo lo que se verifica e instala. No toca la ventana: devuelve resultados y avisa el avance con un callback."""

    def __init__(self, raiz: Path) -> None:
        self.raiz = raiz
        self.py_dir = raiz / "python"
        self.py_exe = self.py_dir / "python.exe"
        self.pyw_exe = self.py_dir / "pythonw.exe"
        self.site = self.py_dir / "Lib" / "site-packages"
        self.ffmpeg_dir = raiz / "ffmpeg"
        self.cache = raiz / "cache_yt_dlp"
        self.proc_actual: Optional[subprocess.Popen] = None
        self.cancelado = False

    # ---------- verificaciones (rápidas, sin ejecutar Python) ----------
    def python_ok(self) -> bool:
        return (self.py_exe.is_file() and self.pyw_exe.is_file() and any(self.py_dir.glob(DLL_PYTHON))
                and (self.py_dir / "DLLs" / "_tkinter.pyd").is_file() and (self.py_dir / "tcl").is_dir()
                and (self.site / "pip").is_dir())

    def codigo_ok(self) -> bool:
        return ((self.raiz / SCRIPT_APP).is_file() and (self.raiz / "nucleo1" / "ventana_principal.py").is_file()
                and (self.raiz / "asegurar_python.py").is_file())

    def ffmpeg_ok(self) -> bool:
        return (self.ffmpeg_dir / "ffmpeg.exe").is_file() and (self.ffmpeg_dir / "ffprobe.exe").is_file()

    def deno_ok(self) -> bool:
        return (self.raiz / "deno" / "deno.exe").is_file()

    def _marca(self, pip_name: str) -> Path:
        return self.raiz / "config" / (".instalando_" + pip_name)    # existe SOLO mientras se instala ese paquete

    def dep_ok(self, nombre: str, import_name: str) -> bool:
        try:
            if self._marca(nombre).exists():
                return False                                       # instalación interrumpida: incompleta
            return ((self.site / import_name).is_dir() or any(self.site.glob(import_name + ".*"))
                    or (self.site / (import_name + ".py")).exists())
        except OSError:
            return False

    def todo_ok(self) -> bool:
        return (self.python_ok() and self.codigo_ok() and self.ffmpeg_ok() and self.deno_ok()
                and all(self.dep_ok(n, i) for n, i in DEPENDENCIAS))

    # ---------- descargas ----------
    def descargar(self, url: str, destino: Path, avance: Optional[Callable[[Optional[int]], None]] = None) -> None:
        """Baja `url` a `destino` (archivo .part que se renombra al terminar). avance(porcentaje o None)."""
        destino.parent.mkdir(parents=True, exist_ok=True)
        parcial = destino.with_name(destino.name + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": "DescargarAlbunes-Instalador"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r, open(parcial, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                hecho, ultimo = 0, -1
                while True:
                    if self.cancelado:
                        raise RuntimeError("cancelado")
                    bloque = r.read(65536)
                    if not bloque:
                        break
                    f.write(bloque)
                    hecho += len(bloque)
                    if avance:
                        pct = int(100 * hecho / total) if total else None
                        if pct != ultimo:                           # solo cuando cambia el porcentaje (no por cada bloque)
                            ultimo = pct if pct is not None else -1
                            avance(pct)
            os.replace(parcial, destino)
        finally:
            parcial.unlink(missing_ok=True)

    def _correr(self, cmd: list, **kw) -> subprocess.CompletedProcess:
        """Corre un comando (se puede cancelar) y devuelve el resultado con la salida de texto."""
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc_actual = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                            text=True, errors="replace", creationflags=flags, env=entorno_limpio(), **kw)
        try:
            salida, _ = self.proc_actual.communicate()
            return subprocess.CompletedProcess(cmd, self.proc_actual.returncode, salida or "")
        finally:
            self.proc_actual = None

    # ---------- Python portable completo (componentes oficiales de python.org) ----------
    def _log(self, texto: str) -> None:
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            with open(self.cache / "instalador.log", "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + texto + "\n")
        except OSError:
            pass

    def faltantes_python(self) -> list:
        """Qué le falta al Python instalado (para mostrar un error claro)."""
        pedidos = [("python.exe", self.py_exe), ("pythonw.exe", self.pyw_exe),
                   ("DLLs\\_tkinter.pyd (Tkinter)", self.py_dir / "DLLs" / "_tkinter.pyd"),
                   ("tcl (Tkinter)", self.py_dir / "tcl"), ("pip", self.site / "pip")]
        falta = [n for n, ruta in pedidos if not ruta.exists()]
        if not any(self.py_dir.glob(DLL_PYTHON)):
            falta.append(DLL_PYTHON)
        return falta

    def _versiones_python(self) -> list:
        """Versiones de la serie publicadas en python.org, de la más nueva a la más vieja."""
        try:
            with urllib.request.urlopen(urllib.request.Request(PY_FTP, headers={"User-Agent": "DescargarAlbunes-Instalador"}),
                                        timeout=30) as r:
                html = r.read().decode("utf-8", errors="replace")
            lista = sorted({m for m in re.findall(r'href="(' + re.escape(PY_SERIE) + r'\.\d+)/"', html)},
                           key=clave_version, reverse=True)
            if lista:
                return lista
        except OSError:
            pass
        return PY_RESPALDO

    def _normalizar_extraccion(self) -> None:
        """Si msiexec dejó los archivos dentro de una subcarpeta, se suben a la raíz de python."""
        if self.py_exe.is_file():
            return
        for exe in self.py_dir.rglob("python.exe"):
            origen = exe.parent
            for hijo in list(origen.iterdir()):
                destino = self.py_dir / hijo.name
                if not destino.exists():
                    shutil.move(str(hijo), str(destino))
            break

    @staticmethod
    def cabinas_del_exe(datos: bytes) -> list:
        """El instalador de Python (WiX Burn) lleva sus componentes en cabinas (.cab) pegadas dentro del propio .exe.
        Devuelve [(desde, hasta)] de cada cabina válida, la más grande primero (esa es la de los componentes)."""
        out = []
        for m in re.finditer(b"MSCF\x00\x00\x00\x00", datos):
            o = m.start()
            if o + 36 > len(datos):
                continue
            tam = struct.unpack_from("<I", datos, o + 8)[0]
            if 1_000_000 < tam and o + tam <= len(datos):
                out.append((o, o + tam))
        return sorted(out, key=lambda t: t[1] - t[0], reverse=True)

    @staticmethod
    def identificar_msi(datos: bytes) -> Optional[str]:
        """Qué componente es este archivo (core, exe, lib, tcltk) según el nombre de producto guardado en el .msi."""
        if datos[:4] != bytes.fromhex("d0cf11e0"):
            return None
        for nombre, clave in MSI_NECESARIOS:
            if clave in datos:
                return nombre
        return None

    def _expandir_cab(self, cab: Path, destino: Path) -> subprocess.CompletedProcess:
        destino.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            return self._correr(["expand.exe", str(cab), "-F:*", str(destino)])     # expand.exe viene con Windows
        return self._correr(["cabextract", "-q", "-d", str(destino), str(cab)])    # (solo para las pruebas en Linux)

    def sacar_msi_del_exe(self, exe: Path, avance: Callable[[str], None]) -> dict:
        """Extrae del instalador los .msi de los componentes necesarios. Devuelve {nombre: ruta}."""
        avance("📦 Abriendo el instalador de Python...")
        datos = exe.read_bytes()
        trabajo = exe.parent / "componentes"
        encontrados: dict = {}
        for n, (desde, hasta) in enumerate(self.cabinas_del_exe(datos)):
            cab = exe.parent / f"contenedor{n}.cab"
            cab.write_bytes(datos[desde:hasta])
            carpeta = trabajo / str(n)
            r = self._expandir_cab(cab, carpeta)
            self._log(f"expand contenedor{n} -> {r.returncode} {r.stdout[-200:]}")
            for f in sorted(carpeta.iterdir()) if carpeta.is_dir() else []:
                if f.is_file() and f.stat().st_size > 10_000:
                    with open(f, "rb") as fh:
                        cabecera = fh.read(4)
                    if cabecera != bytes.fromhex("d0cf11e0"):
                        continue
                    nombre = self.identificar_msi(f.read_bytes())
                    if nombre and nombre not in encontrados:
                        encontrados[nombre] = f.with_name(f.name + ".msi")
                        f.rename(encontrados[nombre])
            if len(encontrados) == len(MSI_NECESARIOS):
                break
        return encontrados

    def extraer_msi(self, msis: dict, avance: Callable[[str], None]) -> None:
        """Descomprime los componentes (.msi) con 'msiexec /a' (instalación administrativa: solo extrae los archivos,
        no registra nada en Windows ni toca los Python que ya tengas)."""
        faltan = [n for n, _c in MSI_NECESARIOS if n not in msis]
        if faltan:
            raise RuntimeError("El instalador de Python no tiene estos componentes: " + ", ".join(faltan))
        if self.py_dir.exists():
            shutil.rmtree(self.py_dir, ignore_errors=True)
        self.py_dir.mkdir(parents=True, exist_ok=True)
        for n, _c in MSI_NECESARIOS:
            avance(f"📦 Descomprimiendo Python ({n})...")
            r = self._correr(["msiexec", "/a", str(msis[n]), "/qn", "/norestart", f"TARGETDIR={self.py_dir}"])
            self._log(f"msiexec /a {n} -> {r.returncode} {r.stdout[-300:]}")
            if r.returncode not in (0, 3010):
                raise RuntimeError(f"msiexec falló con {n} (código {r.returncode}):\n" + r.stdout[-1200:])
        self._normalizar_extraccion()
        # los .msi administrativos dejan una copia de sí mismos junto a los archivos: se borra
        for m in self.py_dir.glob("*.msi"):
            m.unlink(missing_ok=True)

    def instalar_python(self, avance: Callable[[str], None]) -> None:
        desc = self.raiz / "_descargas"
        exe = None
        ultimo = None
        for v in self._versiones_python()[:8]:
            destino = desc / f"python-{v}-amd64.exe"
            try:
                avance(f"⬇️ Descargando Python {v}...")
                self.descargar(PY_EXE_URL.format(v=v), destino,
                               lambda p, v=v: avance(f"⬇️ Descargando Python {v}... {p}%" if p is not None else f"⬇️ Descargando Python {v}..."))
                exe = destino
                break
            except urllib.error.HTTPError as e:
                ultimo = e
                if e.code != 404:                       # 404: esa versión no tiene instalador de Windows, se prueba la anterior
                    raise
        if exe is None:
            raise RuntimeError(f"No se encontró el instalador de Python {PY_SERIE} en python.org ({ultimo})")
        try:
            msis = self.sacar_msi_del_exe(exe, avance)
            self._log(f"componentes encontrados: {sorted(msis)}")
            self.extraer_msi(msis, avance)
            self.asegurar_aislamiento()
            if not (self.site / "pip").is_dir():
                avance("📦 Instalando pip...")
                r = self._correr([str(self.py_exe), "-m", "ensurepip", "--upgrade", "--default-pip"], cwd=str(self.py_dir))
                self._log(f"ensurepip -> {r.returncode} {r.stdout[-300:]}")
        except OSError as e:
            raise RuntimeError(f"No se pudo preparar Python: {e}") from e
        if not self.python_ok():
            self._log(f"faltan: {self.faltantes_python()}")
            raise RuntimeError("El Python quedó incompleto. Falta: " + ", ".join(self.faltantes_python())
                               + f"\n(El registro está en {self.cache / 'instalador.log'})")
        shutil.rmtree(desc, ignore_errors=True)

    SITECUSTOMIZE = (
        "# Lo crea el instalador de Descargar Álbumes: este Python NO debe ver las librerias de otros Python de la PC\n"
        "# (la carpeta de usuario de site-packages), para que use solo las suyas.\n"
        "import sys, site\n"
        "try:\n"
        "    _u = site.getusersitepackages()\n"
        "    sys.path[:] = [p for p in sys.path if p != _u]\n"
        "    site.ENABLE_USER_SITE = False\n"
        "except Exception:\n"
        "    pass\n")

    def asegurar_aislamiento(self) -> None:
        """Pone en Lib\\site-packages un sitecustomize.py que saca del camino la carpeta de usuario de otros Python.
        Sin esto, pip ve las librerías que ya tenés instaladas en tu Python, dice 'ya instalado' y no instala nada."""
        try:
            if self.py_dir.is_dir():
                self.site.mkdir(parents=True, exist_ok=True)
                f = self.site / "sitecustomize.py"
                if not f.is_file() or f.read_text(encoding="utf-8", errors="replace") != self.SITECUSTOMIZE:
                    f.write_text(self.SITECUSTOMIZE, encoding="utf-8")
        except OSError:
            pass

    # ---------- código ----------
    def instalar_codigo(self, avance: Callable[[str], None]) -> None:
        desc = self.raiz / "_descargas"
        zip_cod = desc / "descargar-albunes-main.zip"
        avance("⬇️ Descargando el programa desde GitHub...")
        self.descargar(REPO_ZIP, zip_cod, lambda p: avance(f"⬇️ Descargando el programa... {p}%" if p is not None else "⬇️ Descargando el programa..."))
        raiz_abs = self.raiz.resolve()
        with zipfile.ZipFile(zip_cod) as z:
            for info in z.infolist():
                partes = Path(info.filename).parts[1:]               # se saca la carpeta de arriba (descargar-albunes-main)
                if not partes or info.is_dir() or partes[0] in EXCLUIR_AL_ACTUALIZAR:
                    continue
                destino = raiz_abs.joinpath(*partes).resolve()
                if raiz_abs not in destino.parents:                   # nada de rutas que se salgan de la carpeta
                    continue
                destino.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as o, open(destino, "wb") as f:
                    shutil.copyfileobj(o, f)
        shutil.rmtree(desc, ignore_errors=True)
        if not self.codigo_ok():
            raise RuntimeError(f"El código bajado de GitHub no tiene {SCRIPT_APP} / nucleo1\\ventana_principal.py")

    # ---------- ffmpeg ----------
    def instalar_ffmpeg(self, avance: Callable[[str], None]) -> None:
        desc = self.raiz / "_descargas"
        ultimo: Optional[Exception] = None
        for url in FFMPEG_ZIPS:
            destino = desc / "ffmpeg.zip"
            try:
                avance("⬇️ Descargando ffmpeg...")
                self.descargar(url, destino, lambda p: avance(f"⬇️ Descargando ffmpeg... {p}%" if p is not None else "⬇️ Descargando ffmpeg..."))
                self.ffmpeg_dir.mkdir(parents=True, exist_ok=True)
                avance("📦 Descomprimiendo ffmpeg...")
                with zipfile.ZipFile(destino) as z:
                    for info in z.infolist():
                        nombre = Path(info.filename).name.lower()
                        if nombre in ("ffmpeg.exe", "ffprobe.exe") and "bin" in Path(info.filename).parts:
                            with z.open(info) as o, open(self.ffmpeg_dir / nombre, "wb") as f:
                                shutil.copyfileobj(o, f)
                if self.ffmpeg_ok():
                    shutil.rmtree(desc, ignore_errors=True)
                    return
            except (OSError, RuntimeError, urllib.error.URLError, zipfile.BadZipFile) as e:
                ultimo = e
                if isinstance(e, RuntimeError) and "cancelado" in str(e):
                    raise
        raise RuntimeError(f"No se pudo bajar ffmpeg ({ultimo}).")

    # ---------- Deno (intérprete de JavaScript que yt-dlp necesita para YouTube) ----------
    def instalar_deno(self, avance: Callable[[str], None]) -> None:
        desc = self.raiz / "_descargas"
        destino = desc / "deno.zip"
        avance("⬇️ Descargando Deno...")
        self.descargar(DENO_ZIP, destino, lambda p: avance(f"⬇️ Descargando Deno... {p}%" if p is not None else "⬇️ Descargando Deno..."))
        carpeta = self.raiz / "deno"
        carpeta.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destino) as z:
            for info in z.infolist():
                if Path(info.filename).name.lower() == "deno.exe":
                    with z.open(info) as o, open(carpeta / "deno.exe", "wb") as f:
                        shutil.copyfileobj(o, f)
        shutil.rmtree(desc, ignore_errors=True)
        if not self.deno_ok():
            raise RuntimeError("No se encontró deno.exe dentro del archivo descargado.")

    # ---------- librerías ----------
    def instalar_dependencia(self, nombre: str) -> tuple:
        """pip install <paquete> en el Python del programa. Devuelve (ok, detalle)."""
        self.asegurar_aislamiento()
        marca = self._marca(nombre)
        ok, det = False, None
        try:
            marca.parent.mkdir(parents=True, exist_ok=True)
            reparar = marca.exists()
            marca.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            cmd = [str(self.py_exe), "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location"]
            if reparar:
                cmd.append("--upgrade")
            r = self._correr(cmd + [PIP_ESPECIFICACION.get(nombre, nombre)])
            ok = (r.returncode == 0)
            det = None if ok else (r.stdout or "pip no devolvió detalle.").strip()[-1500:]
            if ok:
                marca.unlink(missing_ok=True)
        except (OSError, RuntimeError) as e:
            ok, det = False, str(e)
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            with open(self.cache / "instalador.log", "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + nombre + "  " + ("OK" if ok else "ERROR\n" + str(det)) + "\n")
        except OSError:
            pass
        return ok, det

    def reparar_permisos(self) -> None:
        """pip (Python 3.13+) deja los archivos con un permiso restringido que impide leerlos desde un inicio normal.
        Se les devuelve el permiso heredado de la carpeta (icacls /reset), una sola vez por instalación."""
        if os.name != "nt":
            return
        marca = self.raiz / "config" / "permisos_ok"
        if marca.exists():
            return
        try:
            r = subprocess.run(["icacls", str(self.raiz), "/reset", "/T", "/C", "/Q"], capture_output=True, text=True,
                               creationflags=CREATE_NO_WINDOW, timeout=600, env=entorno_limpio())
            if r.returncode == 0:
                marca.parent.mkdir(parents=True, exist_ok=True)
                marca.write_text("ok", encoding="utf-8")
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ---------- accesos directos, desinstalador y arranque ----------
    def registrar_desinstalador(self) -> None:
        """Agrega el programa a 'Aplicaciones y características', solo para el usuario actual."""
        if os.name != "nt":
            return
        marca = self.raiz / "config" / "desinstalador_ok"
        if marca.exists():
            return
        r = str(self.raiz)
        script = (
            "@echo off\r\n"
            'if not "%~1"=="x" (copy /y "%~f0" "%TEMP%\\descargaralbunes_desinstalar.cmd" >nul & start "" "%TEMP%\\descargaralbunes_desinstalar.cmd" x & exit /b)\r\n'
            f"title Desinstalar {NOMBRE_APP}\r\n"
            f'echo Se va a borrar {NOMBRE_APP}: {r}\r\n'
            "echo (la musica de la carpeta Desc_Albunes tambien se borra)\r\n"
            f'choice /m "Desinstalar {NOMBRE_APP}"\r\n'
            "if errorlevel 2 exit /b 0\r\n"
            f"powershell -NoProfile -Command \"Get-Process | Where-Object {{ $_.Path -like '{r}\\*' }} | Stop-Process -Force\" >nul 2>nul\r\n"
            "ping -n 3 127.0.0.1 >nul\r\n"
            f'rmdir /s /q "{r}"\r\n'
            f'del "%USERPROFILE%\\Desktop\\{NOMBRE_APP}.lnk" 2>nul\r\n'
            f'del "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\{NOMBRE_APP}.lnk" 2>nul\r\n'
            f'reg delete "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{CARPETA_APP}" /f >nul 2>nul\r\n'
            f"echo Listo, {NOMBRE_APP} fue desinstalado.\r\n"
            "pause\r\n"
        )
        try:
            ruta = self.raiz / "config" / "desinstalar.cmd"
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_bytes(script.encode("cp1252", errors="replace"))
            clave = rf"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{CARPETA_APP}"
            valores = [("DisplayName", "REG_SZ", NOMBRE_APP), ("Publisher", "REG_SZ", "Gustavo Mayoraz"),
                       ("InstallLocation", "REG_SZ", r), ("DisplayIcon", "REG_SZ", str(self.pyw_exe)),
                       ("UninstallString", "REG_SZ", f'"{ruta}"'),
                       ("NoModify", "REG_DWORD", "1"), ("NoRepair", "REG_DWORD", "1")]
            for nombre, tipo, dato in valores:
                res = subprocess.run(["reg", "add", clave, "/v", nombre, "/t", tipo, "/d", dato, "/f"], capture_output=True,
                                     creationflags=CREATE_NO_WINDOW, timeout=30)
                if res.returncode != 0:
                    return
            marca.write_text("ok", encoding="utf-8")
        except (OSError, subprocess.TimeoutExpired):
            pass

    def crear_accesos(self) -> None:
        """Acceso directo en el Escritorio y en el menú Inicio (apuntan directo al Python de la carpeta: abre sin demora)."""
        if os.name != "nt":
            return
        marca = self.raiz / "config" / "accesos_creados"
        if marca.exists():
            return
        def ps(s: str) -> str:
            return "'" + str(s).replace("'", "''") + "'"
        guion = (
            "$w = New-Object -ComObject WScript.Shell; "
            "foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) { "
            f"$l = $w.CreateShortcut((Join-Path $d '{NOMBRE_APP}.lnk')); "
            f"$l.TargetPath = {ps(self.pyw_exe)}; $l.Arguments = {ps(chr(34) + str(self.raiz / SCRIPT_APP) + chr(34))}; "
            f"$l.WorkingDirectory = {ps(self.raiz)}; $l.Description = '{NOMBRE_APP} - descargador de albumes'; $l.Save() }}")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", guion], capture_output=True,
                               text=True, creationflags=CREATE_NO_WINDOW, timeout=60)
            if r.returncode == 0:
                marca.parent.mkdir(parents=True, exist_ok=True)
                marca.write_text("ok", encoding="utf-8")
        except (OSError, subprocess.TimeoutExpired):
            pass

    def lanzar_app(self) -> None:
        flags = (CREATE_NO_WINDOW | DETACHED_PROCESS) if os.name == "nt" else 0
        subprocess.Popen([str(self.pyw_exe if self.pyw_exe.exists() else self.py_exe), str(self.raiz / SCRIPT_APP)],
                         cwd=str(self.raiz), creationflags=flags, close_fds=True, env=entorno_limpio())


# ----------------------------------------------------------------------------------------------------
# Ventana (plantilla VentanaVerificacion)
# ----------------------------------------------------------------------------------------------------
class VentanaVerificacion:
    ANCHO = 400

    def __init__(self, inst: Instalacion, actualizar_codigo: bool = False) -> None:
        self.inst = inst
        self.actualizar_codigo = actualizar_codigo
        self.iniciar = False
        self.errores: dict = {}
        self._pending_after_ids: list = []

        self.ventana = tk.Tk()
        self.ventana.title("🔍 Verificación de dependencias")
        self.ventana.resizable(False, False)
        self.ventana.configure(bg="#f8f9fa")
        self.ventana.attributes("-topmost", True)
        self.ventana.protocol("WM_DELETE_WINDOW", self._al_cerrar)

        cont = tk.Frame(self.ventana, bg="#f8f9fa", padx=10, pady=10)
        cont.pack(fill="both", expand=True)

        # 1. Título
        tk.Label(cont, text="📦 Verificando dependencias", font=("Segoe UI", 14, "bold"), bg="#f8f9fa", fg="#2c3e50").pack(anchor="w")
        tk.Label(cont, text="Asegurando que todo esté listo para comenzar", font=("Segoe UI", 9), bg="#f8f9fa", fg="#7f8c8d").pack(anchor="w", pady=(0, 8))

        # 2. Lista: filas extra (Python, código y ffmpeg, que no son librerías pip) + las dependencias
        self.labels_estado: dict = {}
        self.filas = ([(FILA_PYTHON, "python"), (FILA_CODIGO, "codigo"), (FILA_FFMPEG, "ffmpeg"), (FILA_DENO, "deno")]
                      + [(n, "pip") for n, _i in DEPENDENCIAS])
        for nombre, _tipo in self.filas:
            fila = tk.Frame(cont, bg="#f8f9fa")
            fila.pack(fill="x", pady=1)
            tk.Label(fila, text=nombre, font=("Segoe UI", 9, "bold"), bg="#f8f9fa", fg="#2c3e50", width=14, anchor="w").pack(side="left")
            lbl = tk.Label(fila, text="⏳ Pendiente", font=("Segoe UI", 9), bg="#f8f9fa", fg="#7f8c8d", anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            lbl.bind("<Button-1>", lambda _e, n=nombre: self._detalle(n))
            self.labels_estado[nombre] = lbl

        # 3. Barra
        self.barra = ttk.Progressbar(cont, mode="determinate", length=380)
        self.barra.pack(pady=(8, 4))

        # 4. Estado general
        self.lbl_general = tk.Label(cont, text="🔍 Verificando...", font=("Segoe UI", 9), bg="#f8f9fa", fg="#2c3e50",
                                    anchor="w", justify="left", wraplength=370)
        self.lbl_general.pack(fill="x", pady=(0, 6))

        # 5. Botones
        self.frame_botones = tk.Frame(cont, bg="#f8f9fa")
        self.frame_botones.pack(fill="x", pady=(0, 6))
        self.Iniciar_app = tk.Button(self.frame_botones, text="🚀 Iniciar aplicación", font=("Segoe UI", 9, "bold"), bg="#95a5a6", fg="white",
                                     relief="flat", padx=10, pady=5, state="disabled", command=self._iniciar_app)
        self.Iniciar_app.pack(side="left")
        self.btn_reintentar = tk.Button(self.frame_botones, text="🔄 Reintentar", font=("Segoe UI", 9, "bold"), bg="#95a5a6", fg="white",
                                        relief="flat", padx=10, pady=5, state="disabled", command=self._reintentar)
        self.btn_reintentar.pack(side="left", padx=(8, 0))

        # Tamaño: ancho fijo, alto según el contenido; centrada en pantalla
        self.ventana.update_idletasks()
        alto = self.ventana.winfo_reqheight() + 16
        x = (self.ventana.winfo_screenwidth() - self.ANCHO) // 2
        y = (self.ventana.winfo_screenheight() - alto) // 2
        self.ventana.geometry(f"{self.ANCHO}x{alto}+{x}+{y}")

        self._programar(150, self._lanzar)             # el hilo arranca YA con el mainloop andando

    # ---------- utilidades de la plantilla ----------
    def _programar(self, ms: int, fn: Callable[[], None]):
        """after() que guarda el id: se cancelan al cerrar y no quedan callbacks colgados."""
        caja: list = []

        def correr() -> None:
            if caja and caja[0] in self._pending_after_ids:
                self._pending_after_ids.remove(caja[0])
            fn()
        try:
            id_ = self.ventana.after(ms, correr)
        except (tk.TclError, RuntimeError):
            return None
        caja.append(id_)
        self._pending_after_ids.append(id_)
        return id_

    def _cancelar_pendientes(self) -> None:
        for id_ in self._pending_after_ids:
            try:
                self.ventana.after_cancel(id_)
            except (tk.TclError, RuntimeError):
                pass
        self._pending_after_ids.clear()

    @staticmethod
    def _config_seguro(widget, **kwargs) -> None:
        """widget.config(...) solo si el widget sigue vivo (la ventana pudo cerrarse con un after() todavía en cola)."""
        try:
            if widget.winfo_exists():
                widget.config(**kwargs)
        except (tk.TclError, RuntimeError):
            pass

    def _ui(self, widget, **kwargs) -> None:
        """Actualización de la interfaz desde el hilo de verificación."""
        self._programar(0, lambda: self._config_seguro(widget, **kwargs))

    def _detalle(self, nombre: str) -> None:
        if self.errores.get(nombre):
            messagebox.showerror(f"Error en '{nombre}'", self.errores[nombre], parent=self.ventana)

    # ---------- verificación (hilo) ----------
    def _lanzar(self) -> None:
        self.inst.cancelado = False
        self.errores.clear()
        for nombre, _t in self.filas:
            self._config_seguro(self.labels_estado[nombre], text="⏳ Pendiente", fg="#7f8c8d")
        self._config_seguro(self.barra, value=0)
        self._config_seguro(self.Iniciar_app, state="disabled", bg="#95a5a6")
        self._config_seguro(self.btn_reintentar, state="disabled", bg="#95a5a6")
        threading.Thread(target=self._trabajo, daemon=True).start()

    def _paso(self, nombre: str, tipo: str, hay_python: bool) -> str:
        """Verifica e instala un requisito. Devuelve 'ya', 'nuevo' o 'error'."""
        lbl = self.labels_estado[nombre]
        general = self.lbl_general
        if tipo == "python":
            ok = self.inst.python_ok()
        elif tipo == "codigo":
            ok = self.inst.codigo_ok() and not self.actualizar_codigo
        elif tipo == "ffmpeg":
            ok = self.inst.ffmpeg_ok()
        elif tipo == "deno":
            ok = self.inst.deno_ok()
        else:
            ok = self.inst.dep_ok(nombre, dict(DEPENDENCIAS)[nombre])
        if ok:
            self._ui(lbl, text="✅ Instalado", fg="#27ae60")
            return "ya"
        if tipo == "pip" and not hay_python:
            self.errores[nombre] = "No se puede instalar sin el Python portable."
            self._ui(lbl, text="❌ Sin Python -- click para ver el detalle", fg="#e74c3c")
            return "error"
        self._ui(lbl, text="⏳ Instalando...", fg="#e67e22")
        self._ui(general, text=f"📦 Instalando {nombre} (puede tardar unos minutos)...")
        try:
            if tipo == "python":
                self.inst.instalar_python(lambda t: self._ui(general, text=t))
            elif tipo == "codigo":
                self.inst.instalar_codigo(lambda t: self._ui(general, text=t))
            elif tipo == "ffmpeg":
                self.inst.instalar_ffmpeg(lambda t: self._ui(general, text=t))
            elif tipo == "deno":
                self.inst.instalar_deno(lambda t: self._ui(general, text=t))
            else:
                ok2, det = self.inst.instalar_dependencia(nombre)
                if not ok2 or not self.inst.dep_ok(nombre, dict(DEPENDENCIAS)[nombre]):
                    raise RuntimeError(det or ("pip terminó bien pero el paquete no quedó en la carpeta del programa (" + str(self.inst.site) + ")."))
        except (OSError, RuntimeError, urllib.error.URLError, zipfile.BadZipFile) as e:
            self.errores[nombre] = str(e)
            self._ui(lbl, text="❌ Error -- click acá para ver el detalle", fg="#e74c3c")
            return "error"
        self._ui(lbl, text="✅ Instalado", fg="#27ae60")
        return "nuevo"

    def _trabajo(self) -> None:
        total = len(self.filas)
        nuevos = ya = fallos = 0
        hay_python = True
        for idx, (nombre, tipo) in enumerate(self.filas):
            if self.inst.cancelado:
                return
            self._ui(self.lbl_general, text=f"🔍 Verificando {nombre}...")
            r = self._paso(nombre, tipo, hay_python)
            if r == "ya":
                ya += 1
            elif r == "nuevo":
                nuevos += 1
            else:
                fallos += 1
                if tipo == "python":
                    hay_python = False
            self._ui(self.barra, value=((idx + 1) / total) * 100)
        if fallos == 0:
            self._ui(self.lbl_general, text="🔧 Ajustando permisos de los archivos...")
            self.inst.reparar_permisos()
            self.inst.crear_accesos()
            self.inst.registrar_desinstalador()
            self._programar(0, lambda: self._terminar(True, f"✅ Todo listo: {nuevos} instalado(s), {ya} ya estaban."))
        else:
            self._programar(0, lambda: self._terminar(False, f"❌ Hubo {fallos} error(es): tocá el ❌ para ver el detalle y reintentá."))

    def _terminar(self, ok: bool, resumen: str) -> None:
        self._config_seguro(self.lbl_general, text=resumen)
        if ok:
            self._config_seguro(self.Iniciar_app, state="normal", bg="#27ae60")        # el mismo botón: gris -> verde
        else:
            self._config_seguro(self.btn_reintentar, state="normal", bg="#e67e22")

    def _reintentar(self) -> None:
        self._lanzar()

    # ---------- botones ----------
    def _iniciar_app(self) -> None:
        self._cancelar_pendientes()
        self.iniciar = True
        self.ventana.quit()
        self.ventana.destroy()

    def _al_cerrar(self) -> None:
        self._cancelar_pendientes()
        self.inst.cancelado = True
        p = self.inst.proc_actual
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass
        sys.exit(0)                                    # corta todo el programa, no solo la ventana


# ----------------------------------------------------------------------------------------------------
# Programa
# ----------------------------------------------------------------------------------------------------
def main(argv: list) -> int:
    raiz = calcular_raiz(argv)
    inst = Instalacion(raiz)
    inst.asegurar_aislamiento()
    if inst.todo_ok() and "--ventana" not in argv and "--actualizar" not in argv:       # todo instalado: ni abrir la ventana
        inst.reparar_permisos()
        inst.crear_accesos()
        inst.registrar_desinstalador()
        inst.lanzar_app()
        return 0
    win = VentanaVerificacion(inst, actualizar_codigo=True)       # si hace falta la ventana, de paso se baja la última versión del programa
    win.ventana.mainloop()
    if win.iniciar:
        inst.lanzar_app()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
