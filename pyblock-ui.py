import json
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PYBLOCK_PY   = os.path.join(SCRIPT_DIR, "pyblock.py")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "pyblock-ui.json")

DEFAULTS = {
    # I/O
    "input":  "",
    "output": "",
    # Plate geometry
    "width":           100.0,
    "plate_thickness":   3.0,
    "relief_height":     1.0,
    "min_line_width":    1.0,
    # Raster
    "threshold": 128,
    "invert":    False,
    # Vectorization
    "vectorizer":             "potrace",
    "potrace_threshold":      0.5,
    "potrace_turdsize":       2,
    "potrace_alphamax":       1.0,
    "potrace_opttolerance":   0.2,
    "vtracer_color_precision": 6,
    "vtracer_filter_speckle":  4,
    # STL
    "stl_ascii":      False,
    "curve_segments": 16,
    # UI
    "verbose":          False,
    "overwrite":        False,
    "open_after_run":   False,
}

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # Fill any missing keys with defaults (handles version upgrades)
        return {**DEFAULTS, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save_settings(settings: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        print(f"Warning: could not save settings: {e}", file=sys.stderr)

BG       = "#1e1e2e"   # deep navy
PANEL    = "#2a2a3e"   # slightly lighter panel
BORDER   = "#3d3d55"   # subtle border
ACCENT   = "#7c6af7"   # violet accent
ACCENT2  = "#5af78e"   # mint green for success
WARN     = "#f7c35a"   # amber for warnings
ERR      = "#f75a7c"   # rose for errors
FG       = "#cdd6f4"   # main text (Catppuccin Mocha palette)
FG_DIM   = "#7c7fa3"   # dimmed text
BTN_FG   = "#ffffff"
MONO     = ("Consolas", "Menlo", "DejaVu Sans Mono", "monospace")


class PyBlockUI(tk.Tk):
    def __init__(self):
        super().__init__()

        self.settings = load_settings()
        self._proc: subprocess.Popen | None = None
        self._log_queue: queue.Queue = queue.Queue()

        self.title("pyBlock")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(720, 640)

        # Tk variables — one per setting
        self._vars: dict[str, tk.Variable] = {}
        self._build_vars()
        self._build_ui()
        self._load_vars_from_settings()
        self._update_output_from_input()
        self._update_vectorizer_section()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_log_queue)

    def _build_vars(self):
        S = self.settings
        def sv(key): return tk.StringVar(value=str(S[key]))
        def dv(key): return tk.DoubleVar(value=float(S[key]))
        def iv(key): return tk.IntVar(value=int(S[key]))
        def bv(key): return tk.BooleanVar(value=bool(S[key]))

        self._vars = {
            "input":                   sv("input"),
            "output":                  sv("output"),
            "width":                   dv("width"),
            "plate_thickness":         dv("plate_thickness"),
            "relief_height":           dv("relief_height"),
            "min_line_width":          dv("min_line_width"),
            "threshold":               iv("threshold"),
            "invert":                  bv("invert"),
            "vectorizer":              sv("vectorizer"),
            "potrace_threshold":       dv("potrace_threshold"),
            "potrace_turdsize":        iv("potrace_turdsize"),
            "potrace_alphamax":        dv("potrace_alphamax"),
            "potrace_opttolerance":    dv("potrace_opttolerance"),
            "vtracer_color_precision": iv("vtracer_color_precision"),
            "vtracer_filter_speckle":  iv("vtracer_filter_speckle"),
            "stl_ascii":               bv("stl_ascii"),
            "curve_segments":          iv("curve_segments"),
            "verbose":                 bv("verbose"),
            "overwrite":               bv("overwrite"),
            "open_after_run":          bv("open_after_run"),
        }

        # Auto-populate output when input changes
        self._vars["input"].trace_add("write", lambda *_: self._update_output_from_input())
        # Show/hide vectorizer sub-settings when vectorizer changes
        self._vars["vectorizer"].trace_add("write", lambda *_: self._update_vectorizer_section())

    def _load_vars_from_settings(self):
        S = self.settings
        for key, var in self._vars.items():
            try:
                var.set(S.get(key, DEFAULTS[key]))
            except (tk.TclError, ValueError):
                var.set(DEFAULTS[key])

    def _collect_settings(self) -> dict:
        out = {}
        for key, var in self._vars.items():
            try:
                out[key] = var.get()
            except (tk.TclError, ValueError):
                out[key] = DEFAULTS[key]
        return out

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Header
        hdr = tk.Frame(self, bg=ACCENT, height=4)
        hdr.grid(row=0, column=0, sticky="ew")

        # Scrollable settings pane + log pane
        paned = tk.PanedWindow(self, orient=tk.VERTICAL, bg=BG,
                               sashwidth=6, sashrelief=tk.FLAT)
        paned.grid(row=1, column=0, sticky="nsew", padx=12, pady=(10, 0))

        settings_canvas = self._build_settings_scroll(paned)
        paned.add(settings_canvas, minsize=360, stretch="always")

        log_frame = self._build_log_pane(paned)
        paned.add(log_frame, minsize=120, stretch="always")

        # Bottom action bar
        self._build_action_bar()

    def _section(self, parent, title: str) -> tk.Frame:
        """Create a labelled group frame."""
        outer = tk.Frame(parent, bg=BG)
        outer.columnconfigure(0, weight=1)

        label = tk.Label(outer, text=title.upper(), bg=BG, fg=ACCENT,
                         font=("TkDefaultFont", 8, "bold"), anchor="w",
                         padx=2, pady=4)
        label.grid(row=0, column=0, sticky="ew")

        inner = tk.Frame(outer, bg=PANEL, bd=0,
                         highlightthickness=1, highlightbackground=BORDER)
        inner.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        inner.columnconfigure(1, weight=1)

        return inner

    def _row(self, parent, row: int, label: str, widget_factory,
             col_span: int = 1, note: str = ""):
        """Add a label + widget row inside a section frame."""
        tk.Label(parent, text=label, bg=PANEL, fg=FG,
                 font=("TkDefaultFont", 10), anchor="w",
                 padx=10, pady=4).grid(
            row=row, column=0, sticky="w", padx=(10, 6))

        w = widget_factory(parent)
        w.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=3,
               columnspan=col_span)

        if note:
            tk.Label(parent, text=note, bg=PANEL, fg=FG_DIM,
                     font=("TkDefaultFont", 8), anchor="w").grid(
                row=row, column=2, sticky="w", padx=(4, 10))
        return w

    # ── Settings scroll area ──────────────────────────────────────────────────

    def _build_settings_scroll(self, parent) -> tk.Frame:
        container = tk.Frame(parent, bg=BG)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        canvas = tk.Canvas(container, bg=BG, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical",
                                  command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        inner = tk.Frame(canvas, bg=BG)
        inner.columnconfigure(0, weight=1)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfig(win_id, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scrolling (cross-platform)
        def _on_wheel(event):
            if platform.system() == "Windows":
                canvas.yview_scroll(int(-1 * event.delta / 120), "units")
            elif platform.system() == "Darwin":
                canvas.yview_scroll(int(-1 * event.delta), "units")
            else:
                if event.num == 4:
                    canvas.yview_scroll(-1, "units")
                elif event.num == 5:
                    canvas.yview_scroll(1, "units")

        canvas.bind_all("<MouseWheel>", _on_wheel)
        canvas.bind_all("<Button-4>", _on_wheel)
        canvas.bind_all("<Button-5>", _on_wheel)

        self._populate_settings(inner)

        return container

    def _populate_settings(self, parent):
        """Fill the scrollable area with all setting groups."""
        parent.columnconfigure(0, weight=1)
        r = 0

        # ── Input / Output ────────────────────────────────────────────────────
        sec = self._section(parent, "Input / Output")
        sec.grid(row=r, column=0, sticky="ew", padx=2); r += 1
        sec.columnconfigure(1, weight=1)

        def _input_row(p):
            f = tk.Frame(p, bg=PANEL)
            f.columnconfigure(0, weight=1)
            e = self._entry(f, "input")
            e.grid(row=0, column=0, sticky="ew")
            btn = self._small_btn(f, "…", self._browse_input)
            btn.grid(row=0, column=1, padx=(4, 0))
            return f
        self._row(sec, 0, "Input file", _input_row)

        def _output_row(p):
            f = tk.Frame(p, bg=PANEL)
            f.columnconfigure(0, weight=1)
            e = self._entry(f, "output")
            e.grid(row=0, column=0, sticky="ew")
            btn = self._small_btn(f, "…", self._browse_output)
            btn.grid(row=0, column=1, padx=(4, 0))
            return f
        self._row(sec, 1, "Output STL", _output_row)

        # ── Plate geometry ────────────────────────────────────────────────────
        sec = self._section(parent, "Plate Geometry")
        sec.grid(row=r, column=0, sticky="ew", padx=2); r += 1
        sec.columnconfigure(1, weight=1)

        self._row(sec, 0, "Width", lambda p: self._spin_float(p, "width", 1, 2000, 1.0), note="mm")
        self._row(sec, 1, "Plate thickness", lambda p: self._spin_float(p, "plate_thickness", 0.1, 50, 0.5), note="mm")
        self._row(sec, 2, "Relief height", lambda p: self._spin_float(p, "relief_height", -20, 20, 0.5), note="mm  (negative = emboss)")
        self._row(sec, 3, "Min line width", lambda p: self._spin_float(p, "min_line_width", 0.1, 20, 0.1), note="mm")

        # ── Raster ───────────────────────────────────────────────────────────
        sec = self._section(parent, "Raster Image Processing")
        sec.grid(row=r, column=0, sticky="ew", padx=2); r += 1
        sec.columnconfigure(1, weight=1)

        def _thresh_row(p):
            f = tk.Frame(p, bg=PANEL)
            f.columnconfigure(0, weight=1)
            scale = tk.Scale(f, variable=self._vars["threshold"],
                             from_=0, to=255, orient=tk.HORIZONTAL,
                             bg=PANEL, fg=FG, troughcolor=BORDER,
                             activebackground=ACCENT, highlightthickness=0,
                             bd=0, sliderlength=16, showvalue=True)
            scale.grid(row=0, column=0, sticky="ew")
            return f
        self._row(sec, 0, "Threshold", _thresh_row, note="0–255")
        self._row(sec, 1, "Invert", lambda p: self._check(p, "invert"))

        # ── Vectorization ─────────────────────────────────────────────────────
        sec = self._section(parent, "Vectorization")
        sec.grid(row=r, column=0, sticky="ew", padx=2); r += 1
        sec.columnconfigure(1, weight=1)

        def _vec_combo(p):
            cb = ttk.Combobox(p, textvariable=self._vars["vectorizer"],
                              values=["potrace", "vtracer"],
                              state="readonly", width=12)
            self._style_combo(cb)
            return cb
        self._row(sec, 0, "Engine", _vec_combo)

        # Potrace sub-settings (hidden when vtracer selected)
        self._potrace_frame = tk.Frame(sec, bg=PANEL)
        self._potrace_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
        self._potrace_frame.columnconfigure(1, weight=1)
        self._row(self._potrace_frame, 0, "  Blacklevel", lambda p: self._spin_float(p, "potrace_threshold", 0, 1, 0.05), note="0.0–1.0")
        self._row(self._potrace_frame, 1, "  Turd size",  lambda p: self._spin_int(p, "potrace_turdsize", 0, 100), note="px  suppress speckles")
        self._row(self._potrace_frame, 2, "  Alpha max",  lambda p: self._spin_float(p, "potrace_alphamax", 0, 1.333, 0.05), note="0–1.333")
        self._row(self._potrace_frame, 3, "  Opt tolerance", lambda p: self._spin_float(p, "potrace_opttolerance", 0, 2, 0.05))

        # Vtracer sub-settings (hidden when potrace selected)
        self._vtracer_frame = tk.Frame(sec, bg=PANEL)
        self._vtracer_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
        self._vtracer_frame.columnconfigure(1, weight=1)
        self._row(self._vtracer_frame, 0, "  Color precision", lambda p: self._spin_int(p, "vtracer_color_precision", 1, 8), note="bits")
        self._row(self._vtracer_frame, 1, "  Filter speckle",  lambda p: self._spin_int(p, "vtracer_filter_speckle", 0, 128), note="px")

        # ── STL Output ────────────────────────────────────────────────────────
        sec = self._section(parent, "STL Output")
        sec.grid(row=r, column=0, sticky="ew", padx=2); r += 1
        sec.columnconfigure(1, weight=1)

        self._row(sec, 0, "ASCII STL",      lambda p: self._check(p, "stl_ascii"), note="binary is smaller and faster")
        self._row(sec, 1, "Curve segments", lambda p: self._spin_int(p, "curve_segments", 4, 128), note="per curve")

        # ── Misc ──────────────────────────────────────────────────────────────
        sec = self._section(parent, "Miscellaneous")
        sec.grid(row=r, column=0, sticky="ew", padx=2); r += 1
        sec.columnconfigure(1, weight=1)

        self._row(sec, 0, "Verbose logging", lambda p: self._check(p, "verbose"))

    # ── Log pane ──────────────────────────────────────────────────────────────

    def _build_log_pane(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg=BG)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        tk.Label(frame, text="OUTPUT", bg=BG, fg=ACCENT,
                 font=("TkDefaultFont", 8, "bold"),
                 anchor="w", padx=2).grid(row=0, column=0, sticky="ew")

        txt_frame = tk.Frame(frame, bg=PANEL, highlightthickness=1,
                             highlightbackground=BORDER)
        txt_frame.grid(row=1, column=0, sticky="nsew")
        txt_frame.columnconfigure(0, weight=1)
        txt_frame.rowconfigure(0, weight=1)

        self._log = tk.Text(
            txt_frame,
            bg="#13131f", fg=FG, insertbackground=FG,
            font=(MONO[0], 10), relief=tk.FLAT, bd=0,
            wrap=tk.WORD, state=tk.DISABLED,
            selectbackground=ACCENT, selectforeground=BTN_FG,
            padx=8, pady=6,
        )
        sb = ttk.Scrollbar(txt_frame, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        self._log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        # Colour tags for log levels
        self._log.tag_configure("INFO",  foreground=FG)
        self._log.tag_configure("DEBUG", foreground=FG_DIM)
        self._log.tag_configure("WARN",  foreground=WARN)
        self._log.tag_configure("ERROR", foreground=ERR)
        self._log.tag_configure("FATAL", foreground=ERR)
        self._log.tag_configure("ok",    foreground=ACCENT2)

        return frame

    # ── Action bar ────────────────────────────────────────────────────────────

    def _build_action_bar(self):
        bar = tk.Frame(self, bg=PANEL, highlightthickness=1,
                       highlightbackground=BORDER)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=8)
        bar.columnconfigure(3, weight=1)   # spacer

        self._check_inline(bar, "overwrite",      "Overwrite output", col=0)
        self._check_inline(bar, "open_after_run",  "Open STL after run", col=1)

        self._btn(bar, "Reset to defaults", self._reset_defaults,
                  bg=BORDER, col=2, padx=(16, 4))

        # Spacer
        tk.Frame(bar, bg=PANEL).grid(row=0, column=3, sticky="ew")

        self._run_btn = self._btn(bar, "▶  Run pyBlock", self._run,
                                  bg=ACCENT, col=4, padx=(4, 8))
        self._run_btn.configure(font=("TkDefaultFont", 11, "bold"))

    # ── Widget helpers ────────────────────────────────────────────────────────

    def _entry(self, parent, key: str) -> tk.Entry:
        e = tk.Entry(parent, textvariable=self._vars[key],
                     bg="#13131f", fg=FG, insertbackground=FG,
                     relief=tk.FLAT, bd=4, font=("TkDefaultFont", 10),
                     highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=ACCENT)
        return e

    def _spin_float(self, parent, key: str, lo, hi, inc) -> ttk.Spinbox:
        sb = ttk.Spinbox(parent, textvariable=self._vars[key],
                         from_=lo, to=hi, increment=inc,
                         width=10, format="%.2f")
        return sb

    def _spin_int(self, parent, key: str, lo, hi) -> ttk.Spinbox:
        sb = ttk.Spinbox(parent, textvariable=self._vars[key],
                         from_=lo, to=hi, increment=1, width=10)
        return sb

    def _check(self, parent, key: str) -> tk.Checkbutton:
        cb = tk.Checkbutton(parent, variable=self._vars[key],
                            bg=PANEL, fg=FG, activebackground=PANEL,
                            activeforeground=FG, selectcolor=ACCENT,
                            relief=tk.FLAT, bd=0)
        return cb

    def _check_inline(self, parent, key: str, text: str, col: int):
        cb = tk.Checkbutton(parent, text=text, variable=self._vars[key],
                            bg=PANEL, fg=FG, activebackground=PANEL,
                            activeforeground=FG, selectcolor=BORDER,
                            font=("TkDefaultFont", 10),
                            relief=tk.FLAT, bd=0, pady=6)
        cb.grid(row=0, column=col, padx=(10, 2))

    def _btn(self, parent, text: str, cmd, bg=BORDER, col=0, padx=(4, 4)):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=bg, fg=BTN_FG, activebackground=ACCENT,
                      activeforeground=BTN_FG,
                      relief=tk.FLAT, bd=0, padx=14, pady=7,
                      font=("TkDefaultFont", 10), cursor="hand2")
        b.grid(row=0, column=col, padx=padx, pady=6, sticky="ns")
        return b

    def _small_btn(self, parent, text: str, cmd) -> tk.Button:
        return tk.Button(parent, text=text, command=cmd,
                         bg=BORDER, fg=FG, activebackground=ACCENT,
                         activeforeground=BTN_FG,
                         relief=tk.FLAT, bd=0, padx=8, pady=2,
                         font=("TkDefaultFont", 10), cursor="hand2")

    def _style_combo(self, cb: ttk.Combobox):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground="#13131f",
                        background=BORDER,
                        foreground=FG,
                        selectbackground=ACCENT,
                        selectforeground=BTN_FG,
                        arrowcolor=FG)

    # ── Logic ─────────────────────────────────────────────────────────────────

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select input file",
            filetypes=[
                ("Supported files", "*.svg *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif"),
                ("SVG files",       "*.svg"),
                ("Image files",     "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif"),
                ("All files",       "*.*"),
            ],
        )
        if path:
            self._vars["input"].set(path)

    def _browse_output(self):
        init = self._vars["output"].get() or self._vars["input"].get()
        init_dir  = os.path.dirname(init) if init else ""
        init_file = os.path.splitext(os.path.basename(init))[0] + ".stl" if init else "output.stl"
        path = filedialog.asksaveasfilename(
            title="Save STL as",
            initialdir=init_dir,
            initialfile=init_file,
            defaultextension=".stl",
            filetypes=[("STL files", "*.stl"), ("All files", "*.*")],
        )
        if path:
            self._vars["output"].set(path)

    def _update_output_from_input(self):
        """Auto-populate output path from input path if output is empty or was auto-generated."""
        inp = self._vars["input"].get().strip()
        if not inp:
            return
        current_out = self._vars["output"].get().strip()
        # Only auto-fill if output is empty or looks like a previous auto-fill
        auto = os.path.splitext(inp)[0] + ".stl"
        if not current_out or current_out == auto:
            self._vars["output"].set(auto)

    def _update_vectorizer_section(self):
        vec = self._vars["vectorizer"].get()
        if vec == "potrace":
            self._vtracer_frame.grid_remove()
            self._potrace_frame.grid()
        else:
            self._potrace_frame.grid_remove()
            self._vtracer_frame.grid()

    def _reset_defaults(self):
        for key, var in self._vars.items():
            if key in ("input", "output"):
                continue   # don't clear file paths on reset
            try:
                var.set(DEFAULTS[key])
            except (tk.TclError, ValueError):
                pass

    def _on_close(self):
        save_settings(self._collect_settings())
        self.destroy()

    # ── Run pyBlock ───────────────────────────────────────────────────────────

    def _build_command(self) -> list[str] | None:
        """Assemble the pyblock.py command line. Returns None if invalid."""
        inp = self._vars["input"].get().strip()
        out = self._vars["output"].get().strip()

        if not inp:
            messagebox.showerror("No input", "Please select an input file.")
            return None
        if not os.path.isfile(inp):
            messagebox.showerror("File not found", f"Input file does not exist:\n{inp}")
            return None
        if not out:
            messagebox.showerror("No output", "Please specify an output file path.")
            return None

        if os.path.isfile(out) and not self._vars["overwrite"].get():
            if not messagebox.askyesno(
                "Overwrite?",
                f"Output file already exists:\n{out}\n\nOverwrite it?",
            ):
                return None

        cmd = [sys.executable, PYBLOCK_PY,
               "--input",  inp,
               "--output", out,
               "--width",              str(self._vars["width"].get()),
               "--plate-thickness",    str(self._vars["plate_thickness"].get()),
               "--relief-height",      str(self._vars["relief_height"].get()),
               "--min-line-width",     str(self._vars["min_line_width"].get()),
               "--threshold",          str(self._vars["threshold"].get()),
               "--vectorizer",         self._vars["vectorizer"].get(),
               "--potrace-threshold",  str(self._vars["potrace_threshold"].get()),
               "--potrace-turdsize",   str(self._vars["potrace_turdsize"].get()),
               "--potrace-alphamax",   str(self._vars["potrace_alphamax"].get()),
               "--potrace-opttolerance", str(self._vars["potrace_opttolerance"].get()),
               "--vtracer-color-precision", str(self._vars["vtracer_color_precision"].get()),
               "--vtracer-filter-speckle",  str(self._vars["vtracer_filter_speckle"].get()),
               "--curve-segments",     str(self._vars["curve_segments"].get()),
        ]
        if self._vars["invert"].get():
            cmd.append("--invert")
        if self._vars["stl_ascii"].get():
            cmd.append("--stl-ascii")
        if self._vars["verbose"].get():
            cmd.append("--verbose")

        return cmd

    def _run(self):
        if self._proc is not None:
            # Second click while running = cancel
            self._proc.terminate()
            return

        cmd = self._build_command()
        if cmd is None:
            return

        self._clear_log()
        self._log_line(f"$ {' '.join(cmd)}\n", tag="DEBUG")

        self._run_btn.configure(text="■  Cancel", bg=ERR)

        # Launch in a background thread so the UI stays responsive
        self._output_path = self._vars["output"].get().strip()
        t = threading.Thread(target=self._run_thread, args=(cmd,), daemon=True)
        t.start()

    def _run_thread(self, cmd: list[str]):
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._proc = proc

            for line in proc.stdout:
                self._log_queue.put(("line", line))

            proc.wait()
            self._log_queue.put(("done", proc.returncode))
        except Exception as e:
            self._log_queue.put(("error", str(e)))

    def _poll_log_queue(self):
        try:
            while True:
                item = self._log_queue.get_nowait()
                kind = item[0]

                if kind == "line":
                    self._log_line(item[1])

                elif kind == "done":
                    rc = item[1]
                    self._proc = None
                    self._run_btn.configure(text="▶  Run pyBlock", bg=ACCENT)

                    output_exists = os.path.isfile(self._output_path)

                    if rc == 0 and output_exists:
                        self._log_line(f"\n✓  Finished successfully.\n", tag="ok")
                        if self._vars["open_after_run"].get():
                            self._open_stl(self._output_path)
                    else:
                        self._log_line(f"\n✗  pyBlock exited with code {rc}.\n", tag="ERROR")
                        if not output_exists:
                            messagebox.showerror(
                                "pyBlock failed",
                                f"pyBlock exited with code {rc} and no STL was created.\n\n"
                                "See the output log for details.",
                            )

                elif kind == "error":
                    self._proc = None
                    self._run_btn.configure(text="▶  Run pyBlock", bg=ACCENT)
                    self._log_line(f"\nFailed to launch pyBlock: {item[1]}\n", tag="ERROR")
                    messagebox.showerror("Launch error", item[1])

        except queue.Empty:
            pass

        self.after(50, self._poll_log_queue)

    # ── Log helpers ───────────────────────────────────────────────────────────

    _LEVEL_RE = re.compile(r"\[(INFO |WARN |ERROR|DEBUG|FATAL)\]")

    def _log_line(self, line: str, tag: str = ""):
        """Append a line to the log widget, colour-coded by level."""
        self._log.configure(state=tk.NORMAL)
        if not tag:
            m = self._LEVEL_RE.search(line)
            tag = m.group(1).strip() if m else "INFO"
        # Strip ANSI escape codes
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
        self._log.insert(tk.END, clean, tag)
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _clear_log(self):
        self._log.configure(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.configure(state=tk.DISABLED)

    # ── Open STL in system viewer ─────────────────────────────────────────────

    def _open_stl(self, path: str):
        try:
            if platform.system() == "Windows":
                os.startfile(path)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showwarning("Could not open STL",
                                   f"Failed to open {path}:\n{e}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = PyBlockUI()

    # Apply ttk theme adjustments
    style = ttk.Style(app)
    style.theme_use("clam")
    style.configure("TScrollbar", background=BORDER, troughcolor=BG,
                    arrowcolor=FG_DIM, borderwidth=0)
    style.configure("TSpinbox", fieldbackground="#13131f", foreground=FG,
                    background=BORDER, arrowcolor=FG, borderwidth=0,
                    selectbackground=ACCENT, selectforeground=BTN_FG)
    style.map("TSpinbox", fieldbackground=[("readonly", "#13131f")])
    style.configure("TCombobox", fieldbackground="#13131f", foreground=FG,
                    background=BORDER, selectbackground=ACCENT,
                    selectforeground=BTN_FG)

    app.mainloop()
