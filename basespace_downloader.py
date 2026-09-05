import csv
import io
import os
import re
import sys
import shutil
import queue
import threading
import subprocess
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "BaseSpace File Downloader"

SIZE_TOKEN_RE = re.compile(r"^\d+(\.\d+)?\s*[KMGT]?i?B$", re.IGNORECASE)


def creationflags():
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def bytes_from_unit(value, unit):
    unit = unit.upper()
    factors = {
        "B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
        "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
    }
    return float(value) * factors.get(unit, 1)


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.2f} {unit}"
        n /= 1024


def parse_size_str(s):
    m = re.match(r"^(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?i?B)$", s.strip(), re.IGNORECASE)
    if not m:
        return None
    return bytes_from_unit(m.group("val"), m.group("unit"))


def parse_extensions(raw):
    """Turn the extension field into a list of bare extensions (no leading
    dot). Accepts a single value, a comma-separated list, or 'All files'/'*'/
    empty to mean "no filter"."""
    raw = raw.strip()
    if not raw or raw.lower() in ("all files", "*"):
        return []
    return [p.strip().lstrip(".") for p in raw.split(",") if p.strip()]


@dataclass
class Project:
    name: str
    project_id: str

    def label(self):
        return f"{self.name}  |  {self.project_id}"


@dataclass
class RemoteFile:
    file_id: str
    path: str
    size_bytes: float = None
    size_str: str = "—"

    @property
    def name(self):
        return self.path.rsplit("/", 1)[-1]


