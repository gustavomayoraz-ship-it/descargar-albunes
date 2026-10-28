# -*- coding: utf-8 -*-
"""
Ventana principal de Descargar_Albunes:
  1) Buscar un artista -> se listan los álbumes/playlists encontrados en YouTube.
  2) Al hacer click sobre un álbum se muestra su tracklist real; doble click en
     un tema lo abre en el navegador para escucharlo, sin descargar nada.
  3) El usuario tilda uno, varios o todos los álbumes.
  4) Al descargar: por cada tema se baja el video EXACTO de la playlist (rápido
     y sin errores de coincidencia). Opcionalmente se busca "la mejor versión"
     en YouTube, y esa búsqueda también es el respaldo si el video de la
     playlist no está disponible. Cada tema se valida (tamaño/duración), se
     reintenta si falla, se calcula el BPM, se taggea y se renombra.
  5) Se puede volver a tocar "Descargar": lo que ya está en la carpeta se
     omite y solo se completa lo que falta.
"""

import logging
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import anti_bloqueo
from buscador_youtube import buscar_mejor_resultado, buscar_playlists_artista, obtener_tracklist_playlist
from descargador import procesar_tema
from utils import (
    escribir_json_atomico, leer_json_seguro, ruta_carpeta_album, ruta_descargas_base,
    sanitizar_nombre, tema_ya_descargado,
)
from ventana_dependencias import (
    RUTA_CONFIG_DIR, guardar_config_app, ruta_ffmpeg_ejecutable,
)

log = logging.getLogger("albumes.principal")

MAX_HISTORIAL_BUSQUEDAS = 20
MAX_INTENTOS = 3
RUTA_SESION = os.path.join(RUTA_CONFIG_DIR, "sesion_pendiente.json")
MAX_LINEAS_LOG = 3000


# Etapas visibles en la lista de temas: nombre -> (icono, color de fondo)
ETAPAS = {
    "En cola": ("⌛", "#475569"),
    "Buscando versión": ("🔎", "#0369a1"),
    "Descargando": ("⬇️", "#0e7490"),
    "Validando": ("🛡️", "#b45309"),
    "Normalizando volumen": ("🔊", "#0e7490"),
    "Analizando BPM": ("🎵", "#6d28d9"),
    "Etiquetas": ("🏷️", "#a21caf"),
    "Renombrando": ("✏️", "#be185d"),
    "Reintentando": ("🔁", "#c2410c"),
}
COLOR_OK = "#0b7a4b"
COLOR_ERROR = "#b91c1c"
COLOR_OMITIDO = "#334155"


# ----------------------------------------------------------------------
# Tema visual (oscuro, con acentos violeta/cian)
# ----------------------------------------------------------------------
T = {
    "fondo": "#0b0e22", "panel": "#12163a", "card": "#171c47", "card2": "#1f2557",
    "card_sel": "#2b2f78", "borde": "#2a3070",
    "texto": "#eef0ff", "suave": "#8f97d1", "tenue": "#5d65a3",
    "acento": "#7c3aed", "acento2": "#06b6d4", "acento_hover": "#8b5cf6",
    "ok": "#10b981", "ok_hover": "#34d399", "stop": "#ef4444", "stop_hover": "#f87171",
    "off": "#3a4079", "error": "#fb7185", "aviso": "#fbbf24",
}
FUENTE = "Segoe UI"


def _rgb(hex_):
    hex_ = hex_.lstrip("#")
    return tuple(int(hex_[i:i + 2], 16) for i in (0, 2, 4))


def _mezclar(c1, c2, t):
    a, b = _rgb(c1), _rgb(c2)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class _Abortar(Exception):
    """Demasiados bloqueos seguidos: se frena toda la descarga."""


