# -*- coding: utf-8 -*-
"""
Punto de entrada de Descargar_Albunes.
Todo el código (ventanas, búsqueda, descarga) vive dentro de la carpeta
"nucleo1", al lado de este archivo. Se ejecuta con "py -3.14 descargar_albunes.py" (o con
Iniciar.bat, que usa pythonw).

Muestra primero la ventana de verificación de dependencias y, recién al
continuar, importa y abre la ventana principal. Esa importación es perezosa
a propósito: la ventana principal necesita yt-dlp, que justamente puede estar
sin instalar en una PC nueva.

Cualquier error inesperado queda registrado en config/app.log (con pythonw
no hay consola donde verlo) y se muestra en un cuadro de diálogo.
"""

import logging
import os
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler

RAIZ = os.path.dirname(os.path.abspath(__file__))
RUTA_NUCLEO = os.path.join(RAIZ, "nucleo1")
RUTA_LOG = os.path.join(RAIZ, "config", "app.log")

if RUTA_NUCLEO not in sys.path:
    sys.path.insert(0, RUTA_NUCLEO)


def _ocultar_consola_windows():
    """Si se ejecutó con python.exe en Windows, oculta la consola negra."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _configurar_logging():
    logger = logging.getLogger("albumes")
    logger.setLevel(logging.INFO)
    try:
        os.makedirs(os.path.dirname(RUTA_LOG), exist_ok=True)
        handler = RotatingFileHandler(RUTA_LOG, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
    except Exception:
        pass
    return logger


log = _configurar_logging()


def _mostrar_error_fatal(texto):
    try:
        import tkinter as tk
        from tkinter import messagebox
        raiz = tk.Tk()
        raiz.withdraw()
        raiz.attributes("-topmost", True)
        messagebox.showerror(
            "Descargar Álbumes - error",
            f"{texto}\n\nEl detalle quedó guardado en:\n{RUTA_LOG}",
            parent=raiz,
        )
        raiz.destroy()
    except Exception:
        pass


def _excepthook(tipo, valor, tb):
    log.critical("Excepción no controlada", exc_info=(tipo, valor, tb))
    if not issubclass(tipo, (SystemExit, KeyboardInterrupt)):
        _mostrar_error_fatal(f"Ocurrió un error inesperado:\n{valor}")


def _thread_excepthook(args):
    log.error("Excepción en hilo %s", getattr(args.thread, "name", "?"),
              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _instalar_hooks():
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    try:
        import tkinter as tk

        def _callback_tk(self, exc, val, tb):
            log.error("Error en un callback de Tk", exc_info=(exc, val, tb))

        tk.Tk.report_callback_exception = _callback_tk
    except Exception:
        pass


def main():
    _ocultar_consola_windows()
    _instalar_hooks()
    log.info("=== Inicio de la aplicación (Python %s) ===", sys.version.split()[0])

    from ventana_dependencias import VentanaVerificacion

    def continuar(config_data):
        try:
            from ventana_principal import abrir_ventana_principal
        except Exception as exc:
            log.critical("No se pudo cargar la ventana principal", exc_info=True)
            _mostrar_error_fatal(
                "No se pudo iniciar la aplicación.\n"
                f"Falta o falla una dependencia: {exc}\n"
                "Volvé a abrir el programa para que la verificación la instale."
            )
            return
        abrir_ventana_principal(config_data)

    VentanaVerificacion(on_continuar=continuar).iniciar()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log.critical("Fallo al iniciar", exc_info=True)
        _mostrar_error_fatal("Falló el inicio de la aplicación:\n" + traceback.format_exc(limit=2))
