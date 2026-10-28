# -*- coding: utf-8 -*-
"""
REGLA GENERAL DE ARRANQUE: el programa SIEMPRE corre con la versión de Python
requerida (por defecto 3.14), sin importar cuántas versiones haya instaladas
ni con cuál se lo abra (doble clic, acceso directo, Iniciar.bat, otra terminal).

Qué hace asegurar(), usando solo la biblioteca estándar:
  1. Si ya está corriendo con la versión requerida, no hace nada.
  2. Si no, busca esa versión en la PC (launcher "py", carpetas habituales) y
     reabre el programa con ella; la instancia actual se cierra.
  3. Si no está instalada (solo Windows), la instala sola mostrando una
     ventana de aviso: primero con winget y, si no se puede, bajando el
     instalador oficial de python.org (instalación por usuario, sin permisos
     de administrador).
  4. Si algo falla, muestra un mensaje claro y deja el detalle en
     config/arranque.log.

Cómo reutilizarlo en otro proyecto: copiar este archivo junto al script de
entrada y poner, ANTES de cualquier otro import del proyecto:

    import asegurar_python
    asegurar_python.asegurar((3, 14))
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time

VERSION_REQUERIDA = (3, 14)

_VAR_REEXEC = "PY_REQUERIDO_REEXEC"  # evita bucles de relanzado
_NO_WINDOW = 0x08000000              # CREATE_NO_WINDOW
_DETACHED = 0x00000008               # DETACHED_PROCESS


def _es_windows():
    return sys.platform.startswith("win")


def _flags():
    return _NO_WINDOW if _es_windows() else 0


def _log(mensaje):
    try:
        carpeta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
        os.makedirs(carpeta, exist_ok=True)
        with open(os.path.join(carpeta, "arranque.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + mensaje + "\n")
    except Exception:
        pass
    try:
        print(mensaje)
    except Exception:
        pass


def _mostrar_error(texto):
    _log("ERROR: " + texto)
    try:
        import tkinter as tk
        from tkinter import messagebox
        raiz = tk.Tk()
        raiz.withdraw()
        raiz.attributes("-topmost", True)
        messagebox.showerror("No se puede iniciar el programa", texto, parent=raiz)
        raiz.destroy()
    except Exception:
        pass


# ----------------------------------------------------------------------
# Búsqueda del intérprete requerido
# ----------------------------------------------------------------------
def _verificar(exe, version):
    """True si 'exe' es realmente un Python de la versión pedida."""
    try:
        r = subprocess.run(
            [exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            creationflags=_flags(),
        )
        return r.returncode == 0 and r.stdout.strip() == f"{version[0]}.{version[1]}"
    except Exception:
        return False


def _buscar_interprete(version):
    """Ruta al intérprete de la versión pedida (pythonw.exe en Windows) o None."""
    mayor, menor = version

    if not _es_windows():
        ruta = shutil.which(f"python{mayor}.{menor}")
        return ruta if ruta and _verificar(ruta, version) else None

    candidatos = []
    try:  # 1) el launcher de Windows
        r = subprocess.run(
            ["py", f"-{mayor}.{menor}", "-c", "import sys;print(sys.executable)"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            creationflags=_flags(),
        )
        if r.returncode == 0 and r.stdout.strip():
            candidatos.append(r.stdout.strip().splitlines()[-1].strip())
    except Exception:
        pass

    # 2) carpetas habituales de instalación
    local = os.environ.get("LOCALAPPDATA", "")
    bases = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    if local:
        bases.insert(0, os.path.join(local, "Programs", "Python"))
    for base in bases:
        for sufijo in ("", "-32", "-arm64"):
            candidatos.append(os.path.join(base, f"Python{mayor}{menor}{sufijo}", "python.exe"))

    for c in candidatos:
        if c and os.path.isfile(c) and _verificar(c, version):
            sin_consola = os.path.join(os.path.dirname(c), "pythonw.exe")
            return sin_consola if os.path.isfile(sin_consola) else c
    return None


# ----------------------------------------------------------------------
# Instalación (solo Windows)
# ----------------------------------------------------------------------
def _instalar_con_winget(version):
    if not shutil.which("winget"):
        return False
    mayor, menor = version
    _log(f"Instalando Python {mayor}.{menor} con winget...")
    try:
        r = subprocess.run(
            ["winget", "install", "-e", "--id", f"Python.Python.{mayor}.{menor}", "--silent",
             "--accept-package-agreements", "--accept-source-agreements"],
            capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL,
            creationflags=_flags(),
        )
        _log(f"winget terminó con código {r.returncode}")
    except Exception as exc:
        _log(f"winget falló: {exc}")
    return _buscar_interprete(version) is not None


def _instalar_desde_python_org(version):
    import platform
    import tempfile
    import urllib.request

    mayor, menor = version
    _log(f"Instalando Python {mayor}.{menor} desde python.org...")
    arquitectura = "arm64" if platform.machine().upper() in ("ARM64", "AARCH64") else "amd64"
    try:
        with urllib.request.urlopen("https://www.python.org/ftp/python/", timeout=30) as resp:
            indice = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        _log(f"No se pudo consultar python.org: {exc}")
        return False

    parches = sorted({int(p) for p in re.findall(rf'href="{mayor}\.{menor}\.(\d+)/"', indice)}, reverse=True)
    for parche in parches[:6]:
        v = f"{mayor}.{menor}.{parche}"
        url = f"https://www.python.org/ftp/python/{v}/python-{v}-{arquitectura}.exe"
        destino = os.path.join(tempfile.gettempdir(), f"python-{v}-{arquitectura}.exe")
        try:
            with urllib.request.urlopen(url, timeout=60) as resp, open(destino, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception as exc:
            _log(f"{url} no disponible: {exc}")
            continue
        try:
            _log(f"Ejecutando el instalador de {v}...")
            subprocess.run(
                [destino, "/quiet", "InstallAllUsers=0", "PrependPath=1",
                 "Include_launcher=1", "Include_test=0"],
                timeout=1800, stdin=subprocess.DEVNULL, creationflags=_flags(),
            )
        except Exception as exc:
            _log(f"El instalador falló: {exc}")
        finally:
            try:
                os.remove(destino)
            except OSError:
                pass
        if _buscar_interprete(version):
            return True
    return False


def _instalar(version):
    return _instalar_con_winget(version) or _instalar_desde_python_org(version)


def _ejecutar_con_ventana(titulo, texto, tarea):
    """Corre 'tarea' en un hilo mostrando una ventana de aviso hasta que termine."""
    resultado = {"ok": False}

    def trabajo():
        try:
            resultado["ok"] = bool(tarea())
        except Exception as exc:
            _log(f"La tarea falló: {exc}")

    hilo = threading.Thread(target=trabajo, daemon=True)
    iniciado = False
    try:
        import tkinter as tk
        raiz = tk.Tk()
        raiz.title(titulo)
        raiz.resizable(False, False)
        raiz.attributes("-topmost", True)
        tk.Label(raiz, text=texto, padx=26, pady=20, font=("Segoe UI", 10), justify="left").pack()
        raiz.protocol("WM_DELETE_WINDOW", lambda: None)  # no se puede cerrar a mitad
        hilo.start()
        iniciado = True

        def vigilar():
            if hilo.is_alive():
                raiz.after(300, vigilar)
            else:
                raiz.destroy()

        raiz.after(300, vigilar)
        raiz.mainloop()
    except Exception as exc:
        _log(f"No se pudo mostrar la ventana de aviso: {exc}")
    if not iniciado:
        hilo.start()
    hilo.join()
    return resultado["ok"]


# ----------------------------------------------------------------------
# Relanzado
# ----------------------------------------------------------------------
def _reabrir(exe):
    script = os.path.abspath(sys.argv[0])
    entorno = dict(os.environ)
    entorno[_VAR_REEXEC] = "1"
    _log(f"Reabriendo con {exe}")
    if _es_windows():
        subprocess.Popen([exe, script, *sys.argv[1:]], cwd=os.path.dirname(script),
                         env=entorno, creationflags=_DETACHED, close_fds=True)
        sys.exit(0)
    os.execve(exe, [exe, script, *sys.argv[1:]], entorno)


def asegurar(version=VERSION_REQUERIDA):
    """Garantiza que el programa corra con 'version'; si no, lo relanza (o instala) y sale."""
    version = tuple(version)
    if sys.version_info[:2] == version:
        return

    etiqueta = f"{version[0]}.{version[1]}"
    _log(f"Corriendo con Python {sys.version.split()[0]}; se requiere {etiqueta}")

    if os.environ.get(_VAR_REEXEC):
        _mostrar_error(f"Se reabrió el programa pero sigue corriendo con Python "
                       f"{sys.version.split()[0]} en vez de {etiqueta}.")
        sys.exit(1)

    exe = _buscar_interprete(version)
    if exe is None:
        if not _es_windows():
            _mostrar_error(f"Este programa necesita Python {etiqueta} y no está instalado.\n"
                           f"Instalá python{etiqueta} y volvé a abrirlo.")
            sys.exit(1)
        _ejecutar_con_ventana(
            "Instalando Python",
            f"Este programa necesita Python {etiqueta}.\n"
            f"Se está instalando automáticamente.\nEsperá unos minutos, el programa se abrirá solo...",
            lambda: _instalar(version),
        )
        exe = _buscar_interprete(version)
        if exe is None:
            _mostrar_error(f"No se pudo instalar Python {etiqueta} automáticamente.\n\n"
                           f"Instalalo a mano desde https://www.python.org/downloads/ "
                           f"(marcá 'Add python.exe to PATH') y volvé a abrir el programa.\n\n"
                           f"Detalle en config\\arranque.log")
            sys.exit(1)

    _reabrir(exe)
