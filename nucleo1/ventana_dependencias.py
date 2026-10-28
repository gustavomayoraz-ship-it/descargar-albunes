# -*- coding: utf-8 -*-
"""
Ventana de verificación de dependencias para Descargar_Albunes.
Sigue el patrón estándar "VentanaVerificacion" de las apps de escritorio
Python/Tkinter de Gustavo (sin la sección de proxy, que se eliminó).

Novedades de robustez:
  - Se verifica con importlib.util.find_spec (sin importar el paquete), así
    una actualización de yt-dlp hecha acá se usa en esta misma sesión.
  - yt-dlp se actualiza solo si su versión tiene más de 10 días (YouTube
    cambia seguido y las versiones viejas dejan de funcionar).
  - Fila extra para Deno (runtime de JavaScript que yt-dlp necesita hoy para
    YouTube), además de FFmpeg y Firefox.
  - pip y las descargas corren con salida capturada y quedan en el log.
"""

import datetime
import importlib
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import zipfile
from tkinter import messagebox, ttk

from utils import escribir_json_atomico, leer_json_seguro

log = logging.getLogger("albumes.dependencias")

# RUTA_BASE es la raíz del proyecto (un nivel arriba de nucleo1/, donde vive
# descargar_albunes.py), para que config/, ffmpeg/ y deno/ queden siempre al lado de descargar_albunes.py.
RUTA_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUTA_CONFIG_DIR = os.path.join(RUTA_BASE, "config")
RUTA_CONFIG_APP = os.path.join(RUTA_CONFIG_DIR, "config_app.json")
RUTA_FFMPEG_DIR = os.path.join(RUTA_BASE, "ffmpeg")
RUTA_DENO_DIR = os.path.join(RUTA_BASE, "deno")

ES_WINDOWS = sys.platform.startswith("win")
CREATIONFLAGS = 0x08000000 if ES_WINDOWS else 0  # CREATE_NO_WINDOW
DIAS_MAX_YTDLP = 10

# Lista de dependencias pip específicas de ESTE programa (Descargar_Albunes).
# (nombre_para_pip_e_interfaz, nombre_de_import)
DEPENDENCIAS = [
    ("yt-dlp", "yt_dlp"),
    ("requests", "requests"),
    ("librosa", "librosa"),
    ("numpy", "numpy"),
    ("mutagen", "mutagen"),
]
# Especificación real para pip cuando difiere del nombre mostrado.
# yt-dlp[default] trae también yt-dlp-ejs, necesario para YouTube.
PIP_ESPECIFICACION = {"yt-dlp": "yt-dlp[default]"}

URL_FFMPEG = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
URL_DENO = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
URL_FIREFOX = "https://download.mozilla.org/?product=firefox-latest-ssl&os=win64&lang=es-ES"


# ----------------------------------------------------------------------
# Configuración (sin proxy)
# ----------------------------------------------------------------------
CONFIG_POR_DEFECTO = {"historial_busquedas": [], "buscar_mejor_version": False}


def cargar_config_app():
    data = leer_json_seguro(RUTA_CONFIG_APP, {})
    if not isinstance(data, dict):
        data = {}
    # Descarta restos de la versión anterior con proxy.
    for clave in ("proxy_inicial", "proxy_activo", "proxies"):
        data.pop(clave, None)
    for clave, valor in CONFIG_POR_DEFECTO.items():
        data.setdefault(clave, list(valor) if isinstance(valor, list) else valor)
    return data


def guardar_config_app(config_data):
    escribir_json_atomico(RUTA_CONFIG_APP, config_data)


# ----------------------------------------------------------------------
# Búsqueda de ejecutables externos
# ----------------------------------------------------------------------
def ruta_ffmpeg_ejecutable():
    """Ruta al ffmpeg a usar (PATH, carpeta del proyecto o subcarpetas), o None."""
    en_path = shutil.which("ffmpeg")
    if en_path:
        return en_path

    nombre_binario = "ffmpeg.exe" if ES_WINDOWS else "ffmpeg"
    for candidato in (
        os.path.join(RUTA_FFMPEG_DIR, nombre_binario),
        os.path.join(RUTA_FFMPEG_DIR, "bin", nombre_binario),
    ):
        if os.path.isfile(candidato):
            return candidato

    if os.path.isdir(RUTA_FFMPEG_DIR):
        for raiz, _dirs, archivos in os.walk(RUTA_FFMPEG_DIR):
            if nombre_binario in archivos:
                return os.path.join(raiz, nombre_binario)
    return None


