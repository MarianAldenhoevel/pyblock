#!/usr/bin/env python3
"""
pyblock-ui.py  -  Tkinter frontend for pyBlock.

Saves settings to pyblock-ui.json next to this script on exit,
reloads them on startup.
"""

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

# -- Locate pyblock.py relative to this script --------------------------------

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
PYBLOCK_PY    = os.path.join(SCRIPT_DIR, "pyblock.py")
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

# -- Log colours (only custom styling kept) -----------------------------------

LOG_BG  = "#13131f"
LOG_FG  = "#cdd6f4"
LOG_DIM = "#7c7fa3"
LOG_WARN = "#f7c35a"
LOG_ERR  = "#f75a7c"
LOG_OK   = "#5af78e"

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

# -- Scrollable frame ---------------------------------------------------------

class ScrollFrame(tk.Frame):
    """A frame whose content scrolls vertically.  Add widgets to self.inner."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._c = tk.Canvas(self, bd=0, highlightthickness=0, takefocus=False)
        self._sb = ttk.Scrollbar(self, orient="vertical", command=self._c.yview)
        self._c.configure(yscrollcommand=self._sb.set)
        self._c.grid(row=0, column=0, sticky="nsew")
        self._sb.grid(row=0, column=1, sticky="ns")

        self.inner = tk.Frame(self._c)
        self.inner.columnconfigure(0, weight=1)
        self._win = self._c.create_window((0, 0), window=self.inner, anchor="nw")

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
        self.minsize(580, 500)
        self.geometry("700x780")

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
        self._vars["input"].trace_add("write", lambda *_: self._auto_output())
        self._vars["vectorizer"].trace_add("write", lambda *_: self._update_vec_panel())

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
        self.rowconfigure(0, weight=1)   # paned window expands

        # PanedWindow: settings (top) + log (bottom) with draggable sash
        self._paned = tk.PanedWindow(self, orient=tk.VERTICAL,
                                     sashwidth=6, sashrelief=tk.RAISED)
        self._paned.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        # Scrollable settings pane
        self._scroll = ScrollFrame(self._paned)
        self._build_settings(self._scroll.inner)
        self._paned.add(self._scroll, stretch="always", minsize=200)

        # Log pane
        self._log_frame = self._make_log_frame(self._paned)
        self._paned.add(self._log_frame, stretch="always", minsize=80)

        # Set initial 2:1 split after geometry is resolved
        self.after(10, self._set_initial_sash)

        # Action bar (outside paned window, always visible)
        self._build_bar()

    def _set_initial_sash(self):
        total = self._paned.winfo_height()
        if total > 1:
            self._paned.sash_place(0, 0, int(total * 2 / 3))
        else:
            self.after(50, self._set_initial_sash)

    # -- Settings -------------------------------------------------------------

    def _build_settings(self, p):
        p.columnconfigure(0, weight=1)
        row = 0
        row = self._section(p, row, "Input / Output",    self._sec_io)
        row = self._section(p, row, "Plate Geometry",    self._sec_geom)
        row = self._section(p, row, "Raster Processing", self._sec_raster)
        row = self._section(p, row, "Vectorization",     self._sec_vec)
        row = self._section(p, row, "STL Output",        self._sec_stl)
        row = self._section(p, row, "Miscellaneous",     self._sec_misc)

    def _section(self, parent, r, title, builder):
        tk.Label(parent, text=title, font=("TkDefaultFont", 9, "bold"),
                 anchor="w", padx=4, pady=4).grid(
            row=r, column=0, sticky="ew")
        box = ttk.LabelFrame(parent, text="")
        box.grid(row=r+1, column=0, sticky="ew", padx=4, pady=(0, 6))
        # col 0 = row label (fixed), col 1 = widget (fixed),
        # col 2 = spacer (weight=1, absorbs slack), col 3 = note (fixed)
        box.columnconfigure(1, weight=0)
        box.columnconfigure(2, weight=1)
        builder(box)
        return r + 2

    def _row(self, box, r, label, widget, note=""):
        tk.Label(box, text=label, anchor="w",
                 padx=8, pady=4).grid(row=r, column=0, sticky="w")
        # File-entry frames (entry + browse btn) span the widget + spacer cols.
        # All other widgets sit at natural width in col 1 only.
        is_stretch = isinstance(widget, tk.Frame)
        if is_stretch:
            widget.grid(row=r, column=1, columnspan=2,
                        sticky="ew", padx=(2, 4), pady=2)
        else:
            widget.grid(row=r, column=1,
                        sticky="w", padx=(2, 4), pady=2)
        if note:
            tk.Label(box, text=note, foreground="grey", anchor="w",
                     font=("TkDefaultFont", 9)).grid(
                row=r, column=3, sticky="w", padx=(2, 8))

    # Section content ---------------------------------------------------------

    def _sec_io(self, box):
        f0 = tk.Frame(box)
        f0.columnconfigure(0, weight=1)
        ttk.Entry(f0, textvariable=self._vars["input"]).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(f0, text="...", width=3,
                   command=self._browse_in).grid(row=0, column=1, padx=(2, 0))
        self._row(box, 0, "Input file", f0)

        f1 = tk.Frame(box)
        f1.columnconfigure(0, weight=1)
        ttk.Entry(f1, textvariable=self._vars["output"]).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(f1, text="...", width=3,
                   command=self._browse_out).grid(row=0, column=1, padx=(2, 0))
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
        sf = tk.Frame(box)
        sf.columnconfigure(0, weight=1)
        tk.Scale(sf, variable=self._vars["threshold"],
                 from_=0, to=255, orient=tk.HORIZONTAL,
                 showvalue=True).grid(row=0, column=0, sticky="ew")
        self._row(box, 0, "Threshold", sf, "0 - 255")
        self._row(box, 1, "Invert", ttk.Checkbutton(box, variable=self._vars["invert"]))

    def _sec_vec(self, box):
        cb = ttk.Combobox(box, textvariable=self._vars["vectorizer"],
                          values=["potrace", "vtracer"],
                          state="readonly", width=12)
        self._row(box, 0, "Engine", cb)

        self._potrace_box = tk.Frame(box)
        self._potrace_box.columnconfigure(1, weight=0)
        self._potrace_box.columnconfigure(2, weight=1)
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

        self._vtracer_box = tk.Frame(box)
        self._vtracer_box.columnconfigure(1, weight=0)
        self._vtracer_box.columnconfigure(2, weight=1)
        self._row(self._vtracer_box, 0, "  Color precision",
            self._spini(self._vtracer_box, "vtracer_color_precision", 1, 8),
            "bits")
        self._row(self._vtracer_box, 1, "  Filter speckle",
            self._spini(self._vtracer_box, "vtracer_filter_speckle", 0, 128),
            "px")
        self._vtracer_box.grid(row=1, column=0, columnspan=3, sticky="ew")

    def _sec_stl(self, box):
        self._row(box, 0, "ASCII STL",
                  ttk.Checkbutton(box, variable=self._vars["stl_ascii"]),
                  "binary is smaller and faster")
        self._row(box, 1, "Curve segments",
                  self._spini(box, "curve_segments", 4, 128), "per curve")

    def _sec_misc(self, box):
        self._row(box, 0, "Verbose logging",
                  ttk.Checkbutton(box, variable=self._vars["verbose"]))

    # -- Log ------------------------------------------------------------------

    def _make_log_frame(self, parent) -> tk.Frame:
        outer = tk.Frame(parent)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        tk.Label(outer, text="Output", font=("TkDefaultFont", 9, "bold"),
                 anchor="w", padx=4).grid(row=0, column=0, sticky="ew")

        box = tk.Frame(outer)
        box.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        self._log = tk.Text(
            box,
            bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG,
            font=("Consolas", 10), relief=tk.SUNKEN, bd=1,
            wrap=tk.WORD, state=tk.DISABLED,
            padx=6, pady=4)
        sb = ttk.Scrollbar(box, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        self._log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        self._log.tag_configure("INFO",  foreground=LOG_FG)
        self._log.tag_configure("DEBUG", foreground=LOG_DIM)
        self._log.tag_configure("WARN",  foreground=LOG_WARN)
        self._log.tag_configure("ERROR", foreground=LOG_ERR)
        self._log.tag_configure("FATAL", foreground=LOG_ERR)
        self._log.tag_configure("ok",    foreground=LOG_OK)

        return outer

    # -- Action bar -----------------------------------------------------------

    def _build_bar(self):
        bar = tk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        bar.columnconfigure(2, weight=1)

        ttk.Checkbutton(bar, text="Overwrite output",
                        variable=self._vars["overwrite"]).grid(
            row=0, column=0, padx=(4, 8), pady=4)
        ttk.Checkbutton(bar, text="Open STL after run",
                        variable=self._vars["open_after_run"]).grid(
            row=0, column=1, padx=(0, 8), pady=4)

        ttk.Button(bar, text="Reset to defaults",
                   command=self._reset).grid(
            row=0, column=3, padx=(0, 6), pady=4)

        self._run_btn = ttk.Button(bar, text="Run pyBlock", command=self._run)
        self._run_btn.grid(row=0, column=4, padx=(0, 4), pady=4)

    # -- Widget factories -----------------------------------------------------

    def _spinf(self, parent, key, lo, hi, step):
        return ttk.Spinbox(parent, textvariable=self._vars[key],
                           from_=lo, to=hi, increment=step,
                           width=10, format="%.2f")

    def _spini(self, parent, key, lo, hi):
        return ttk.Spinbox(parent, textvariable=self._vars[key],
                           from_=lo, to=hi, increment=1, width=10)

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
        if self._vars["invert"].get():    cmd.append("--invert")
        if self._vars["stl_ascii"].get(): cmd.append("--stl-ascii")
        if self._vars["verbose"].get():   cmd.append("--verbose")
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
        self._run_btn.configure(text="Cancel")
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
                    self._run_btn.configure(text="Run pyBlock")
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
                    self._run_btn.configure(text="Run pyBlock")
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
