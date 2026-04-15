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

# -- Defaults -----------------------------------------------------------------

DEFAULTS: dict = {
    "input":  "",
    "output": "",
    "width":              100.0,
    "plate_thickness":      3.0,
    "relief_height":        1.0,
    "min_line_width":       1.0,
    "threshold":          128,
    "invert":             False,
    "vectorizer":         "potrace",
    "potrace_threshold":    0.5,
    "potrace_turdsize":     2,
    "potrace_alphamax":     1.0,
    "potrace_opttolerance": 0.2,
    "vtracer_color_precision": 6,
    "vtracer_filter_speckle":  4,
    "stl_ascii":          False,
    "curve_segments":     16,
    "verbose":            False,
    "overwrite":          False,
    "open_after_run":     False,
}

# -- Persistence --------------------------------------------------------------

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {**DEFAULTS, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)

def save_settings(s: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError as e:
        print(f"Warning: could not save settings: {e}", file=sys.stderr)

# -- Colours ------------------------------------------------------------------

BG      = "#1e1e2e"
PANEL   = "#2a2a3e"
BORDER  = "#3d3d55"
ACCENT  = "#7c6af7"
ACCENT2 = "#5af78e"
WARN    = "#f7c35a"
ERR     = "#f75a7c"
FG      = "#cdd6f4"
FG_DIM  = "#7c7fa3"
BTN_FG  = "#ffffff"
LOG_BG  = "#13131f"

# -- Scrollable frame ---------------------------------------------------------

class ScrollFrame(tk.Frame):
    """A frame whose content scrolls vertically.  Add widgets to self.inner."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._c = tk.Canvas(self, bg=BG, bd=0,
                            highlightthickness=0, takefocus=False)
        self._sb = ttk.Scrollbar(self, orient="vertical",
                                 command=self._c.yview)
        self._c.configure(yscrollcommand=self._sb.set)
        self._c.grid(row=0, column=0, sticky="nsew")
        self._sb.grid(row=0, column=1, sticky="ns")

        self.inner = tk.Frame(self._c, bg=BG)
        self.inner.columnconfigure(0, weight=1)
        self._win = self._c.create_window((0, 0), window=self.inner,
                                          anchor="nw")

        self.inner.bind("<Configure>", self._on_inner)
        self._c.bind("<Configure>",    self._on_canvas)

        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(seq, self._wheel, add="+")

    def _on_inner(self, _=None):
        self._c.configure(scrollregion=self._c.bbox("all"))

    def _on_canvas(self, e):
        self._c.itemconfigure(self._win, width=e.width)

    def _wheel(self, e):
        sys_ = platform.system()
        if sys_ == "Windows":
            self._c.yview_scroll(int(-1 * e.delta / 120), "units")
        elif sys_ == "Darwin":
            self._c.yview_scroll(int(-1 * e.delta), "units")
        else:
            self._c.yview_scroll(-1 if e.num == 4 else 1, "units")


# -- Main window --------------------------------------------------------------

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("pyBlock")
        self.configure(bg=BG)
        self.minsize(680, 600)
        self.geometry("760x820")

        # Apply ttk styles BEFORE any widgets are created
        self._setup_styles()

        self.settings = load_settings()
        self._proc = None
        self._q    = queue.Queue()
        self._output_path = ""

        self._vars = self._make_vars()
        self._trace_vars()
        self._build()
        self._update_vec_panel()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)

    # -- Styles ---------------------------------------------------------------

    def _setup_styles(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TScrollbar",
                     background=BORDER, troughcolor=BG,
                     arrowcolor=FG_DIM, borderwidth=0, relief="flat")
        st.configure("TSpinbox",
                     fieldbackground=LOG_BG, foreground=FG,
                     background=BORDER, arrowcolor=FG,
                     borderwidth=1, relief="flat",
                     selectbackground=ACCENT, selectforeground=BTN_FG,
                     insertcolor=FG)
        st.map("TSpinbox",
               fieldbackground=[("disabled", PANEL)],
               foreground=[("disabled", FG_DIM)])
        st.configure("TCombobox",
                     fieldbackground=LOG_BG, foreground=FG,
                     background=BORDER, arrowcolor=FG,
                     selectbackground=ACCENT, selectforeground=BTN_FG,
                     borderwidth=1, relief="flat")
        st.map("TCombobox",
               fieldbackground=[("readonly", LOG_BG)],
               foreground=[("readonly", FG)],
               selectbackground=[("readonly", ACCENT)])

    # -- Variables ------------------------------------------------------------

    def _make_vars(self) -> dict:
        S = self.settings
        return {
            "input":                   tk.StringVar(value=S["input"]),
            "output":                  tk.StringVar(value=S["output"]),
            "width":                   tk.DoubleVar(value=S["width"]),
            "plate_thickness":         tk.DoubleVar(value=S["plate_thickness"]),
            "relief_height":           tk.DoubleVar(value=S["relief_height"]),
            "min_line_width":          tk.DoubleVar(value=S["min_line_width"]),
            "threshold":               tk.IntVar(value=S["threshold"]),
            "invert":                  tk.BooleanVar(value=S["invert"]),
            "vectorizer":              tk.StringVar(value=S["vectorizer"]),
            "potrace_threshold":       tk.DoubleVar(value=S["potrace_threshold"]),
            "potrace_turdsize":        tk.IntVar(value=S["potrace_turdsize"]),
            "potrace_alphamax":        tk.DoubleVar(value=S["potrace_alphamax"]),
            "potrace_opttolerance":    tk.DoubleVar(value=S["potrace_opttolerance"]),
            "vtracer_color_precision": tk.IntVar(value=S["vtracer_color_precision"]),
            "vtracer_filter_speckle":  tk.IntVar(value=S["vtracer_filter_speckle"]),
            "stl_ascii":               tk.BooleanVar(value=S["stl_ascii"]),
            "curve_segments":          tk.IntVar(value=S["curve_segments"]),
            "verbose":                 tk.BooleanVar(value=S["verbose"]),
            "overwrite":               tk.BooleanVar(value=S["overwrite"]),
            "open_after_run":          tk.BooleanVar(value=S["open_after_run"]),
        }

    def _trace_vars(self):
        self._vars["input"].trace_add("write",
            lambda *_: self._auto_output())
        self._vars["vectorizer"].trace_add("write",
            lambda *_: self._update_vec_panel())

    def _collect(self) -> dict:
        out = {}
        for k, v in self._vars.items():
            try:
                out[k] = v.get()
            except (tk.TclError, ValueError):
                out[k] = DEFAULTS[k]
        return out

    # -- Build layout ---------------------------------------------------------

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=2)   # settings gets more space
        self.rowconfigure(2, weight=1)   # log

        # Accent strip at top
        tk.Frame(self, bg=ACCENT, height=4).grid(row=0, column=0, sticky="ew")

        # Scrollable settings
        self._scroll = ScrollFrame(self, bg=BG)
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=(10, 4))
        self._build_settings(self._scroll.inner)

        # Log
        self._build_log()

        # Action bar
        self._build_bar()

    # -- Settings -------------------------------------------------------------

    def _build_settings(self, p):
        p.columnconfigure(0, weight=1)
        row = 0
        row = self._section(p, row, "Input / Output",     self._sec_io)
        row = self._section(p, row, "Plate Geometry",     self._sec_geom)
        row = self._section(p, row, "Raster Processing",  self._sec_raster)
        row = self._section(p, row, "Vectorization",      self._sec_vec)
        row = self._section(p, row, "STL Output",         self._sec_stl)
        row = self._section(p, row, "Miscellaneous",      self._sec_misc)

    def _section(self, parent, r, title, builder):
        tk.Label(parent, text=title.upper(), bg=BG, fg=ACCENT,
                 font=("TkDefaultFont", 8, "bold"),
                 anchor="w", padx=4, pady=6).grid(
            row=r, column=0, sticky="ew")
        box = tk.Frame(parent, bg=PANEL,
                       highlightthickness=1, highlightbackground=BORDER)
        box.grid(row=r+1, column=0, sticky="ew", pady=(0, 6))
        box.columnconfigure(1, weight=1)
        builder(box)
        return r + 2

    def _row(self, box, r, label, widget, note=""):
        tk.Label(box, text=label, bg=PANEL, fg=FG,
                 font=("TkDefaultFont", 10), anchor="w",
                 padx=10, pady=5).grid(row=r, column=0, sticky="w")
        widget.grid(row=r, column=1, sticky="ew", padx=(4, 6), pady=3)
        if note:
            tk.Label(box, text=note, bg=PANEL, fg=FG_DIM,
                     font=("TkDefaultFont", 9),
                     anchor="w").grid(row=r, column=2, sticky="w",
                                      padx=(2, 8))

    # Section content ---------------------------------------------------------

    def _sec_io(self, box):
        f0 = tk.Frame(box, bg=PANEL)
        f0.columnconfigure(0, weight=1)
        self._entry(f0, "input").grid(row=0, column=0, sticky="ew")
        self._sbtn(f0, "...", self._browse_in).grid(row=0, column=1, padx=(4,0))
        self._row(box, 0, "Input file", f0)

        f1 = tk.Frame(box, bg=PANEL)
        f1.columnconfigure(0, weight=1)
        self._entry(f1, "output").grid(row=0, column=0, sticky="ew")
        self._sbtn(f1, "...", self._browse_out).grid(row=0, column=1, padx=(4,0))
        self._row(box, 1, "Output STL", f1)

    def _sec_geom(self, box):
        self._row(box, 0, "Width",
            self._spinf(box, "width", 1, 2000, 1.0), "mm")
        self._row(box, 1, "Plate thickness",
            self._spinf(box, "plate_thickness", 0.1, 50, 0.5), "mm")
        self._row(box, 2, "Relief height",
            self._spinf(box, "relief_height", -20, 20, 0.5),
            "mm  (negative = emboss)")
        self._row(box, 3, "Min line width",
            self._spinf(box, "min_line_width", 0.1, 20, 0.1), "mm")

    def _sec_raster(self, box):
        sf = tk.Frame(box, bg=PANEL)
        sf.columnconfigure(0, weight=1)
        tk.Scale(sf, variable=self._vars["threshold"],
                 from_=0, to=255, orient=tk.HORIZONTAL,
                 bg=PANEL, fg=FG, troughcolor=BORDER,
                 activebackground=ACCENT, highlightthickness=0,
                 bd=0, sliderlength=14, showvalue=True,
                 font=("TkDefaultFont", 9)).grid(row=0, column=0, sticky="ew")
        self._row(box, 0, "Threshold", sf, "0 - 255")
        self._row(box, 1, "Invert", self._chk(box, "invert"))

    def _sec_vec(self, box):
        cb = ttk.Combobox(box, textvariable=self._vars["vectorizer"],
                          values=["potrace", "vtracer"],
                          state="readonly", width=12)
        self._row(box, 0, "Engine", cb)

        self._potrace_box = tk.Frame(box, bg=PANEL)
        self._potrace_box.columnconfigure(1, weight=1)
        self._row(self._potrace_box, 0, "  Blacklevel",
            self._spinf(self._potrace_box, "potrace_threshold", 0, 1, 0.05),
            "0.0 - 1.0")
        self._row(self._potrace_box, 1, "  Turd size",
            self._spini(self._potrace_box, "potrace_turdsize", 0, 100),
            "px  suppress speckles")
        self._row(self._potrace_box, 2, "  Alpha max",
            self._spinf(self._potrace_box, "potrace_alphamax", 0, 1.333, 0.05),
            "0 - 1.333")
        self._row(self._potrace_box, 3, "  Opt tolerance",
            self._spinf(self._potrace_box, "potrace_opttolerance", 0, 2, 0.05))
        self._potrace_box.grid(row=1, column=0, columnspan=3, sticky="ew")

        self._vtracer_box = tk.Frame(box, bg=PANEL)
        self._vtracer_box.columnconfigure(1, weight=1)
        self._row(self._vtracer_box, 0, "  Color precision",
            self._spini(self._vtracer_box, "vtracer_color_precision", 1, 8),
            "bits")
        self._row(self._vtracer_box, 1, "  Filter speckle",
            self._spini(self._vtracer_box, "vtracer_filter_speckle", 0, 128),
            "px")
        self._vtracer_box.grid(row=1, column=0, columnspan=3, sticky="ew")

    def _sec_stl(self, box):
        self._row(box, 0, "ASCII STL", self._chk(box, "stl_ascii"),
                  "binary is smaller and faster")
        self._row(box, 1, "Curve segments",
                  self._spini(box, "curve_segments", 4, 128), "per curve")

    def _sec_misc(self, box):
        self._row(box, 0, "Verbose logging", self._chk(box, "verbose"))

    # -- Log ------------------------------------------------------------------

    def _build_log(self):
        outer = tk.Frame(self, bg=BG)
        outer.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 4))
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        tk.Label(outer, text="OUTPUT", bg=BG, fg=ACCENT,
                 font=("TkDefaultFont", 8, "bold"),
                 anchor="w", padx=4).grid(row=0, column=0, sticky="ew")

        box = tk.Frame(outer, bg=PANEL,
                       highlightthickness=1, highlightbackground=BORDER)
        box.grid(row=1, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        self._log = tk.Text(
            box, bg=LOG_BG, fg=FG, insertbackground=FG,
            font=("Consolas", 10), relief=tk.FLAT, bd=0,
            wrap=tk.WORD, state=tk.DISABLED,
            selectbackground=ACCENT, selectforeground=BTN_FG,
            padx=8, pady=6)
        sb = ttk.Scrollbar(box, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        self._log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        for tag, col in [("INFO", FG), ("DEBUG", FG_DIM), ("WARN", WARN),
                         ("ERROR", ERR), ("FATAL", ERR), ("ok", ACCENT2)]:
            self._log.tag_configure(tag, foreground=col)

    # -- Action bar -----------------------------------------------------------

    def _build_bar(self):
        bar = tk.Frame(self, bg=PANEL,
                       highlightthickness=1, highlightbackground=BORDER)
        bar.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))
        bar.columnconfigure(2, weight=1)

        for col, key, text in [(0, "overwrite",     "Overwrite output"),
                               (1, "open_after_run", "Open STL after run")]:
            tk.Checkbutton(bar, text=text, variable=self._vars[key],
                           bg=PANEL, fg=FG,
                           activebackground=PANEL, activeforeground=FG,
                           selectcolor=BORDER,
                           font=("TkDefaultFont", 10),
                           relief=tk.FLAT, bd=0, pady=6).grid(
                row=0, column=col, padx=(10, 2))

        tk.Button(bar, text="Reset to defaults", command=self._reset,
                  bg=BORDER, fg=BTN_FG,
                  activebackground=FG_DIM, activeforeground=BTN_FG,
                  relief=tk.FLAT, bd=0, padx=12, pady=7,
                  font=("TkDefaultFont", 10), cursor="hand2").grid(
            row=0, column=3, padx=(0, 8), pady=6)

        self._run_btn = tk.Button(
            bar, text="Run pyBlock", command=self._run,
            bg=ACCENT, fg=BTN_FG,
            activebackground="#9d8fff", activeforeground=BTN_FG,
            relief=tk.FLAT, bd=0, padx=16, pady=7,
            font=("TkDefaultFont", 11, "bold"), cursor="hand2")
        self._run_btn.grid(row=0, column=4, padx=(0, 10), pady=6)

    # -- Widget factories -----------------------------------------------------

    def _entry(self, parent, key):
        return tk.Entry(parent, textvariable=self._vars[key],
                        bg=LOG_BG, fg=FG, insertbackground=FG,
                        relief=tk.FLAT, bd=3,
                        font=("TkDefaultFont", 10),
                        highlightthickness=1,
                        highlightbackground=BORDER,
                        highlightcolor=ACCENT)

    def _spinf(self, parent, key, lo, hi, step):
        return ttk.Spinbox(parent, textvariable=self._vars[key],
                           from_=lo, to=hi, increment=step,
                           width=10, format="%.2f")

    def _spini(self, parent, key, lo, hi):
        return ttk.Spinbox(parent, textvariable=self._vars[key],
                           from_=lo, to=hi, increment=1, width=10)

    def _chk(self, parent, key):
        return tk.Checkbutton(parent, variable=self._vars[key],
                              bg=PANEL, fg=FG,
                              activebackground=PANEL, activeforeground=FG,
                              selectcolor=BORDER, relief=tk.FLAT, bd=0)

    def _sbtn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd,
                         bg=BORDER, fg=FG,
                         activebackground=ACCENT, activeforeground=BTN_FG,
                         relief=tk.FLAT, bd=0, padx=8, pady=2,
                         font=("TkDefaultFont", 10), cursor="hand2")

    # -- Logic ----------------------------------------------------------------

    def _auto_output(self):
        inp = self._vars["input"].get().strip()
        if not inp:
            return
        auto = os.path.splitext(inp)[0] + ".stl"
        cur  = self._vars["output"].get().strip()
        if not cur or cur == auto:
            self._vars["output"].set(auto)

    def _update_vec_panel(self):
        vec = self._vars["vectorizer"].get()
        if vec == "potrace":
            self._vtracer_box.grid_remove()
            self._potrace_box.grid()
        else:
            self._potrace_box.grid_remove()
            self._vtracer_box.grid()

    def _reset(self):
        for k, v in self._vars.items():
            if k in ("input", "output"):
                continue
            try:
                v.set(DEFAULTS[k])
            except (tk.TclError, ValueError):
                pass

    def _browse_in(self):
        p = filedialog.askopenfilename(
            title="Select input file",
            filetypes=[
                ("Supported files",
                 "*.svg *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif"),
                ("SVG files",   "*.svg"),
                ("Image files", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif"),
                ("All files",   "*.*"),
            ])
        if p:
            self._vars["input"].set(p)

    def _browse_out(self):
        inp = self._vars["input"].get()
        p = filedialog.asksaveasfilename(
            title="Save STL as",
            initialdir=os.path.dirname(inp) if inp else "",
            initialfile=os.path.splitext(os.path.basename(inp))[0] + ".stl"
                        if inp else "output.stl",
            defaultextension=".stl",
            filetypes=[("STL files", "*.stl"), ("All files", "*.*")])
        if p:
            self._vars["output"].set(p)

    def _on_close(self):
        save_settings(self._collect())
        self.destroy()

    # -- Run ------------------------------------------------------------------

    def _build_cmd(self):
        inp = self._vars["input"].get().strip()
        out = self._vars["output"].get().strip()

        if not inp:
            messagebox.showerror("No input", "Please select an input file.")
            return None
        if not os.path.isfile(inp):
            messagebox.showerror("File not found",
                                 f"Input file does not exist:\n{inp}")
            return None
        if not out:
            messagebox.showerror("No output",
                                 "Please specify an output file path.")
            return None
        if os.path.isfile(out) and not self._vars["overwrite"].get():
            if not messagebox.askyesno("Overwrite?",
                f"Output file already exists:\n{out}\n\nOverwrite it?"):
                return None

        cmd = [
            sys.executable, PYBLOCK_PY,
            "--input",  inp, "--output", out,
            "--width",              str(self._vars["width"].get()),
            "--plate-thickness",    str(self._vars["plate_thickness"].get()),
            "--relief-height",      str(self._vars["relief_height"].get()),
            "--min-line-width",     str(self._vars["min_line_width"].get()),
            "--threshold",          str(self._vars["threshold"].get()),
            "--vectorizer",         self._vars["vectorizer"].get(),
            "--potrace-threshold",  str(self._vars["potrace_threshold"].get()),
            "--potrace-turdsize",   str(self._vars["potrace_turdsize"].get()),
            "--potrace-alphamax",   str(self._vars["potrace_alphamax"].get()),
            "--potrace-opttolerance",
                                    str(self._vars["potrace_opttolerance"].get()),
            "--vtracer-color-precision",
                                    str(self._vars["vtracer_color_precision"].get()),
            "--vtracer-filter-speckle",
                                    str(self._vars["vtracer_filter_speckle"].get()),
            "--curve-segments",     str(self._vars["curve_segments"].get()),
        ]
        if self._vars["invert"].get():       cmd.append("--invert")
        if self._vars["stl_ascii"].get():    cmd.append("--stl-ascii")
        if self._vars["verbose"].get():      cmd.append("--verbose")
        return cmd

    def _run(self):
        if self._proc is not None:
            self._proc.terminate()
            return
        cmd = self._build_cmd()
        if cmd is None:
            return
        self._clear_log()
        self._log_line(f"$ {' '.join(cmd)}\n", "DEBUG")
        self._output_path = self._vars["output"].get().strip()
        self._run_btn.configure(text="Cancel", bg=ERR)
        threading.Thread(target=self._worker, args=(cmd,), daemon=True).start()

    def _worker(self, cmd):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
            self._proc = proc
            for line in proc.stdout:
                self._q.put(("line", line))
            proc.wait()
            self._q.put(("done", proc.returncode))
        except Exception as e:
            self._q.put(("error", str(e)))

    def _poll(self):
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind == "line":
                    self._log_line(data)
                elif kind == "done":
                    self._proc = None
                    self._run_btn.configure(text="Run pyBlock", bg=ACCENT)
                    rc, exists = data, os.path.isfile(self._output_path)
                    if rc == 0 and exists:
                        self._log_line("\nFinished successfully.\n", "ok")
                        if self._vars["open_after_run"].get():
                            self._open_stl(self._output_path)
                    else:
                        self._log_line(
                            f"\npyBlock exited with code {rc}.\n", "ERROR")
                        if not exists:
                            messagebox.showerror("pyBlock failed",
                                f"pyBlock exited with code {rc} and no STL "
                                f"was created.\n\nSee the output log.")
                elif kind == "error":
                    self._proc = None
                    self._run_btn.configure(text="Run pyBlock", bg=ACCENT)
                    self._log_line(f"\nFailed to launch: {data}\n", "ERROR")
                    messagebox.showerror("Launch error", data)
        except queue.Empty:
            pass
        self.after(50, self._poll)

    # -- Log ------------------------------------------------------------------

    _LEVEL = re.compile(r"\[(INFO |WARN |ERROR|DEBUG|FATAL)\]")

    def _log_line(self, text, tag=""):
        clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
        if not tag:
            m = self._LEVEL.search(clean)
            tag = m.group(1).strip() if m else "INFO"
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, clean, tag)
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _clear_log(self):
        self._log.configure(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.configure(state=tk.DISABLED)

    # -- Open STL -------------------------------------------------------------

    def _open_stl(self, path):
        try:
            sys_ = platform.system()
            if sys_ == "Windows":
                os.startfile(path)
            elif sys_ == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showwarning("Could not open STL",
                                   f"Failed to open:\n{path}\n\n{e}")


# -- Entry point --------------------------------------------------------------

if __name__ == "__main__":
    App().mainloop()