def ruta_deno_ejecutable():
    """Ruta a deno (carpeta del proyecto o PATH), o None."""
    nombre = "deno.exe" if ES_WINDOWS else "deno"
    local = os.path.join(RUTA_DENO_DIR, nombre)
    if os.path.isfile(local):
        return local
    return shutil.which("deno")


def asegurar_deno_en_path():
    """Agrega la carpeta de deno del proyecto al PATH del proceso (yt-dlp lo busca ahí)."""
    local = os.path.join(RUTA_DENO_DIR, "deno.exe" if ES_WINDOWS else "deno")
    if os.path.isfile(local):
        carpeta = os.path.dirname(local)
        partes = os.environ.get("PATH", "").split(os.pathsep)
        if carpeta not in partes:
            os.environ["PATH"] = carpeta + os.pathsep + os.environ.get("PATH", "")


def ruta_firefox_ejecutable():
    """Ruta al firefox.exe instalado (PATH, Program Files, perfil o registro), o None."""
    en_path = shutil.which("firefox")
    if en_path:
        return en_path
    if not ES_WINDOWS:
        return None

    candidatos = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Mozilla Firefox", "firefox.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Mozilla Firefox", "firefox.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Mozilla Firefox", "firefox.exe"),
    ]
    for candidato in candidatos:
        if candidato and os.path.isfile(candidato):
            return candidato

    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Mozilla\Mozilla Firefox") as clave:
                    version, _ = winreg.QueryValueEx(clave, "CurrentVersion")
                with winreg.OpenKey(hive, rf"SOFTWARE\Mozilla\Mozilla Firefox\{version}\Main") as clave_main:
                    ruta, _ = winreg.QueryValueEx(clave_main, "PathToExe")
                if ruta and os.path.isfile(ruta):
                    return ruta
            except OSError:
                continue
    except ImportError:
        pass
    return None


# ----------------------------------------------------------------------
# pip / descargas (sin ventanas, con salida capturada)
# ----------------------------------------------------------------------
def _paquete_instalado(import_name):
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


def antiguedad_ytdlp_dias():
    """Días desde la fecha de la versión instalada de yt-dlp (None si no se sabe)."""
    try:
        from importlib.metadata import version
        m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", version("yt-dlp"))
        if not m:
            return None
        fecha = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (datetime.date.today() - fecha).days
    except Exception:
        return None


def _pip_install(especificacion, actualizar=False):
    """Instala/actualiza con pip. Reintenta con --user si falla por permisos."""
    base = [sys.executable, "-m", "pip", "install", especificacion, "--quiet",
            "--disable-pip-version-check"]
    if actualizar:
        base.append("-U")
    for extra in ([], ["--user"]):
        try:
            r = subprocess.run(
                base + extra, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                creationflags=CREATIONFLAGS, timeout=900,
            )
            if r.returncode == 0:
                importlib.invalidate_caches()
                return True
            log.warning("pip %s falló (%s): %s", especificacion, extra, (r.stderr or r.stdout)[-400:])
        except Exception:
            log.warning("pip %s lanzó una excepción", especificacion, exc_info=True)
    return False


def _descargar_archivo(url, destino, progreso=None):
    """Descarga por streaming (sin cargar todo en memoria). progreso(mb_descargados)."""
    import requests

    with requests.get(url, stream=True, timeout=(15, 60), allow_redirects=True) as resp:
        resp.raise_for_status()
        total = 0
        with open(destino, "wb") as f:
            for bloque in resp.iter_content(chunk_size=1024 * 256):
                if bloque:
                    f.write(bloque)
                    total += len(bloque)
                    if progreso:
                        progreso(total / (1024 * 1024))
    if total == 0:
        raise RuntimeError("descarga vacía")