class BaseSpaceDownloader(ttk.Frame):
    def __init__(self, master=None, show_header=True, configure_window=True):
        super().__init__(master)
        self.show_header = show_header
        if configure_window:
            window = self.winfo_toplevel()
            window.title(APP_TITLE)
            window.geometry("1080x820")
            window.minsize(960, 700)

        self.events = queue.Queue()
        self.current_process = None
        self.download_active = False
        self.cancel_requested = threading.Event()
        self.projects = {}
        self.all_files = []          # RemoteFile objects from the last fetch
        self.item_to_file = {}       # treeview item id -> RemoteFile
        self.authenticated = False

        self.bs_path_var = tk.StringVar(value=self.default_bs_path())
        self.auth_status_var = tk.StringVar(value="Not checked")
        self.user_var = tk.StringVar(value="—")
        self.project_var = tk.StringVar()
        self.extension_var = tk.StringVar(value="fastq.gz")
        self.name_filter_var = tk.StringVar()
        self.outdir_var = tk.StringVar()

        self.file_count_var = tk.StringVar(value="0")
        self.selected_count_var = tk.StringVar(value="0")
        self.selected_size_var = tk.StringVar(value="0 B")
        self.status_var = tk.StringVar(value="Not authenticated")
        self.progress_text_var = tk.StringVar(value="0 / 0")
        self.current_file_var = tk.StringVar(value="Waiting to start")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build_ui()
        self.status_var.trace_add("write", self._refresh_status_banner)
        self.name_filter_var.trace_add("write", lambda *_: self._apply_name_filter())
        self.after(100, self._process_events)

    # ------------------------------------------------------------------ #
    # Setup helpers
    # ------------------------------------------------------------------ #

    def default_bs_path(self):
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        candidate = base / "bs.exe"
        if candidate.exists():
            return str(candidate)
        local = Path.cwd() / "bs.exe"
        if local.exists():
            return str(local)
        found = shutil.which("bs.exe") or shutil.which("bs")
        return found or str(local)

    def bs_executable(self):
        path = self.bs_path_var.get().strip().strip('"')
        if not path:
            raise FileNotFoundError("bs.exe path is empty.")
        if Path(path).exists():
            return path
        found = shutil.which(path)
        if found:
            return found
        raise FileNotFoundError(f"Cannot find BaseSpace CLI: {path}")

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Muted.TLabel", foreground="#5f6b7a")
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("CardValue.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("CardLabel.TLabel", foreground="#5f6b7a", font=("Segoe UI", 8))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(18, 8))
        style.map("Primary.TButton", foreground=[("disabled", "#7a8490"), ("!disabled", "#0b4f79")])

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        if self.show_header:
            ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
            ttk.Label(
                outer,
                text="Authenticate  →  choose project  →  fetch file list  →  select  →  download",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(2, 10))

        self.status_banner = tk.Label(
            outer, text="NOT AUTHENTICATED", anchor="w", padx=14, pady=9,
            font=("Segoe UI", 11, "bold"), bg="#e9ecef", fg="#343a40",
        )
        self.status_banner.pack(fill="x", pady=(0, 8))

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 6))
        left = ttk.Frame(top)
        right = ttk.Frame(top)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        top.columnconfigure(0, weight=1, uniform="cols")
        top.columnconfigure(1, weight=1, uniform="cols")

        # -- Account -----------------------------------------------------
        auth = ttk.LabelFrame(left, text="1. BaseSpace account", padding=10, style="Section.TLabelframe")
        auth.pack(fill="x", pady=(0, 10))
        ttk.Label(auth, text="bs.exe").grid(row=0, column=0, sticky="w")
        ttk.Entry(auth, textvariable=self.bs_path_var).grid(row=0, column=1, columnspan=2, sticky="ew", padx=8)
        ttk.Button(auth, text="Browse", command=self.browse_bs).grid(row=0, column=3)
        ttk.Button(auth, text="Authenticate", command=self.authenticate).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Button(auth, text="Check Login", command=self.check_login).grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(auth, text="Status:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Label(auth, textvariable=self.auth_status_var).grid(row=2, column=1, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(auth, text="User:").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Label(auth, textvariable=self.user_var).grid(row=3, column=1, columnspan=3, sticky="w", pady=(4, 0))
        auth.columnconfigure(1, weight=1)
        auth.columnconfigure(2, weight=1)

        # -- Project -------------------------------------------------------
        project = ttk.LabelFrame(left, text="2. Source project", padding=10, style="Section.TLabelframe")
        project.pack(fill="x", pady=(0, 10))
        self.project_combo = ttk.Combobox(project, textvariable=self.project_var, state="normal")
        self.project_combo.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(project, text="Refresh Projects", command=self.load_projects).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(project, text="Select a project or type its numeric Project ID.", style="Muted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        project.columnconfigure(0, weight=1)

        # -- Output --------------------------------------------------------
        outdir = ttk.LabelFrame(right, text="3. Output folder", padding=10, style="Section.TLabelframe")
        outdir.pack(fill="x", pady=(0, 10))
        ttk.Entry(outdir, textvariable=self.outdir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(outdir, text="Browse", command=self.browse_outdir).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(outdir, text="Defaults to BaseSpace_Project_<ID>_FASTQ next to this app.", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        outdir.columnconfigure(0, weight=1)

        # -- Filters ---------------------------------------------------
        filters = ttk.LabelFrame(right, text="4. File type / name filter", padding=10, style="Section.TLabelframe")
        filters.pack(fill="x", pady=(0, 10))
        ttk.Label(filters, text="Extension").grid(row=0, column=0, sticky="w")
        self.extension_entry = ttk.Entry(filters, textvariable=self.extension_var)
        self.extension_entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(
            filters, text="Enter one extension (e.g. txt), a comma-separated list (bam,bai,vcf.gz), or * for all files.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Label(filters, text="Name contains").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(filters, textvariable=self.name_filter_var).grid(row=2, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
        ttk.Button(filters, text="Fetch File List", command=self.fetch_files, style="Primary.TButton").grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0)
        )
        filters.columnconfigure(1, weight=1)

        # -- File list -------------------------------------------------
        files_frame = ttk.LabelFrame(outer, text="5. Files (ctrl/shift-click to multi-select)", padding=10, style="Section.TLabelframe")
        files_frame.pack(fill="both", expand=True, pady=(0, 6))

        toolbar = ttk.Frame(files_frame)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="Select All", command=self.select_all).pack(side="left")
        ttk.Button(toolbar, text="Select None", command=self.select_none).pack(side="left", padx=(6, 0))
        self.start_btn = ttk.Button(
            toolbar, text="START DOWNLOAD", command=self.start_download, state="disabled", style="Primary.TButton"
        )
        self.start_btn.pack(side="left", padx=(12, 0))
        self.cancel_btn = ttk.Button(toolbar, text="Cancel", command=self.cancel_download, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Retry Failed", command=self.select_failed).pack(side="left", padx=(6, 0))

        stats = ttk.Frame(toolbar)
        stats.pack(side="right")
        self._stat(stats, 0, "FILES LISTED", self.file_count_var)
        self._stat(stats, 1, "SELECTED", self.selected_count_var)
        self._stat(stats, 2, "SELECTED SIZE", self.selected_size_var)

        progress_row = ttk.Frame(files_frame)
        progress_row.pack(fill="x", pady=(0, 6))
        self.progress = ttk.Progressbar(progress_row, variable=self.progress_var, maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(progress_row, textvariable=self.progress_text_var, font=("Segoe UI", 10, "bold")).pack(
            side="right", padx=(10, 0)
        )
        ttk.Label(files_frame, textvariable=self.current_file_var, style="Muted.TLabel").pack(
            fill="x", pady=(0, 5)
        )

        columns = ("id", "name", "size", "path")
        self.tree = ttk.Treeview(files_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("id", text="File ID")
        self.tree.heading("name", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("path", text="Path")
        self.tree.column("id", width=100, anchor="w")
        self.tree.column("name", width=340, anchor="w")
        self.tree.column("size", width=100, anchor="e")
        self.tree.column("path", width=380, anchor="w")
        tree_scroll = ttk.Scrollbar(files_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda *_: self._update_selection_stats())

        # -- Log -----------------------------------------------------------
        self.logs_frame = ttk.LabelFrame(outer, text="Activity log", padding=8, style="Section.TLabelframe")
        self.logs_frame.pack(fill="x", pady=(0, 4), before=files_frame)
        log_toolbar = ttk.Frame(self.logs_frame)
        log_toolbar.pack(fill="x", pady=(0, 6))
        self.log_toggle_btn = ttk.Button(log_toolbar, text="Show log", command=self.toggle_activity_log)
        self.log_toggle_btn.pack(side="left")
        ttk.Button(log_toolbar, text="Open failed-downloads log", command=self.open_failed_log).pack(side="left", padx=(6, 0))
        ttk.Button(log_toolbar, text="Save log", command=self.save_activity_log).pack(side="right")
        ttk.Button(log_toolbar, text="Clear", command=self.clear_activity_log).pack(side="right", padx=(0, 6))
        self.log_body = ttk.Frame(self.logs_frame)
        self.log = tk.Text(self.log_body, height=7, wrap="word", font=("Consolas", 9), relief="flat")
        scroll = ttk.Scrollbar(self.log_body, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.failed_files = []

    def _stat(self, parent, col, label, variable):
        frame = ttk.Frame(parent, padding=(8, 0))
        frame.grid(row=0, column=col, sticky="nsew", padx=(0, 4))
        ttk.Label(frame, text=label, style="CardLabel.TLabel").pack(anchor="e")
        ttk.Label(frame, textvariable=variable, style="CardValue.TLabel").pack(anchor="e")

    def _refresh_status_banner(self, *_):
        if not hasattr(self, "status_banner"):
            return
        status = self.status_var.get()
        lowered = status.lower()
        if "completed" in lowered or "ready" in lowered and "not ready" not in lowered:
            bg, fg = "#d1fadf", "#05603a"
        elif "downloading" in lowered or "fetching" in lowered or "checking" in lowered or "loading" in lowered:
            bg, fg = "#dbeafe", "#1e40af"
        elif "error" in lowered or "failed" in lowered:
            bg, fg = "#fee4e2", "#b42318"
        elif "cancel" in lowered or "warning" in lowered:
            bg, fg = "#fef0c7", "#93370d"
        else:
            bg, fg = "#e9ecef", "#343a40"
        self.status_banner.configure(text=status.upper(), bg=bg, fg=fg)

    def clear_activity_log(self):
        self.log.delete("1.0", "end")

    def toggle_activity_log(self):
        if self.log_body.winfo_manager():
            self.log_body.pack_forget()
            self.logs_frame.pack_configure(fill="x", expand=False)
            self.log_toggle_btn.configure(text="Show log")
        else:
            self.log_body.pack(fill="both", expand=True)
            self.logs_frame.pack_configure(fill="both", expand=True)
            self.log_toggle_btn.configure(text="Hide log")

    def _append_log(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def save_activity_log(self):
        path = filedialog.asksaveasfilename(
            title="Save activity log", defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log.get("1.0", "end-1c"), encoding="utf-8")
            self.status_var.set("Activity log saved")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not save the activity log:\n\n{exc}")

    def open_failed_log(self):
        outdir = Path(self.outdir_var.get().strip() or ".")
        report = outdir / "failed_downloads.tsv"
        if not report.is_file():
            messagebox.showinfo(APP_TITLE, "No failed-downloads log is available for this output folder.")
            return
        try:
            if os.name == "nt":
                os.startfile(report)
            else:
                webbrowser.open(report.resolve().as_uri())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not open the log:\n\n{exc}")

    def browse_bs(self):
        path = filedialog.askopenfilename(
            title="Select bs.exe",
            filetypes=[("BaseSpace CLI", "bs.exe"), ("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.bs_path_var.set(path)

    def browse_outdir(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.outdir_var.set(folder)

    # ------------------------------------------------------------------ #
    # Process running (auth / list / contents) — captures full output
    # ------------------------------------------------------------------ #

    def run_capture(self, args, callback=None, purpose="command", open_auth_url=False):
        def worker():
            try:
                exe = self.bs_executable()
                cmd = [exe] + args
                self.events.put(("log", "\n> " + subprocess.list2cmdline(cmd) + "\n"))
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=creationflags(),
                )
                output = []
                opened = False
                for line in proc.stdout:
                    output.append(line)
                    self.events.put(("log", line))
                    if open_auth_url and not opened:
                        m = re.search(r"https?://[^\s]+", line)
                        if m:
                            url = m.group(0).rstrip(".,)")
                            opened = True
                            try:
                                webbrowser.open(url)
                                self.events.put(("log", f"Opened authentication URL in browser: {url}\n"))
                            except Exception:
                                pass
                code = proc.wait()
                self.events.put(("capture_done", purpose, code, "".join(output), callback))
            except Exception as exc:
                self.events.put(("error", f"{purpose}: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def authenticate(self):
        self.auth_status_var.set("Authenticating…")
        self.status_var.set("Waiting for BaseSpace authentication")
        self.run_capture(["auth"], callback=self._auth_finished, purpose="auth", open_auth_url=True)

    def _auth_finished(self, code, output):
        if code == 0:
            self.check_login()
        else:
            self.authenticated = False
            self.auth_status_var.set("Authentication failed")
            self.status_var.set("Authentication failed")

    def check_login(self):
        self.auth_status_var.set("Checking…")
        self.run_capture(["whoami"], callback=self._whoami_finished, purpose="whoami")

    def _whoami_finished(self, code, output):
        if code != 0:
            self.authenticated = False
            self.auth_status_var.set("Not authenticated")
            self.user_var.set("—")
            self.status_var.set("Not authenticated")
            return
        name = ""
        for line in output.splitlines():
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) >= 2 and cols[0].lower() == "name":
                name = cols[1]
        self.user_var.set(name or "Authenticated BaseSpace user")
        self.authenticated = True
        self.auth_status_var.set("Authenticated")
        self.status_var.set("Authenticated — pick a project")

    # ------------------------------------------------------------------ #
    # Projects
    # ------------------------------------------------------------------ #

    def load_projects(self):
        self.status_var.set("Loading projects…")
        self.run_capture(["list", "projects"], callback=self._projects_finished, purpose="list projects")

    def _projects_finished(self, code, output):
        if code != 0:
            self.status_var.set("Could not load projects")
            return
        projects = self.parse_pipe_table(output, id_col=1, name_col=0)
        self.projects = {f"{name}  |  {pid}": Project(name, pid) for pid, name in projects}
        labels = list(self.projects.keys())
        self.project_combo["values"] = labels
        if labels:
            if self.project_var.get() not in labels:
                self.project_var.set(labels[0])
            self.status_var.set(f"Loaded {len(labels)} projects")
        else:
            self.status_var.set("No projects parsed; type a Project ID manually")

    @staticmethod
    def parse_pipe_table(output, id_col, name_col):
        """Legacy fallback parser for bs's human-readable '|' tables."""
        found = []
        for line in output.splitlines():
            if "|" not in line:
                continue
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            cols = [c for c in cols if c]
            if len(cols) <= max(id_col, name_col):
                continue
            lowered = [c.lower() for c in cols]
            if any(v in {"name", "id", "project", "project id", "project name"} for v in lowered):
                continue
            if all(re.fullmatch(r"[-:]+", c) for c in cols):
                continue
            pid, name = cols[id_col], cols[name_col]
            if pid.isdigit():
                found.append((pid, name))
        return found

    def selected_project_id(self):
        value = self.project_var.get().strip()
        if value in self.projects:
            return self.projects[value].project_id
        if value.isdigit():
            return value
        m = re.search(r"\|\s*(\d+)\s*$", value)
        if m:
            return m.group(1)
        raise ValueError("Select a project or type its numeric Project ID.")

    # ------------------------------------------------------------------ #
    # Fetching file list
    # ------------------------------------------------------------------ #

    def fetch_files(self):
        try:
            exe = self.bs_executable()
            project_id = self.selected_project_id()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        exts = parse_extensions(self.extension_var.get())
        # Only pass --extension to the CLI when there's exactly one value —
        # multi-extension / no-extension filtering is done client-side below,
        # since we can't assume the CLI supports a comma-separated list.
        cli_ext = exts[0] if len(exts) == 1 else ""

        if not self.outdir_var.get().strip():
            self.outdir_var.set(f"BaseSpace_Project_{project_id}_FASTQ")

        self.status_var.set("Fetching file list…")
        self.tree.delete(*self.tree.get_children())
        self.all_files = []
        self.item_to_file = {}

        def worker():
            try:
                # Attempt 1: CSV output, easier and more robust to parse.
                cmd = [exe, "contents", "project", "-i", project_id, "-f", "csv"]
                if cli_ext:
                    cmd.append(f"--extension={cli_ext}")
                self.events.put(("log", "\n> " + subprocess.list2cmdline(cmd) + "\n"))
                proc = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=creationflags(),
                )
                files = self.parse_contents_csv(proc.stdout)

                if not files:
                    # Attempt 2: legacy '|' table, same column positions the
                    # working bash script relies on (id in col 2, path in col 3).
                    cmd2 = [exe, "contents", "project", "-i", project_id]
                    if cli_ext:
                        cmd2.append(f"--extension={cli_ext}")
                    self.events.put(("log", "\n> " + subprocess.list2cmdline(cmd2) + "\n"))
                    proc2 = subprocess.run(
                        cmd2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        creationflags=creationflags(),
                    )
                    files = self.parse_contents_legacy(proc2.stdout)
                    if not files and proc2.returncode != 0:
                        self.events.put(("log", proc2.stdout + "\n"))
                        self.events.put(("fetch_done", None, "Could not list project contents (see log)."))
                        return

                # Client-side extension filter — always applied, so a
                # comma-separated list (or an extension the CLI's own
                # --extension flag didn't recognize) still narrows results.
                if exts:
                    lowered_exts = [e.lower() for e in exts]
                    files = [f for f in files if any(f.path.lower().endswith(e) for e in lowered_exts)]

                self.events.put(("fetch_done", files, None))
            except Exception as exc:
                self.events.put(("fetch_done", None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def parse_contents_csv(output):
        """Try to parse '-f csv' output. Column names vary by CLI version,
        so match loosely on any header containing 'id', 'path'/'name', 'size'."""
        lines = [l for l in output.splitlines() if l.strip()]
        if not lines:
            return []
        try:
            rows = list(csv.reader(lines))
        except Exception:
            return []
        if not rows:
            return []
        header = [h.strip().lower() for h in rows[0]]
        id_idx = next((i for i, h in enumerate(header) if "id" in h), None)
        path_idx = next((i for i, h in enumerate(header) if "path" in h), None)
        if path_idx is None:
            path_idx = next((i for i, h in enumerate(header) if "name" in h), None)
        size_idx = next((i for i, h in enumerate(header) if "size" in h), None)
        if id_idx is None or path_idx is None:
            return []

        results = []
        for r in rows[1:]:
            if len(r) <= max(id_idx, path_idx):
                continue
            file_id = r[id_idx].strip()
            path = r[path_idx].strip()
            if not file_id.isdigit() or not path:
                continue
            size_bytes = None
            size_str = "—"
            if size_idx is not None and size_idx < len(r):
                raw = r[size_idx].strip()
                if raw.isdigit():
                    size_bytes = float(raw)
                    size_str = human_size(size_bytes)
                elif SIZE_TOKEN_RE.match(raw):
                    size_bytes = parse_size_str(raw)
                    size_str = raw
            results.append(RemoteFile(file_id, path, size_bytes, size_str))
        return results

    @staticmethod
    def parse_contents_legacy(output):
        """Mirrors the proven bash-script parsing: id in column 2, path in
        column 3 of bs's '|'-delimited table, plus a best-effort size guess
        from any remaining column that looks like '123.45 MB'."""
        results = []
        for line in output.splitlines():
            if "|" not in line:
                continue
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            cols = [c for c in cols if c != ""]
            if len(cols) < 3:
                continue
            file_id, path = cols[1], cols[2]
            if not file_id.isdigit() or not path:
                continue
            size_bytes, size_str = None, "—"
            for c in cols:
                if SIZE_TOKEN_RE.match(c):
                    size_bytes = parse_size_str(c)
                    size_str = c
                    break
            results.append(RemoteFile(file_id, path, size_bytes, size_str))
        return results

    def _fetch_done(self, files, error):
        if error:
            self.status_var.set("Could not fetch file list")
            messagebox.showerror(APP_TITLE, error)
            return
        self.all_files = files or []
        self._apply_name_filter()
        if not self.all_files:
            self.status_var.set("No matching files found in this project")
        else:
            self.status_var.set(f"Found {len(self.all_files)} file(s) — ready to select")

    def _apply_name_filter(self):
        needle = self.name_filter_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self.item_to_file = {}
        shown = 0
        for f in self.all_files:
            if needle and needle not in f.name.lower():
                continue
            item = self.tree.insert("", "end", values=(f.file_id, f.name, f.size_str, f.path))
            self.item_to_file[item] = f
            shown += 1
        self.file_count_var.set(str(shown))
        self._update_selection_stats()

    # ------------------------------------------------------------------ #
    # Selection helpers
    # ------------------------------------------------------------------ #

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def select_none(self):
        self.tree.selection_remove(self.tree.get_children())

    def select_failed(self):
        if not self.failed_files:
            messagebox.showinfo(APP_TITLE, "No failed downloads to retry yet.")
            return
        failed_ids = {f.file_id for f in self.failed_files}
        matches = [item for item, f in self.item_to_file.items() if f.file_id in failed_ids]
        self.tree.selection_set(matches)
        self.status_var.set(f"Selected {len(matches)} previously failed file(s) — ready to retry")

    def _update_selection_stats(self):
        selected = [self.item_to_file[i] for i in self.tree.selection() if i in self.item_to_file]
        self.selected_count_var.set(str(len(selected)))
        total = sum(f.size_bytes for f in selected if f.size_bytes)
        self.selected_size_var.set(human_size(total) if total else "—")
        self.start_btn.configure(state="normal" if selected and not self.download_active else "disabled")

    # ------------------------------------------------------------------ #
    # Download
    # ------------------------------------------------------------------ #

    def start_download(self):
        if self.current_process is not None:
            messagebox.showinfo(APP_TITLE, "A download is already running.")
            return
        try:
            exe = self.bs_executable()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        selected = [self.item_to_file[i] for i in self.tree.selection() if i in self.item_to_file]
        if not selected:
            messagebox.showerror(APP_TITLE, "Select at least one file to download.")
            return

        outdir = Path(self.outdir_var.get().strip() or "BaseSpace_Download")
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not create output folder:\n\n{exc}")
            return

        total_size = sum(f.size_bytes for f in selected if f.size_bytes)
        answer = messagebox.askyesno(
            APP_TITLE,
            f"Download {len(selected)} file(s)?\n\n"
            f"Output folder: {outdir}\n"
            f"Approx. total size: {human_size(total_size) if total_size else 'unknown'}",
        )
        if not answer:
            return

        self.cancel_requested.clear()
        self.failed_files = []
        self.progress_var.set(0)
        self.progress_text_var.set(f"0 / {len(selected)}")
        self.current_file_var.set("Starting…")
        self.download_active = True
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.status_var.set("Downloading")

        def worker():
            success, failed = 0, []
            for idx, f in enumerate(selected, start=1):
                if self.cancel_requested.is_set():
                    self.events.put(("log", "\nDownload cancelled by user.\n"))
                    break

                self.events.put(("download_progress", idx, len(selected), f.name))
                cmd = [exe, "download", "file", "-i", f.file_id, "-o", str(outdir)]
                self.events.put(("log", "\n> " + subprocess.list2cmdline(cmd) + "\n"))
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", bufsize=1,
                        creationflags=creationflags(),
                    )
                    self.current_process = proc
                    for line in proc.stdout:
                        self.events.put(("log", line))
                    code = proc.wait()
                    self.current_process = None
                except Exception as exc:
                    self.events.put(("log", f"ERROR launching download: {exc}\n"))
                    code = -1
                    self.current_process = None

                if code == 0:
                    success += 1
                else:
                    failed.append(f)

            self.events.put(("download_done", success, failed, outdir))

        threading.Thread(target=worker, daemon=True).start()

    def cancel_download(self):
        if self.current_process is None and not self.cancel_requested.is_set():
            return
        if not messagebox.askyesno(APP_TITLE, "Cancel remaining downloads? The file in progress will stop too."):
            return
        self.cancel_requested.set()
        self.status_var.set("Cancelling…")
        proc = self.current_process
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _download_progress(self, idx, total, name):
        pct = 100.0 * (idx - 1) / total if total else 0
        self.progress_var.set(pct)
        self.progress_text_var.set(f"{idx - 1} / {total}")
        self.current_file_var.set(f"Downloading ({idx}/{total}): {name}")

    def _download_done(self, success, failed, outdir):
        self.current_process = None
        self.download_active = False
        self.failed_files = failed
        self.cancel_btn.config(state="disabled")
        self._update_selection_stats()
        total = success + len(failed)
        self.progress_var.set(100 if total and not self.cancel_requested.is_set() else self.progress_var.get())
        self.progress_text_var.set(f"{success} / {total}")

        if failed:
            report = Path(outdir) / "failed_downloads.tsv"
            try:
                with open(report, "w", encoding="utf-8") as fh:
                    fh.write("file_id\tpath\n")
                    for f in failed:
                        fh.write(f"{f.file_id}\t{f.path}\n")
                self._append_log(f"\nFailed downloads recorded in: {report}\n")
            except Exception as exc:
                self._append_log(f"\nCould not write failed-downloads log: {exc}\n")

        if self.cancel_requested.is_set():
            self.status_var.set(f"Cancelled — {success} of {total} completed")
            self.current_file_var.set("Download cancelled")
        elif failed:
            self.status_var.set(f"Completed with {len(failed)} failure(s)")
            self.current_file_var.set(f"{success} succeeded, {len(failed)} failed — use Retry Failed")
            messagebox.showwarning(
                APP_TITLE,
                f"{success} file(s) downloaded, {len(failed)} failed.\n\n"
                "Use the 'Retry Failed' button to re-select them, or check "
                "failed_downloads.tsv in the output folder.",
            )
        else:
            self.status_var.set("Download completed")
            self.current_file_var.set("All selected files downloaded")
            messagebox.showinfo(APP_TITLE, f"All {success} file(s) downloaded successfully.")

    # ------------------------------------------------------------------ #
    # Event pump
    # ------------------------------------------------------------------ #

    def _process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(event[1])
                elif kind == "error":
                    self._append_log("\nERROR: " + event[1] + "\n")
                    self.status_var.set("Error")
                    messagebox.showerror(APP_TITLE, event[1])
                elif kind == "capture_done":
                    _, purpose, code, output, callback = event
                    if callback:
                        callback(code, output)
                elif kind == "fetch_done":
                    self._fetch_done(event[1], event[2])
                elif kind == "download_progress":
                    self._download_progress(event[1], event[2], event[3])
                elif kind == "download_done":
                    self._download_done(event[1], event[2], event[3])
        except queue.Empty:
            pass
        self.after(100, self._process_events)


if __name__ == "__main__":
    root = tk.Tk()
    app = BaseSpaceDownloader(root)
    app.pack(fill="both", expand=True)
    root.mainloop()