class VentanaPrincipal:
    def __init__(self, config_data):
        self.config_data = config_data
        self.ffmpeg_path_dir = self._carpeta_ffmpeg()

        self.albumes_encontrados = []  # dicts {titulo, url, id, cantidad, var, _fila, _label}
        self._temas_por_item = {}      # iid del Treeview -> dict tema
        self._fila_resaltada = None
        self._pending_after_ids = []
        self._descargando = False
        self._cancelar_descarga = threading.Event()
        self._ultimo_sleep_informado = None
        self._gen_busqueda = 0         # descarta respuestas de búsquedas viejas
        self._gen_tracklist = 0        # descarta tracklists de clicks viejos
        self._sesion_terminada = False  # True si la última tanda llegó hasta el final
        self._cola_estado = {}         # fila de la lista -> estado visible del tema en proceso

        self.ventana = tk.Tk()
        self.ventana.title("🎵 Descargar Álbumes")
        self.ventana.configure(bg=T["fondo"])
        self.ventana.geometry("1120x900")
        self.ventana.minsize(900, 700)

        self._construir_ui()
        self.ventana.protocol("WM_DELETE_WINDOW", self._on_cerrar)
        self._programar(400, self._ofrecer_continuar)

    @staticmethod
    def _carpeta_ffmpeg():
        ruta = ruta_ffmpeg_ejecutable()
        return os.path.dirname(ruta) if ruta else None

    # ------------------------------------------------------------------
    def _programar(self, ms, fn):
        try:
            id_ = self.ventana.after(ms, fn)
        except (RuntimeError, tk.TclError):
            return None  # la ventana ya se cerró
        self._pending_after_ids.append(id_)
        if len(self._pending_after_ids) > 500:
            self._pending_after_ids = self._pending_after_ids[-200:]
        return id_

    def _cancelar_pendientes(self):
        for id_ in self._pending_after_ids:
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

    # ------------------------------------------------------------------
    def _boton(self, padre, texto, comando, color, color_hover, **kw):
        """Botón plano con efecto al pasar el mouse."""
        b = tk.Button(
            padre, text=texto, command=comando, bg=color, fg="white", relief="flat", bd=0,
            font=(FUENTE, 10, "bold"), cursor="hand2", activebackground=color_hover,
            activeforeground="white", padx=16, pady=7, **kw,
        )
        estado = {"orig": None}

        def entrar(_e):
            if str(b["state"]) == "normal" and estado["orig"] is None:
                estado["orig"] = b["bg"]
                b.config(bg=_mezclar(b["bg"], "#ffffff", 0.16))

        def salir(_e):
            if estado["orig"] is not None:
                if str(b["state"]) == "normal":
                    b.config(bg=estado["orig"])
                estado["orig"] = None

        b.bind("<Enter>", entrar)
        b.bind("<Leave>", salir)
        return b

    def _estilos(self):
        st = ttk.Style(self.ventana)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Dark.Treeview", background=T["card"], fieldbackground=T["card"],
                     foreground=T["texto"], rowheight=28, borderwidth=0, font=(FUENTE, 10))
        st.map("Dark.Treeview", background=[("selected", T["card_sel"])],
               foreground=[("selected", "white")])
        st.configure("Dark.Treeview.Heading", background=T["card2"], foreground=T["suave"],
                     relief="flat", font=(FUENTE, 9, "bold"), borderwidth=0)
        st.map("Dark.Treeview.Heading", background=[("active", T["card_sel"])])
        st.configure("Dark.Vertical.TScrollbar", background=T["card2"], troughcolor=T["card"],
                     bordercolor=T["card"], arrowcolor=T["suave"], lightcolor=T["card2"],
                     darkcolor=T["card2"], relief="flat")
        st.map("Dark.Vertical.TScrollbar", background=[("active", T["acento"])])
        st.configure("Neon.Horizontal.TProgressbar", troughcolor=T["card"], background=T["acento2"],
                     bordercolor=T["panel"], lightcolor=T["acento2"], darkcolor=T["acento"], thickness=10)
        st.configure("Dark.TCombobox", fieldbackground=T["card2"], background=T["card2"],
                     foreground=T["texto"], arrowcolor=T["acento2"], bordercolor=T["borde"],
                     lightcolor=T["card2"], darkcolor=T["card2"], insertcolor=T["texto"],
                     selectbackground=T["card2"], selectforeground=T["texto"])
        st.map("Dark.TCombobox", fieldbackground=[("readonly", T["card2"])],
               foreground=[("readonly", T["texto"])])
        self.ventana.option_add("*TCombobox*Listbox.background", T["card2"])
        self.ventana.option_add("*TCombobox*Listbox.foreground", T["texto"])
        self.ventana.option_add("*TCombobox*Listbox.selectBackground", T["acento"])
        self.ventana.option_add("*TCombobox*Listbox.font", (FUENTE, 10))

    def _chip(self, padre, titulo, variable, color):
        f = tk.Frame(padre, bg=T["fondo"], padx=12, pady=5, highlightthickness=1,
                     highlightbackground=color)
        tk.Label(f, textvariable=variable, font=(FUENTE, 15, "bold"), bg=T["fondo"], fg=color).pack()
        tk.Label(f, text=titulo, font=(FUENTE, 7, "bold"), bg=T["fondo"], fg=T["suave"]).pack()
        return f

    def _tarjeta(self, padre, titulo, icono):
        """Panel oscuro con barra de título. Devuelve (marco_externo, cuerpo)."""
        ext = tk.Frame(padre, bg=T["borde"], padx=1, pady=1)
        interior = tk.Frame(ext, bg=T["card"])
        interior.pack(fill="both", expand=True)
        cab = tk.Frame(interior, bg=T["card2"])
        cab.pack(fill="x")
        tk.Frame(cab, bg=T["acento2"], width=4).pack(side="left", fill="y")
        lbl = tk.Label(cab, text=f"{icono}  {titulo}", font=(FUENTE, 10, "bold"), bg=T["card2"],
                       fg=T["texto"], anchor="w", padx=10, pady=7)
        lbl.pack(side="left", fill="x", expand=True)
        cuerpo = tk.Frame(interior, bg=T["card"])
        cuerpo.pack(fill="both", expand=True)
        return ext, cuerpo, lbl

    def _construir_ui(self):
        self._estilos()
        self.ventana.configure(bg=T["fondo"])

        # estadísticas del encabezado
        self.var_stat_total = tk.StringVar(value="0")
        self.var_stat_ok = tk.StringVar(value="0")
        self.var_stat_omit = tk.StringVar(value="0")
        self.var_stat_err = tk.StringVar(value="0")
        self._stats = {"total": 0, "ok": 0, "omit": 0, "err": 0}

        # ---- encabezado con degradado + ecualizador ----
        self.cab = tk.Canvas(self.ventana, height=104, highlightthickness=0, bg=T["fondo"])
        self.cab.pack(fill="x")
        self._barras = []
        self._eq_fase = 0.0
        self._eq_corriendo = False
        self.cab.create_text(28, 38, anchor="w", text="Descargar Álbumes", fill="white",
                             font=(FUENTE, 24, "bold"), tags="titulo")
        self.cab.create_text(30, 70, anchor="w", fill="#dcd6ff", tags="subtitulo",
                             text="Buscá un artista · escuchá los temas · bajá discos completos en máxima calidad",
                             font=(FUENTE, 10))
        marco_chips = tk.Frame(self.cab, bg=T["fondo"])
        self._chip(marco_chips, "TEMAS", self.var_stat_total, T["acento2"]).pack(side="left", padx=4)
        self._chip(marco_chips, "LISTOS", self.var_stat_ok, T["ok"]).pack(side="left", padx=4)
        self._chip(marco_chips, "OMITIDOS", self.var_stat_omit, T["aviso"]).pack(side="left", padx=4)
        self._chip(marco_chips, "ERRORES", self.var_stat_err, T["error"]).pack(side="left", padx=4)
        self._ventana_chips = self.cab.create_window(0, 40, window=marco_chips, anchor="e", tags="chips")
        self.cab.bind("<Configure>", self._redibujar_cabecera)

        contenedor = tk.Frame(self.ventana, bg=T["fondo"], padx=18, pady=12)
        contenedor.pack(fill="both", expand=True)

        # ---- búsqueda ----
        fila_busqueda = tk.Frame(contenedor, bg=T["fondo"])
        fila_busqueda.pack(fill="x", pady=(0, 10))
        marco_entrada = tk.Frame(fila_busqueda, bg=T["acento"], padx=2, pady=2)
        marco_entrada.pack(side="left", fill="x", expand=True)
        tk.Label(marco_entrada, text=" 🔍", bg=T["card2"], fg=T["acento2"], font=(FUENTE, 13)).pack(
            side="left", fill="y")
        self.var_busqueda = tk.StringVar()
        self.combo_busqueda = ttk.Combobox(
            marco_entrada, textvariable=self.var_busqueda, font=(FUENTE, 13), style="Dark.TCombobox",
            values=self.config_data.get("historial_busquedas", []),
        )
        self.combo_busqueda.pack(side="left", fill="x", expand=True, ipady=6)
        self.combo_busqueda.bind("<Return>", lambda e: self._on_buscar())
        self.combo_busqueda.bind("<<ComboboxSelected>>", lambda e: self._on_buscar())
        self.btn_buscar = self._boton(fila_busqueda, "Buscar", self._on_buscar, T["acento"], T["acento_hover"])
        self.btn_buscar.config(padx=26, pady=10, font=(FUENTE, 11, "bold"))
        self.btn_buscar.pack(side="left", padx=(10, 0))

        fila_seleccion = tk.Frame(contenedor, bg=T["fondo"])
        fila_seleccion.pack(fill="x", pady=(0, 6))
        self.var_seleccionar_todos = tk.BooleanVar(value=False)
        tk.Checkbutton(
            fila_seleccion, text="Seleccionar todos", variable=self.var_seleccionar_todos,
            bg=T["fondo"], fg=T["texto"], selectcolor=T["card2"], activebackground=T["fondo"],
            activeforeground=T["texto"], font=(FUENTE, 9, "bold"), bd=0, highlightthickness=0,
            command=self._toggle_seleccionar_todos,
        ).pack(side="left")
        tk.Label(
            fila_seleccion, text="   Click en un álbum para ver sus temas  ·  Doble click en un tema para escucharlo en YouTube",
            bg=T["fondo"], fg=T["tenue"], font=(FUENTE, 8),
        ).pack(side="left")

        # ---- panel dividido: álbumes | temas ----
        paned = tk.PanedWindow(contenedor, orient="horizontal", sashwidth=10, bg=T["fondo"],
                               bd=0, sashrelief="flat")
        paned.pack(fill="both", expand=True, pady=(0, 10))

        marco_lista, cuerpo_albumes, _ = self._tarjeta(paned, "Álbumes encontrados", "💿")
        self.canvas = tk.Canvas(cuerpo_albumes, bg=T["card"], highlightthickness=0, yscrollincrement=24)
        scrollbar = ttk.Scrollbar(cuerpo_albumes, orient="vertical", command=self.canvas.yview,
                                  style="Dark.Vertical.TScrollbar")
        self.frame_resultados = tk.Frame(self.canvas, bg=T["card"])
        self.frame_resultados.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self._ventana_resultados = self.canvas.create_window((0, 0), window=self.frame_resultados, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._ventana_resultados, width=e.width))
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        # Rueda del mouse: funciona en toda la zona de álbumes (también sobre cada fila).
        for evento in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.ventana.bind_all(evento, self._rueda_albumes, add="+")

        self.lbl_sin_resultados = tk.Label(
            self.frame_resultados, text="🎧\nBuscá un artista para ver sus álbumes acá.",
            bg=T["card"], fg=T["suave"], font=(FUENTE, 10), padx=12, pady=30, justify="center",
        )
        self.lbl_sin_resultados.pack(fill="x")

        marco_tracklist, cuerpo_temas, self.lbl_panel_derecho = self._tarjeta(paned, "Temas del álbum", "🎼")
        self.tree_tracklist = ttk.Treeview(
            cuerpo_temas, columns=("numero", "titulo"), show="headings", selectmode="browse",
            style="Dark.Treeview",
        )
        self.tree_tracklist.heading("numero", text="#")
        self.tree_tracklist.heading("titulo", text="Tema")
        self.tree_tracklist.column("numero", width=40, anchor="center", stretch=False)
        self.tree_tracklist.column("titulo", width=260, anchor="w")
        sc_temas = ttk.Scrollbar(cuerpo_temas, orient="vertical", command=self.tree_tracklist.yview,
                                 style="Dark.Vertical.TScrollbar")
        self.tree_tracklist.configure(yscrollcommand=sc_temas.set)
        self.lbl_reproduciendo = tk.Label(
            cuerpo_temas, text="▶  Doble click en un tema para abrirlo en YouTube",
            bg=T["card"], fg=T["suave"], font=(FUENTE, 8), anchor="w", wraplength=340, justify="left",
        )
        self.lbl_reproduciendo.pack(side="bottom", fill="x", padx=10, pady=(4, 8))
        sc_temas.pack(side="right", fill="y")
        self.tree_tracklist.pack(side="left", fill="both", expand=True)
        self.tree_tracklist.bind("<Double-1>", self._on_doble_click_tema)

        paned.add(marco_lista, minsize=300, width=480)
        paned.add(marco_tracklist, minsize=300, width=420)

        # ---- opciones ----
        fila_opciones = tk.Frame(contenedor, bg=T["fondo"])
        fila_opciones.pack(fill="x", pady=(0, 8))
        tk.Label(fila_opciones, text="Artista (carpeta y tags)", bg=T["fondo"],
                 font=(FUENTE, 9, "bold"), fg=T["suave"]).pack(side="left")
        marco_art = tk.Frame(fila_opciones, bg=T["borde"], padx=1, pady=1)
        marco_art.pack(side="left", padx=(8, 18))
        self.var_artista_carpeta = tk.StringVar()
        tk.Entry(marco_art, textvariable=self.var_artista_carpeta, width=26, bg=T["card2"],
                 fg=T["texto"], insertbackground=T["texto"], relief="flat",
                 font=(FUENTE, 10)).pack(ipady=4, padx=2)
        self.var_mejor_version = tk.BooleanVar(value=bool(self.config_data.get("buscar_mejor_version", False)))
        self.var_normalizar = tk.BooleanVar(value=bool(self.config_data.get("normalizar_volumen", True)))
        for texto, var in (("🔎  Mejor versión de cada tema (más lento)", self.var_mejor_version),
                           ("🔊  Igualar volumen (-14 LUFS)", self.var_normalizar)):
            tk.Checkbutton(
                fila_opciones, text=texto, variable=var, bg=T["fondo"], fg=T["texto"],
                selectcolor=T["card2"], activebackground=T["fondo"], activeforeground=T["texto"],
                font=(FUENTE, 9), bd=0, highlightthickness=0,
            ).pack(side="left", padx=(0, 14))

        # ---- botones de acción ----
        fila_descarga = tk.Frame(contenedor, bg=T["fondo"])
        fila_descarga.pack(fill="x")
        self.btn_descargar = self._boton(fila_descarga, "⬇  Descargar seleccionados", self._on_descargar,
                                         T["off"], T["ok_hover"], state="disabled")
        self.btn_descargar.config(padx=24, pady=10, font=(FUENTE, 11, "bold"))
        self.btn_descargar.pack(side="left")
        self.btn_detener_descarga = self._boton(fila_descarga, "⏹  Detener", self._on_detener_descarga,
                                                T["off"], T["stop_hover"], state="disabled")
        self.btn_detener_descarga.config(pady=10)
        self.btn_detener_descarga.pack(side="left", padx=(10, 0))
        self.btn_abrir_carpeta = self._boton(fila_descarga, "📂  Abrir carpeta", self._on_abrir_carpeta_descargas,
                                             T["card2"], T["card_sel"])
        self.btn_abrir_carpeta.config(pady=10)
        self.btn_abrir_carpeta.pack(side="right")

        self.progressbar = ttk.Progressbar(contenedor, mode="determinate", length=380,
                                           style="Neon.Horizontal.TProgressbar")
        self.progressbar.pack(fill="x", pady=(12, 4))

        self.lbl_estado = tk.Label(
            contenedor, text="Listo para descargar.", font=(FUENTE, 9), bg=T["fondo"],
            fg=T["suave"], anchor="w",
        )
        self.lbl_estado.pack(fill="x")

        # ---- lista de temas en proceso ----
        marco_cola_ext, cuerpo_cola, _ = self._tarjeta(contenedor, "Temas en proceso · descarga, análisis y arreglos", "📋")
        marco_cola_ext.pack(fill="both", expand=True, pady=(8, 0))
        self.lista_cola = tk.Listbox(
            cuerpo_cola, height=9, font=(FUENTE, 10), activestyle="none",
            bg=T["card"], fg=T["texto"], selectbackground=T["card_sel"], selectforeground="white",
            relief="flat", bd=0, highlightthickness=0,
        )
        scroll_cola = ttk.Scrollbar(cuerpo_cola, orient="vertical", command=self.lista_cola.yview,
                                    style="Dark.Vertical.TScrollbar")
        self.lista_cola.configure(yscrollcommand=scroll_cola.set)
        scroll_cola.pack(side="right", fill="y")
        self.lista_cola.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)

        marco_log = tk.Frame(contenedor, bg=T["fondo"])
        marco_log.pack(fill="x", pady=(8, 0))
        self.texto_log = tk.Text(marco_log, height=4, font=("Consolas", 9), bg="#080a1a", fg="#9fb0e8",
                                 relief="flat", bd=0, padx=8, pady=6, insertbackground="white")
        self.texto_log.pack(fill="x")
        self.texto_log.configure(state="disabled")

    # ---- encabezado: degradado y ecualizador animado ----------------------
    def _redibujar_cabecera(self, _e=None):
        w = max(self.cab.winfo_width(), 200)
        h = int(self.cab["height"])
        self.cab.delete("grad")
        paso = 4
        for x in range(0, w, paso):
            color = _mezclar("#4c1d95", "#0891b2", x / w)
            self.cab.create_rectangle(x, 0, x + paso, h, fill=color, outline="", tags="grad")
        self.cab.tag_lower("grad")
        # barras del ecualizador (borde inferior)
        self.cab.delete("barra")
        self._barras = []
        ancho, sep = 6, 5
        n = w // (ancho + sep)
        for i in range(n):
            x = i * (ancho + sep) + 4
            color = _mezclar(_mezclar("#4c1d95", "#0891b2", x / w), "#ffffff", 0.22)
            item = self.cab.create_rectangle(x, h - 6, x + ancho, h, fill=color, outline="", tags="barra")
            self._barras.append(item)
        self.cab.tag_raise("titulo")
        self.cab.tag_raise("subtitulo")
        self.cab.tag_raise("chips")
        self.cab.coords(self._ventana_chips, w - 22, 52)
        self._eq_pintar()

    def _eq_pintar(self):
        h = int(self.cab["height"])
        n = len(self._barras)
        if not n:
            return
        for i, item in enumerate(self._barras):
            if self._eq_corriendo:
                v = 0.5 + 0.5 * math.sin(self._eq_fase + i * 0.55) * math.sin(self._eq_fase * 0.7 + i * 0.21)
                alto = 4 + abs(v) * 34
            else:
                alto = 4 + 10 * abs(math.sin(i * 0.35)) * (0.4 + 0.6 * math.sin(i * 0.08 + 1) ** 2)
            x1, _, x2, _ = self.cab.coords(item)
            self.cab.coords(item, x1, h - alto, x2, h)

    def _eq_animar(self):
        if not self._descargando:
            self._eq_corriendo = False
            self._eq_pintar()
            return
        self._eq_fase += 0.55
        self._eq_pintar()
        self._programar(90, self._eq_animar)

    def _eq_iniciar(self):
        if not self._eq_corriendo:
            self._eq_corriendo = True
            self._eq_animar()

    def _rueda_albumes(self, event):
        """Mueve la lista de álbumes con la rueda cuando el mouse está sobre ella."""
        try:
            w = self.ventana.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        while w is not None:
            if w is self.canvas or w is self.frame_resultados:
                break
            w = getattr(w, "master", None)
        else:
            return
        if w is None:
            return
        try:
            if self.frame_resultados.winfo_reqheight() <= self.canvas.winfo_height():
                return  # todo entra: no hay nada que mover
            if getattr(event, "num", None) == 4:
                paso = -1
            elif getattr(event, "num", None) == 5:
                paso = 1
            else:
                paso = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(paso * 2, "units")
        except tk.TclError:
            pass

    def _stats_set(self, **cambios):
        self._stats.update(cambios)
        self.var_stat_total.set(str(self._stats["total"]))
        self.var_stat_ok.set(str(self._stats["ok"]))
        self.var_stat_omit.set(str(self._stats["omit"]))
        self.var_stat_err.set(str(self._stats["err"]))

    # ------------------------------------------------------------------
    # Búsqueda de álbumes
    # ------------------------------------------------------------------
    def _on_buscar(self):
        artista = self.var_busqueda.get().strip()
        if not artista or self._descargando:
            return
        self._guardar_en_historial(artista)
        self.var_artista_carpeta.set(artista)  # editable antes de descargar
        self._gen_busqueda += 1
        gen = self._gen_busqueda
        self.btn_buscar.config(state="disabled", text="Buscando…")
        self.btn_descargar.config(state="disabled", bg=T["off"])
        self._log(f"Buscando álbumes de: {artista}")
        threading.Thread(target=self._buscar_thread, args=(artista, gen), daemon=True).start()

    def _guardar_config(self):
        try:
            guardar_config_app(self.config_data)
        except Exception:
            log.warning("No se pudo guardar la configuración", exc_info=True)

    def _guardar_en_historial(self, artista):
        """Agrega la búsqueda al historial (más reciente primero, sin duplicados)."""
        historial = self.config_data.setdefault("historial_busquedas", [])
        historial[:] = [h for h in historial if h.lower() != artista.lower()]
        historial.insert(0, artista)
        del historial[MAX_HISTORIAL_BUSQUEDAS:]
        self._guardar_config()
        self.combo_busqueda["values"] = historial

    def _buscar_thread(self, artista, gen):
        error = None
        resultados = []
        try:
            resultados = buscar_playlists_artista(artista)
        except Exception as exc:
            error = str(exc)
            log.warning("Falló la búsqueda de '%s': %s", artista, error)
        self._programar(0, lambda: self._mostrar_resultados(resultados, error, gen))

    def _mostrar_resultados(self, resultados, error, gen):
        if gen != self._gen_busqueda:
            return  # llegó una respuesta de una búsqueda vieja
        for widget in self.frame_resultados.winfo_children():
            widget.destroy()
        self.albumes_encontrados = []
        self._fila_resaltada = None
        self._limpiar_tracklist()

        if error:
            tk.Label(
                self.frame_resultados, text="No se pudo buscar (ver el registro de abajo).",
                bg=T["card"], fg=T["error"], font=(FUENTE, 10), padx=12, pady=12,
            ).pack(anchor="w")
            msg = f"❌ Error al buscar: {error[:300]}"
            if anti_bloqueo.es_error_de_bloqueo(error):
                msg += "\n   ↳ YouTube parece estar limitando las consultas. Esperá unos minutos y reintentá."
            self._log(msg)
        elif not resultados:
            tk.Label(
                self.frame_resultados, text="No se encontraron álbumes/playlists para ese artista.",
                bg=T["card"], fg=T["suave"], font=(FUENTE, 10), padx=12, pady=12,
            ).pack(anchor="w")
        else:
            for item in resultados:
                var = tk.BooleanVar(value=False)
                entry = {**item, "var": var}
                fila = tk.Frame(self.frame_resultados, bg=T["card"], highlightthickness=1,
                                highlightbackground=T["borde"])
                fila.pack(fill="x", padx=8, pady=3)
                tk.Checkbutton(
                    fila, variable=var, bg=T["card"], selectcolor=T["card2"], activebackground=T["card"],
                    bd=0, highlightthickness=0, command=self._actualizar_boton_descargar,
                ).pack(side="left", padx=(4, 0))
                texto = item["titulo"]
                if item.get("cantidad"):
                    texto += f"  ({item['cantidad']} temas)"
                lbl = tk.Label(
                    fila, text=texto, bg=T["card"], fg=T["texto"], font=(FUENTE, 10), anchor="w",
                    wraplength=330, justify="left", cursor="hand2", pady=8,
                )
                lbl.pack(side="left", fill="x", expand=True)
                entry["_fila"] = fila
                entry["_label"] = lbl
                lbl.bind("<Button-1>", lambda e, it=entry: self._on_click_album(it))
                fila.bind("<Button-1>", lambda e, it=entry: self._on_click_album(it))
                self.albumes_encontrados.append(entry)

        self.btn_buscar.config(state="normal", text="Buscar")
        if not error:
            self._log(f"Se encontraron {len(resultados)} álbum(es)/playlist(s).")
        self._actualizar_boton_descargar()

    def _toggle_seleccionar_todos(self):
        marcado = self.var_seleccionar_todos.get()
        for item in self.albumes_encontrados:
            item["var"].set(marcado)
        self._actualizar_boton_descargar()

    def _actualizar_boton_descargar(self):
        hay_seleccion = any(item["var"].get() for item in self.albumes_encontrados)
        if hay_seleccion and not self._descargando:
            self.btn_descargar.config(state="normal", bg=T["ok"])
        else:
            self.btn_descargar.config(state="disabled", bg=T["off"])

    # ------------------------------------------------------------------
    # Tracklist del álbum clickeado + preview con doble click
    # ------------------------------------------------------------------
    def _on_click_album(self, item):
        if self._fila_resaltada is not None:
            self._config_seguro(self._fila_resaltada, bg=T["card"], highlightbackground=T["borde"])
            for hijo in self._fila_resaltada.winfo_children():
                if isinstance(hijo, tk.Label):
                    self._config_seguro(hijo, bg=T["card"])
        item["_fila"].config(bg=T["card_sel"], highlightbackground=T["acento2"])
        item["_label"].config(bg=T["card_sel"])
        for hijo in item["_fila"].winfo_children():
            if isinstance(hijo, tk.Checkbutton):
                hijo.config(bg=T["card_sel"], activebackground=T["card_sel"])
        self._fila_resaltada = item["_fila"]

        self._gen_tracklist += 1
        gen = self._gen_tracklist
        self.lbl_panel_derecho.config(text=f"Temas de: {item['titulo']}\n⏳ Cargando...")
        self.tree_tracklist.delete(*self.tree_tracklist.get_children())
        self._temas_por_item = {}
        threading.Thread(target=self._cargar_tracklist_thread, args=(item, gen), daemon=True).start()

    def _cargar_tracklist_thread(self, item, gen):
        error = None
        temas = []
        try:
            temas = obtener_tracklist_playlist(item["url"])
        except Exception as exc:
            error = str(exc)
            log.warning("Falló la lectura del álbum '%s': %s", item["titulo"], error)
        self._programar(0, lambda: self._llenar_tracklist(item, temas, error, gen))

    def _llenar_tracklist(self, item, temas, error, gen):
        if gen != self._gen_tracklist:
            return  # el usuario ya clickeó otro álbum
        self.tree_tracklist.delete(*self.tree_tracklist.get_children())
        self._temas_por_item = {}
        for tema in temas:
            titulo = tema["titulo"] if tema.get("disponible") else f"{tema['titulo']}  (no disponible)"
            iid = self.tree_tracklist.insert("", "end", values=(tema["numero"], titulo))
            self._temas_por_item[iid] = tema
        if error:
            self._log(f"❌ Error leyendo el álbum: {error[:300]}")
            cantidad_txt = "(no se pudo leer el contenido)"
        else:
            cantidad_txt = f"({len(temas)} temas)"
        self.lbl_panel_derecho.config(text=f"Temas de: {item['titulo']} {cantidad_txt}")

    def _limpiar_tracklist(self):
        self._gen_tracklist += 1
        self.tree_tracklist.delete(*self.tree_tracklist.get_children())
        self._temas_por_item = {}
        self.lbl_panel_derecho.config(text="Temas del álbum")

    def _on_doble_click_tema(self, event):
        iid = self.tree_tracklist.identify_row(event.y)
        if not iid:
            return
        tema = self._temas_por_item.get(iid)
        if not tema or not tema.get("url"):
            return
        try:
            webbrowser.open(tema["url"])
            self.lbl_reproduciendo.config(text=f"🌐 Abierto en YouTube: {tema['titulo']}")
        except Exception as exc:
            self.lbl_reproduciendo.config(text=f"❌ No se pudo abrir: {exc}")

    # ------------------------------------------------------------------
    # Descarga
    # ------------------------------------------------------------------
    def _on_descargar(self):
        seleccionados = [item for item in self.albumes_encontrados if item["var"].get()]
        if not seleccionados or self._descargando:
            return
        artista = self.var_artista_carpeta.get().strip() or self.var_busqueda.get().strip()
        if not artista:
            messagebox.showwarning("Falta el artista", "Escribí el nombre del artista.", parent=self.ventana)
            return
        mejor_version = bool(self.var_mejor_version.get())
        self.config_data["buscar_mejor_version"] = mejor_version
        self.config_data["normalizar_volumen"] = bool(self.var_normalizar.get())
        self._guardar_config()
        self._iniciar_descarga(seleccionados, artista, mejor_version)

    # ---- sesión pendiente: retomar si el programa se cerró a medias ------
    def _guardar_sesion(self, seleccionados, artista, mejor_version):
        try:
            escribir_json_atomico(RUTA_SESION, {
                "artista": artista,
                "mejor_version": mejor_version,
                "albumes": [{k: a.get(k) for k in ("titulo", "url", "id", "cantidad")} for a in seleccionados],
            })
        except Exception:
            log.warning("No se pudo guardar la sesión pendiente", exc_info=True)

    @staticmethod
    def _borrar_sesion():
        try:
            os.remove(RUTA_SESION)
        except OSError:
            pass

    def _ofrecer_continuar(self):
        if self._descargando:
            return
        datos = leer_json_seguro(RUTA_SESION, None)
        if not isinstance(datos, dict) or not datos.get("albumes") or not datos.get("artista"):
            self._borrar_sesion()
            return
        albumes = [a for a in datos["albumes"] if isinstance(a, dict) and a.get("url")]
        if not albumes:
            self._borrar_sesion()
            return
        nombres = "\n".join(f"  • {a.get('titulo', '?')}" for a in albumes[:8])
        if len(albumes) > 8:
            nombres += f"\n  … y {len(albumes) - 8} más"
        seguir = messagebox.askyesno(
            "Descarga sin terminar",
            f"La última vez quedó una descarga sin terminar de {datos['artista']}:\n\n{nombres}\n\n"
            "¿Querés continuar donde quedó?\n(Lo que ya se descargó se omite.)",
            parent=self.ventana,
        )
        if not seguir:
            self._borrar_sesion()
            return
        self.var_artista_carpeta.set(datos["artista"])
        self.var_busqueda.set(datos["artista"])
        self.var_mejor_version.set(bool(datos.get("mejor_version")))
        self._iniciar_descarga(albumes, datos["artista"], bool(datos.get("mejor_version")))

    def _iniciar_descarga(self, seleccionados, artista, mejor_version):
        self._guardar_sesion(seleccionados, artista, mejor_version)
        self._sesion_terminada = False
        self._descargando = True
        self._eq_iniciar()
        self._cancelar_descarga.clear()
        self.btn_descargar.config(state="disabled", bg=T["off"], text="⏳  Descargando...")
        self.btn_detener_descarga.config(state="normal", bg=T["stop"], text="⏹  Detener")
        self.btn_buscar.config(state="disabled")
        self.progressbar.config(value=0)
        threading.Thread(
            target=self._descargar_thread, args=(seleccionados, artista, mejor_version), daemon=True,
        ).start()

    def _on_detener_descarga(self):
        if not self._descargando or self._cancelar_descarga.is_set():
            return
        self._cancelar_descarga.set()
        self.btn_detener_descarga.config(state="disabled", text="⏳ Deteniendo...")
        self._log("⏹ Deteniendo descarga (se corta después del tema actual)...")

    def _on_abrir_carpeta_descargas(self):
        carpeta = ruta_descargas_base()
        try:
            if hasattr(os, "startfile"):
                os.startfile(carpeta)  # Windows
            elif sys.platform == "darwin":
                subprocess.Popen(["open", carpeta])
            else:
                subprocess.Popen(["xdg-open", carpeta])
        except Exception as exc:
            self._log(f"❌ No se pudo abrir la carpeta de descargas: {exc}")

    def _on_cerrar(self):
        if self._descargando:
            seguir = messagebox.askyesno(
                "Descarga en curso",
                "Hay una descarga en curso. Si cerrás ahora se pierde el tema que se está bajando "
                "(lo ya descargado queda guardado y la próxima vez se retoma).\n\n¿Cerrar igual?",
                parent=self.ventana,
            )
            if not seguir:
                return
            self._cancelar_descarga.set()
        self._cancelar_pendientes()
        self.ventana.destroy()

    def _esperar(self, segundos):
        """Espera cancelable: 'Detener descarga' la corta al instante. True si se canceló."""
        return self._cancelar_descarga.wait(timeout=segundos)

    def _esperar_entre_temas(self):
        segundos = anti_bloqueo.sleep_interval_actual()
        if segundos != self._ultimo_sleep_informado:
            self._ultimo_sleep_informado = segundos
            self._programar(0, lambda s=segundos: self._log(
                f"🧠 Anti-bloqueo: esperando ~{s}s entre descargas "
                f"(se ajusta solo según los bloqueos detectados)."))
        self._esperar(segundos + random.uniform(0, 2))

    def _log_hilo(self, texto):
        self._programar(0, lambda t=texto: self._log(t))

    # ---- hilo de descarga --------------------------------------------
    def _descargar_thread(self, seleccionados, artista, mejor_version):
        try:
            self._descargar_interno(seleccionados, artista, mejor_version)
        except Exception as exc:
            log.exception("Error inesperado en la descarga")
            self._log_hilo(f"❌ Error inesperado: {exc}")
        finally:
            # Pase lo que pase, la interfaz vuelve a quedar utilizable.
            self._programar(0, self._finalizar_descarga)

    def _descargar_interno(self, seleccionados, artista, mejor_version):
        anti_bloqueo.reiniciar_bloqueos_consecutivos()

        # 1) Armar la lista completa de trabajo (álbum, tema).
        trabajo = []
        carpetas_usadas = {}
        for album in seleccionados:
            if self._cancelar_descarga.is_set():
                self._log_hilo("⏹ Descarga detenida por el usuario.")
                return
            self._log_hilo(f"📀 Leyendo tracklist de: {album['titulo']}")
            try:
                temas = obtener_tracklist_playlist(album["url"])
            except Exception as exc:
                self._log_hilo(f"❌ No se pudo leer '{album['titulo']}': {str(exc)[:300]}")
                continue

            nombre_album = album["titulo"]
            clave = sanitizar_nombre(nombre_album).lower()
            if clave in carpetas_usadas and carpetas_usadas[clave] != album["url"]:
                # Dos playlists con el mismo nombre: no mezclarlas en una carpeta.
                nombre_album = f"{nombre_album} [{album.get('id') or len(carpetas_usadas)}]"
            else:
                carpetas_usadas[clave] = album["url"]

            for tema in temas:
                trabajo.append({
                    "album": nombre_album,
                    "numero": tema["numero"],
                    "titulo_tema": tema["titulo"],
                    "url": tema.get("url"),
                    "duracion": tema.get("duracion"),
                    "disponible": tema.get("disponible", True),
                })

        total = len(trabajo)
        if total == 0:
            self._log_hilo("⚠️ No se encontraron temas para descargar.")
            return

        self._programar(0, lambda: self._cola_cargar(trabajo))
        self._esperar_cola_cargada(trabajo)

        # 2) Procesar cada tema.
        descargados = 0
        omitidos = 0
        fallidos = []
        no_disponibles = []
        detenida = False
        abortada = False

        for idx, tarea in enumerate(trabajo):
            if self._cancelar_descarga.is_set():
                detenida = True
                break
            album, numero, titulo_tema = tarea["album"], tarea["numero"], tarea["titulo_tema"]
            hizo_descarga = False

            carpeta = ruta_carpeta_album(artista, album)
            if tema_ya_descargado(carpeta, numero):
                omitidos += 1
                self._cola_fin(tarea, "omitido", "ya estaba descargado")
                self._log_hilo(f"⏭ Ya existe, se omite: {titulo_tema} ({album} #{numero})")
            elif not tarea.get("url") or not tarea.get("disponible", True):
                # Borrado/privado en la playlist: no hay nombre ni video que buscar.
                no_disponibles.append(f"{album} #{numero}")
                self._cola_fin(tarea, "no_disponible", "video borrado o privado en YouTube")
                self._log_hilo(f"🚫 {album} #{numero}: el video fue borrado o es privado en "
                               f"YouTube, no se puede descargar.")
            else:
                hizo_descarga = True
                self._log_hilo(f"⬇️ {titulo_tema} ({album} #{numero})")
                try:
                    ruta_final = self._obtener_tema(tarea, artista, mejor_version)
                except _Abortar:
                    abortada = True
                    break
                if ruta_final:
                    descargados += 1
                    self._cola_fin(tarea, "ok", self._resumen_ok(tarea, ruta_final))
                    self._log_hilo(f"✅ Guardado: {os.path.basename(ruta_final)}")
                elif self._cancelar_descarga.is_set():
                    detenida = True
                    break
                else:
                    fallidos.append(f"{album} #{numero} - {titulo_tema}")
                    self._cola_fin(tarea, "error", "no se pudo descargar: " + tarea.get("_ultimo_error", "sin detalle"))
                    self._log_hilo(f"❌ No se pudo descargar: {titulo_tema}")

            hechos = idx + 1
            self._programar(0, lambda p=(hechos / total) * 100, c=hechos, t=total:
                            self._actualizar_progreso(p, c, t))
            if hizo_descarga and idx < total - 1 and not self._cancelar_descarga.is_set():
                self._esperar_entre_temas()

        # 3) Resumen final.
        if abortada:
            self._log_hilo("🛑 Demasiados bloqueos seguidos de YouTube: descarga frenada. "
                           "Esperá un rato (o probá más tarde) y tocá Descargar de nuevo: "
                           "se retoma donde quedó.")
        elif detenida:
            self._log_hilo("⏹ Descarga detenida por el usuario.")
        else:
            self._sesion_terminada = True
        resumen = (f"🏁 Resumen: {descargados} descargados, {omitidos} ya existían, "
                   f"{len(fallidos)} con error, {len(no_disponibles)} no disponibles en YouTube.")
        self._log_hilo(resumen)
        if fallidos:
            self._log_hilo("   Con error (tocá Descargar de nuevo para reintentarlos):")
            for f in fallidos:
                self._log_hilo(f"   • {f}")
        log.info(resumen)

    def _obtener_tema(self, tarea, artista, mejor_version):
        """
        Consigue el mp3 de un tema. Fuentes: el video exacto de la playlist y/o
        la búsqueda de "mejor versión" (en el orden que corresponda). Cada
        fuente se reintenta hasta MAX_INTENTOS veces. Devuelve la ruta o None.
        """
        fuentes = ["busqueda", "pl"] if mejor_version else ["pl", "busqueda"]
        arreglos = tarea.setdefault("_arreglos", [])

        for fuente in fuentes:
            if self._cancelar_descarga.is_set():
                return None
            if fuente == "busqueda":
                self._log_hilo("   🔎 Buscando la mejor versión en YouTube...")
                self._cola_etapa(tarea, "Buscando versión")
                try:
                    r = buscar_mejor_resultado(artista, tarea["titulo_tema"],
                                               duracion_esperada=tarea.get("duracion"))
                except Exception as exc:
                    self._log_hilo(f"   ⚠️ Falló la búsqueda: {str(exc)[:200]}")
                    r = None
                if not r or not r.get("url"):
                    self._log_hilo("   ⚠️ No hay una coincidencia confiable.")
                    continue
                url = r["url"]
                self._log_hilo(f"   ↳ Encontrado: {r.get('titulo')}")
                if fuente != fuentes[0]:
                    arreglos.append("video de la playlist falló: se usó otra versión de YouTube")
                elif mejor_version:
                    arreglos.append("mejor versión elegida por búsqueda")
            else:
                url = tarea["url"]

            for intento in range(1, MAX_INTENTOS + 1):
                if self._cancelar_descarga.is_set():
                    return None
                try:
                    if intento > 1:
                        self._cola_etapa(tarea, "Reintentando", f"intento {intento}/{MAX_INTENTOS}")
                    else:
                        self._cola_etapa(tarea, "Descargando")
                    propios = []
                    ruta = procesar_tema(
                        url, artista, tarea["titulo_tema"], tarea["numero"], tarea["album"],
                        ffmpeg_path=self.ffmpeg_path_dir, duracion_esperada=tarea.get("duracion"),
                        on_etapa=lambda n: self._cola_etapa(tarea, n), arreglos=propios,
                        normalizar=bool(self.config_data.get("normalizar_volumen", True)),
                    )
                    if intento > 1:
                        arreglos.append(f"reintentado ({intento} intentos)")
                    arreglos.extend(propios)
                    return ruta
                except Exception as exc:
                    msg = str(exc)
                    tarea["_ultimo_error"] = msg.replace("\n", " ")[:90]
                    log.warning("Intento %s/%s falló (%s): %s", intento, MAX_INTENTOS, url, msg[:300])
                    self._log_hilo(f"   ⚠️ Intento {intento}/{MAX_INTENTOS} falló: {msg[:160]}")
                    if "Archivo inválido" in msg:
                        arreglos.append("archivo descartado por no pasar la validación")
                    if anti_bloqueo.es_error_de_bloqueo(msg):
                        if anti_bloqueo.debe_abortar():
                            raise _Abortar()
                        pausa = anti_bloqueo.segundos_de_pausa_por_bloqueos() or 20
                        self._log_hilo(f"   ⏳ Bloqueo detectado: pausa de {pausa}s antes de seguir.")
                        if self._esperar(pausa):
                            return None
                    elif anti_bloqueo.es_error_no_disponible(msg):
                        break  # reintentar no sirve: pasar a la otra fuente
                    elif intento < MAX_INTENTOS:
                        if self._esperar(5 * intento):
                            return None
        return None

    # ---- lista de temas en proceso (estilo YouTube MP3) -----------------
    def _cola_cargar(self, trabajo):
        """Llena la lista con todos los temas: cada fila empieza 'En cola'."""
        self._cola_estado = {}
        self._stats_set(total=len(trabajo), ok=0, omit=0, err=0)
        try:
            self.lista_cola.delete(0, "end")
        except tk.TclError:
            return
        for i, t in enumerate(trabajo):
            t["_fila"] = i
            t["_base"] = f"{t['album']} · {int(t['numero']):02d}. {t['titulo_tema']}"
            self.lista_cola.insert("end", "")
            self._cola_pintar(i, "En cola", t["_base"])

    def _cola_pintar(self, fila, etapa, base, pulso=0, extra=""):
        icono, color = ETAPAS.get(etapa, ("⚙️", "#475569"))
        if etapa == "En cola":
            texto = f"{icono} En cola   ·   {base}"
        else:
            celdas = 8
            llenas = min(celdas - 1, pulso % celdas)
            barra = "▰" * llenas + "▱" * (celdas - llenas)
            texto = f"{icono} {barra} {etapa}   ·   {base}"
        if extra:
            texto += f"   ·   {extra}"
        self._cola_escribir(fila, texto, color)

    def _cola_escribir(self, fila, texto, color):
        try:
            self.lista_cola.delete(fila)
            self.lista_cola.insert(fila, texto)
            self.lista_cola.itemconfig(fila, bg=color, fg="white")
        except tk.TclError:
            pass

    def _cola_etapa(self, tarea, etapa, extra=""):
        """Desde cualquier hilo: marca la etapa actual del tema (con barrita animada)."""
        fila = tarea.get("_fila")
        if fila is None:
            return
        def _ui():
            st = self._cola_estado.get(fila)
            if st is None:
                st = {"tarea": tarea, "pulso": 0}
                self._cola_estado[fila] = st
                self._programar(350, lambda: self._cola_animar(fila))
            st["etapa"], st["extra"], st["pulso"] = etapa, extra, 0
            self._cola_pintar(fila, etapa, tarea["_base"], 0, extra)
            try:
                self.lista_cola.see(fila)
            except tk.TclError:
                pass
        self._programar(0, _ui)

    def _cola_animar(self, fila):
        st = self._cola_estado.get(fila)
        if not st:
            return
        st["pulso"] += 1
        self._cola_pintar(fila, st["etapa"], st["tarea"]["_base"], st["pulso"], st.get("extra", ""))
        self._programar(350, lambda: self._cola_animar(fila))

    def _cola_fin(self, tarea, estado, resumen=""):
        """estado: 'ok' | 'error' | 'omitido' | 'no_disponible'."""
        fila = tarea.get("_fila")
        if fila is None:
            return
        def _ui():
            self._cola_estado.pop(fila, None)
            clave = {"ok": "ok", "omitido": "omit"}.get(estado, "err")
            self._stats_set(**{clave: self._stats[clave] + 1})
            iconos = {"ok": ("✅", COLOR_OK), "error": ("❌", COLOR_ERROR),
                      "omitido": ("⏭", COLOR_OMITIDO), "no_disponible": ("🚫", COLOR_ERROR)}
            icono, color = iconos.get(estado, ("•", COLOR_OMITIDO))
            texto = f"{icono} {tarea['_base']}" + (f"   ·   {resumen}" if resumen else "")
            self._cola_escribir(fila, texto, color)
        self._programar(0, _ui)

    def _esperar_cola_cargada(self, trabajo):
        """El hilo espera (poco) a que la interfaz haya dibujado la lista."""
        for _ in range(100):
            if all("_fila" in t for t in trabajo):
                return
            time.sleep(0.05)

    def _resumen_ok(self, tarea, ruta_final):
        m = re.match(r"\[(\d+|SD) BPM\]", os.path.basename(ruta_final))
        bpm = f"{m.group(1)} BPM · " if m else ""
        return bpm + self._resumen_arreglos(tarea)

    @staticmethod
    def _resumen_arreglos(tarea, solo_arreglos=False):
        arreglos = tarea.get("_arreglos") or []
        if not arreglos:
            return "" if solo_arreglos else "sin arreglos necesarios"
        return "arreglos: " + "; ".join(arreglos)

    def _actualizar_progreso(self, porcentaje, completados, total):
        self._config_seguro(self.progressbar, value=porcentaje)
        self._config_seguro(self.lbl_estado, text=f"Progreso: {completados}/{total} temas")

    def _finalizar_descarga(self):
        if self._sesion_terminada:
            self._borrar_sesion()
        self._descargando = False
        self._cancelar_descarga.clear()
        self._config_seguro(self.btn_buscar, state="normal")
        self._config_seguro(self.btn_descargar, text="⬇  Descargar seleccionados")
        self._config_seguro(self.btn_detener_descarga, state="disabled", bg=T["off"], text="⏹  Detener")
        self._actualizar_boton_descargar()

    # ------------------------------------------------------------------
    def _log(self, texto):
        log.info(texto)
        try:
            self.texto_log.configure(state="normal")
            self.texto_log.insert("end", texto + "\n")
            if int(self.texto_log.index("end-1c").split(".")[0]) > MAX_LINEAS_LOG:
                self.texto_log.delete("1.0", "1000.0")
            self.texto_log.see("end")
            self.texto_log.configure(state="disabled")
        except tk.TclError:
            pass

    def iniciar(self):
        self.ventana.mainloop()


def abrir_ventana_principal(config_data):
    VentanaPrincipal(config_data).iniciar()