class VentanaVerificacion:
    def __init__(self, config_data=None, on_continuar=None):
        self.on_continuar = on_continuar
        self.config_data = config_data if config_data is not None else cargar_config_app()

        self.ventana = tk.Tk()
        self.ventana.title("🔍 Verificación de dependencias")
        self.ventana.resizable(False, False)
        self.ventana.configure(bg="#f8f9fa")
        self.ventana.attributes("-topmost", True)

        self._pending_after_ids = []
        self._cancelar_pendientes()

        self.labels_estado = {}
        self._criticos_ok = False

        self._construir_ui()
        self.ventana.protocol("WM_DELETE_WINDOW", self._on_cerrar)
        self._centrar_ventana()
        self.ventana.after(200, self._iniciar_verificacion_thread)

    # ------------------------------------------------------------------
    # Utilidades de hilo / after seguros
    # ------------------------------------------------------------------
    def _programar(self, ms, fn):
        try:
            id_ = self.ventana.after(ms, fn)
        except (RuntimeError, tk.TclError):
            return None
        self._pending_after_ids.append(id_)
        return id_

    def _cancelar_pendientes(self):
        for id_ in getattr(self, "_pending_after_ids", []):
            try:
                self.ventana.after_cancel(id_)
            except Exception:
                pass
        self._pending_after_ids = []

    def _config_seguro(self, widget, **kwargs):
        try:
            if widget.winfo_exists():
                widget.config(**kwargs)
        except Exception:
            pass

    def _estado(self, widget, texto, color):
        """Atajo para actualizar un label de estado desde cualquier hilo."""
        self._programar(0, lambda: self._config_seguro(widget, text=texto, fg=color))

    def _general(self, texto):
        self._programar(0, lambda: self._config_seguro(self.lbl_estado_general, text=texto))

    def _centrar_ventana(self):
        self.ventana.update_idletasks()
        ancho = 400
        alto = self.ventana.winfo_reqheight() + 16
        x = (self.ventana.winfo_screenwidth() // 2) - (ancho // 2)
        y = (self.ventana.winfo_screenheight() // 2) - (alto // 2)
        self.ventana.geometry(f"{ancho}x{alto}+{x}+{y}")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _fila_estado(self, contenedor, nombre):
        fila = tk.Frame(contenedor, bg="#f8f9fa")
        fila.pack(fill="x", pady=2)
        tk.Label(
            fila, text=nombre, font=("Segoe UI", 10, "bold"),
            bg="#f8f9fa", fg="#2c3e50", width=14, anchor="w",
        ).pack(side="left")
        lbl = tk.Label(
            fila, text="⏳ Pendiente", font=("Segoe UI", 10),
            bg="#f8f9fa", fg="#7f8c8d", anchor="w",
        )
        lbl.pack(side="left", fill="x", expand=True)
        return lbl

    def _boton_instalador(self, contenedor, texto, comando):
        btn = tk.Button(
            contenedor, text=texto, font=("Segoe UI", 9), bg="#95a5a6", fg="white",
            relief="flat", padx=8, pady=3, state="disabled", command=comando,
        )
        btn.pack(fill="x", pady=2)
        return btn

    def _construir_ui(self):
        contenedor = tk.Frame(self.ventana, bg="#f8f9fa", padx=16, pady=12)
        contenedor.pack(fill="both", expand=True)

        tk.Label(
            contenedor, text="📦 Verificando dependencias",
            font=("Segoe UI", 14, "bold"), bg="#f8f9fa", fg="#2c3e50",
        ).pack(anchor="w")
        tk.Label(
            contenedor, text="Asegurando que todo esté listo para comenzar",
            font=("Segoe UI", 9), bg="#f8f9fa", fg="#7f8c8d",
        ).pack(anchor="w", pady=(0, 10))

        for nombre, _import_name in DEPENDENCIAS:
            self.labels_estado[nombre] = self._fila_estado(contenedor, nombre)

        # Filas extra: requisitos externos que no son paquetes pip.
        self.lbl_estado_ffmpeg = self._fila_estado(contenedor, "FFmpeg")
        self.lbl_estado_deno = self._fila_estado(contenedor, "Deno (JS)")
        self.lbl_estado_firefox = self._fila_estado(contenedor, "Firefox")

        self.progressbar = ttk.Progressbar(contenedor, mode="determinate", length=380)
        self.progressbar.pack(fill="x", pady=(10, 6))

        self.lbl_estado_general = tk.Label(
            contenedor, text="🔍 Iniciando verificación...",
            font=("Segoe UI", 9), bg="#f8f9fa", fg="#2c3e50", anchor="w",
            justify="left", wraplength=368,
        )
        self.lbl_estado_general.pack(fill="x", pady=(0, 10))

        frame_botones = tk.Frame(contenedor, bg="#f8f9fa")
        frame_botones.pack(fill="x")

        self.btn_iniciar = tk.Button(
            frame_botones, text="🚀 Iniciar aplicación", font=("Segoe UI", 10, "bold"),
            bg="#95a5a6", fg="white", state="disabled", relief="flat", padx=10, pady=4,
            command=self._on_iniciar_app,
        )
        self.btn_iniciar.pack(fill="x", pady=(0, 6))

        self.btn_instalar_ffmpeg = self._boton_instalador(
            frame_botones, "🎬 Instalar FFmpeg automáticamente", self._instalar_ffmpeg_manual)
        self.btn_instalar_deno = self._boton_instalador(
            frame_botones, "🧩 Instalar Deno automáticamente", self._instalar_deno_manual)
        self.btn_instalar_firefox = self._boton_instalador(
            frame_botones, "🦊 Instalar Firefox automáticamente", self._instalar_firefox_manual)

    # ------------------------------------------------------------------
    # Verificación (hilo aparte)
    # ------------------------------------------------------------------
    def _iniciar_verificacion_thread(self):
        threading.Thread(target=self._verificar_todo, daemon=True).start()

    def _progreso(self, valor):
        self._programar(0, lambda: self._config_seguro(self.progressbar, value=valor))

    def _verificar_todo(self):
        try:
            self._verificar_todo_interno()
        except Exception:
            log.exception("Error inesperado en la verificación de dependencias")
            self._general("⚠️ Error inesperado al verificar. Podés intentar iniciar igual.")
            self._programar(0, self._habilitar_boton_iniciar)

    def _verificar_todo_interno(self):
        total = len(DEPENDENCIAS) + 3  # + ffmpeg, deno, firefox
        for idx, (nombre, import_name) in enumerate(DEPENDENCIAS):
            lbl = self.labels_estado[nombre]
            self._general(f"🔍 Verificando {nombre}...")
            especificacion = PIP_ESPECIFICACION.get(nombre, nombre)

            if _paquete_instalado(import_name):
                texto_ok = "✅ Instalado"
                if nombre == "yt-dlp":
                    dias = antiguedad_ytdlp_dias()
                    if dias is not None and dias > DIAS_MAX_YTDLP:
                        self._estado(lbl, "⬆️ Actualizando...", "#e67e22")
                        self._general("⬆️ Actualizando yt-dlp (YouTube cambia seguido)...")
                        if _pip_install(especificacion, actualizar=True):
                            texto_ok = "✅ Actualizado"
                        else:
                            texto_ok = "✅ Instalado (sin actualizar)"
                self._estado(lbl, texto_ok, "#27ae60")
            else:
                self._estado(lbl, "⏳ Instalando...", "#e67e22")
                self._general(f"📦 Instalando {nombre}...")
                if _pip_install(especificacion) and _paquete_instalado(import_name):
                    self._estado(lbl, "✅ Instalado", "#27ae60")
                else:
                    self._estado(lbl, "❌ Error", "#e74c3c")
            self._progreso(((idx + 1) / total) * 100)

        # FFmpeg (requisito extra, no es pip)
        self._general("🔍 Verificando FFmpeg...")
        ffmpeg_ok = ruta_ffmpeg_ejecutable() is not None
        self._aplicar_estado_externo(
            ffmpeg_ok, self.lbl_estado_ffmpeg, self.btn_instalar_ffmpeg, "❌ No encontrado")
        self._progreso(((len(DEPENDENCIAS) + 1) / total) * 100)

        # Deno (runtime JS que yt-dlp usa para YouTube)
        self._general("🔍 Verificando Deno...")
        deno_ok = ruta_deno_ejecutable() is not None
        if not ES_WINDOWS and not deno_ok:
            self._estado(self.lbl_estado_deno, "⚠️ Instalar a mano", "#e67e22")
        else:
            self._aplicar_estado_externo(
                deno_ok, self.lbl_estado_deno, self.btn_instalar_deno, "⚠️ No encontrado")
        self._progreso(((len(DEPENDENCIAS) + 2) / total) * 100)

        # Firefox (opcional: cookies para reducir bloqueos)
        self._general("🔍 Verificando Firefox...")
        firefox_ok = ruta_firefox_ejecutable() is not None
        self._aplicar_estado_externo(
            firefox_ok, self.lbl_estado_firefox, self.btn_instalar_firefox, "⚠️ No encontrado")
        self._progreso(100)

        pip_ok = all(_paquete_instalado(imp) for _, imp in DEPENDENCIAS)
        self._criticos_ok = pip_ok and ffmpeg_ok

        faltan = []
        if not pip_ok:
            faltan.append("alguna librería falló (revisá la lista)")
        if not ffmpeg_ok:
            faltan.append("FFmpeg es obligatorio para convertir a mp3")
        recomendados = []
        if not deno_ok:
            recomendados.append("Deno (sin él YouTube puede fallar)")
        if not firefox_ok:
            recomendados.append("Firefox (reduce los bloqueos)")

        if faltan:
            resumen = "⚠️ " + "; ".join(faltan) + "."
        elif recomendados:
            resumen = "⚠️ Falta recomendado: " + ", ".join(recomendados) + ". Podés iniciar igual."
        else:
            resumen = "✅ Todo listo. Podés iniciar la aplicación."
        self._general(resumen)
        self._programar(0, self._habilitar_boton_iniciar)

    def _aplicar_estado_externo(self, ok, label, boton, texto_faltante):
        if ok:
            self._estado(label, "✅ Instalado", "#27ae60")
            self._programar(0, lambda: self._config_seguro(boton, state="disabled", bg="#95a5a6"))
        else:
            self._estado(label, texto_faltante, "#e74c3c")
            self._programar(0, lambda: self._config_seguro(boton, state="normal", bg="#2980b9"))

    def _habilitar_boton_iniciar(self):
        self._config_seguro(self.btn_iniciar, state="normal", bg="#27ae60")

    # ------------------------------------------------------------------
    # Instaladores automáticos (FFmpeg / Deno / Firefox)
    # ------------------------------------------------------------------
    def _resultado_instalacion(self, ok, label, boton, texto_boton, texto_reintento):
        if ok:
            self._estado(label, "✅ Instalado", "#27ae60")
            self._programar(0, lambda: self._config_seguro(
                boton, text=texto_boton, state="disabled", bg="#95a5a6"))
        else:
            self._estado(label, "❌ Error al instalar", "#e74c3c")
            self._programar(0, lambda: self._config_seguro(
                boton, text=texto_reintento, state="normal", bg="#2980b9"))

    def _cb_progreso_mb(self, label):
        ultimo = {"mb": -1}

        def cb(mb):
            if int(mb) != ultimo["mb"]:  # actualiza cada MB, no en cada bloque
                ultimo["mb"] = int(mb)
                self._estado(label, f"⏳ Descargando {int(mb)} MB...", "#e67e22")
        return cb

    # ---- FFmpeg ----
    def _instalar_ffmpeg_manual(self):
        self.btn_instalar_ffmpeg.config(state="disabled", text="⏳ Instalando FFmpeg...")
        self.lbl_estado_ffmpeg.config(text="⏳ Descargando...", fg="#e67e22")
        threading.Thread(target=self._descargar_ffmpeg_thread, daemon=True).start()

    def _descargar_ffmpeg_thread(self):
        ok = False
        tmp_zip = os.path.join(tempfile.gettempdir(), "ffmpeg_albumes.zip")
        try:
            os.makedirs(RUTA_FFMPEG_DIR, exist_ok=True)
            _descargar_archivo(URL_FFMPEG, tmp_zip, self._cb_progreso_mb(self.lbl_estado_ffmpeg))
            self._estado(self.lbl_estado_ffmpeg, "⏳ Descomprimiendo...", "#e67e22")
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(RUTA_FFMPEG_DIR)
            # El zip trae una subcarpeta ffmpeg-X.X-essentials_build/ con bin/.
            for nombre in os.listdir(RUTA_FFMPEG_DIR):
                sub = os.path.join(RUTA_FFMPEG_DIR, nombre)
                if os.path.isdir(sub) and nombre.lower().startswith("ffmpeg") \
                        and os.path.isdir(os.path.join(sub, "bin")):
                    destino_bin = os.path.join(RUTA_FFMPEG_DIR, "bin")
                    if not os.path.isdir(destino_bin):
                        shutil.copytree(os.path.join(sub, "bin"), destino_bin)
                    shutil.rmtree(sub, ignore_errors=True)  # ahorra cientos de MB
                    break
            ok = ruta_ffmpeg_ejecutable() is not None
        except Exception:
            log.exception("Falló la instalación de FFmpeg")
        finally:
            try:
                os.remove(tmp_zip)
            except OSError:
                pass
        self._resultado_instalacion(
            ok, self.lbl_estado_ffmpeg, self.btn_instalar_ffmpeg,
            "🎬 Instalar FFmpeg automáticamente", "🎬 Reintentar instalar FFmpeg")

    # ---- Deno ----
    def _instalar_deno_manual(self):
        self.btn_instalar_deno.config(state="disabled", text="⏳ Instalando Deno...")
        self.lbl_estado_deno.config(text="⏳ Descargando...", fg="#e67e22")
        threading.Thread(target=self._descargar_deno_thread, daemon=True).start()

    def _descargar_deno_thread(self):
        ok = False
        tmp_zip = os.path.join(tempfile.gettempdir(), "deno_albumes.zip")
        try:
            os.makedirs(RUTA_DENO_DIR, exist_ok=True)
            _descargar_archivo(URL_DENO, tmp_zip, self._cb_progreso_mb(self.lbl_estado_deno))
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(RUTA_DENO_DIR)
            asegurar_deno_en_path()
            ok = ruta_deno_ejecutable() is not None
        except Exception:
            log.exception("Falló la instalación de Deno")
        finally:
            try:
                os.remove(tmp_zip)
            except OSError:
                pass
        self._resultado_instalacion(
            ok, self.lbl_estado_deno, self.btn_instalar_deno,
            "🧩 Instalar Deno automáticamente", "🧩 Reintentar instalar Deno")

    # ---- Firefox ----
    def _instalar_firefox_manual(self):
        self.btn_instalar_firefox.config(state="disabled", text="⏳ Instalando Firefox...")
        self.lbl_estado_firefox.config(text="⏳ Descargando...", fg="#e67e22")
        threading.Thread(target=self._descargar_firefox_thread, daemon=True).start()

    def _descargar_firefox_thread(self):
        ok = False
        ruta_instalador = os.path.join(tempfile.gettempdir(), "firefox_installer.exe")
        try:
            # Instalador oficial de Mozilla (completo). /S = silencioso. Puede
            # pedir permiso de administrador (aparece el aviso de Windows).
            _descargar_archivo(URL_FIREFOX, ruta_instalador, self._cb_progreso_mb(self.lbl_estado_firefox))
            self._estado(self.lbl_estado_firefox, "⏳ Instalando...", "#e67e22")
            subprocess.run([ruta_instalador, "/S"], timeout=600, creationflags=CREATIONFLAGS,
                           check=False, stdin=subprocess.DEVNULL)
            ok = ruta_firefox_ejecutable() is not None
        except Exception:
            log.exception("Falló la instalación de Firefox")
        finally:
            try:
                os.remove(ruta_instalador)
            except OSError:
                pass
        self._resultado_instalacion(
            ok, self.lbl_estado_firefox, self.btn_instalar_firefox,
            "🦊 Instalar Firefox automáticamente", "🦊 Reintentar instalar Firefox")

    # ------------------------------------------------------------------
    # Cierre / continuar
    # ------------------------------------------------------------------
    def _on_iniciar_app(self):
        if not self._criticos_ok:
            seguir = messagebox.askyesno(
                "Faltan componentes",
                "Falta algo obligatorio (alguna librería o FFmpeg), así que las descargas "
                "van a fallar.\n\n¿Querés iniciar la aplicación igual?",
                parent=self.ventana,
            )
            if not seguir:
                return
        self._cancelar_pendientes()
        callback = self.on_continuar
        config = self.config_data
        self.ventana.quit()
        self.ventana.destroy()
        if callback:
            callback(config)

    def _on_cerrar(self):
        self._cancelar_pendientes()
        sys.exit(0)

    def iniciar(self):
        self.ventana.mainloop()


if __name__ == "__main__":
    VentanaVerificacion().iniciar()
