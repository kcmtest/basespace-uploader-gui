import os
import re
import sys
import time
import queue
import shutil
import threading
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import psutil
except ImportError:
    psutil = None


APP_TITLE = "BaseSpace FASTQ Uploader"
FASTQ_RE = re.compile(
    r"^(?P<sample>.+)_S(?P<sample_no>\d+)_L(?P<lane>\d{3})_R(?P<read>[12])_(?P<set>\d{3})(?:\.(?:fastq|fq))(?:\.gz)?$",
    re.IGNORECASE,
)

FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")

URL_RE = re.compile(r"https?://[^\s]+")
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
FILE_PROGRESS_RE = re.compile(
    r"Uploaded\s+(?P<done>\d+)\s*/\s*(?P<total>\d+)\s+files\s+"
    r"\((?P<pct>[\d.]+)\s*%\)",
    re.IGNORECASE,
)
CREATE_SAMPLE_RE = re.compile(r"Creating\s+sample:\s*(?P<sample>.+?)\s*$", re.IGNORECASE)

# Matches e.g.:
# 877.18 MiB / 933.00 MiB
# 1.07 GiB / 1.11 GiB
SIZE_PROGRESS_RE = re.compile(
    r"(?P<done>\d+(?:\.\d+)?)\s*(?P<done_unit>[KMGT]?i?B)\s*/\s*"
    r"(?P<total>\d+(?:\.\d+)?)\s*(?P<total_unit>[KMGT]?i?B)",
    re.IGNORECASE,
)


def creationflags():
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def bytes_from_unit(value, unit):
    unit = unit.upper()
    factors = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
    }
    return float(value) * factors.get(unit, 1)


def human_size(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.2f} {unit}"
        n /= 1024


@dataclass
class Project:
    name: str
    project_id: str

    def label(self):
        return f"{self.name}  |  {self.project_id}"


class BaseSpaceUploader(ttk.Frame):
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
        self.projects = {}
        self.upload_started_at = None
        self.net_last_bytes = None
        self.net_last_time = None
        self.network_job = None
        self.authenticated = False
        self.last_validation = None
        self.upload_total_files = 0
        self.upload_total_bytes = 0
        self.upload_completed_files = 0
        self.upload_completed_bytes = 0
        self.upload_current_sample = None
        self.upload_current_group_done = 0
        self.upload_sample_file_sizes = {}
        self.upload_completed_samples = set()

        self.bs_path_var = tk.StringVar(value=self.default_bs_path())
        self.auth_status_var = tk.StringVar(value="Not checked")
        self.user_var = tk.StringVar(value="—")
        self.project_var = tk.StringVar()
        self.folder_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        # Invalid FASTQ names are never sent as datasets. BaseSpace will ignore
        # them, while validate_folder records them for later review.
        self.skip_invalid_var = tk.BooleanVar(value=True)

        self.fastq_count_var = tk.StringVar(value="0")
        self.valid_fastq_count_var = tk.StringVar(value="0")
        self.invalid_fastq_count_var = tk.StringVar(value="0")
        self.sample_count_var = tk.StringVar(value="0")
        self.complete_pairs_var = tk.StringVar(value="0")
        self.missing_reads_var = tk.StringVar(value="0")
        self.total_size_var = tk.StringVar(value="0 B")
        self.pair_status_var = tk.StringVar(value="No folder selected")

        self.live_speed_var = tk.StringVar(value="0.00 MiB/s")
        self.status_var = tk.StringVar(value="Not authenticated")
        self.progress_text_var = tk.StringVar(value="0%")
        self.file_progress_var = tk.StringVar(value="Waiting to start")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build_ui()
        self.status_var.trace_add("write", self._refresh_status_banner)
        self.project_var.trace_add("write", lambda *_: self._update_readiness())
        self.bs_path_var.trace_add("write", lambda *_: self._update_readiness())
        self.after(100, self._process_events)

        if psutil is None:
            self._append_log(
                "WARNING: psutil is not installed. Uploading will work, but live network speed will be unavailable.\n"
                "Install it with: pip install psutil\n"
            )

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
                text="Authenticate  →  choose project  →  choose FASTQ folder  →  validate  →  upload",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(2, 10))

        self.status_banner = tk.Label(
            outer, text="NOT AUTHENTICATED", anchor="w", padx=14, pady=9,
            font=("Segoe UI", 11, "bold"), bg="#e9ecef", fg="#343a40",
        )
        self.status_banner.pack(fill="x", pady=(0, 8))

        content = ttk.Frame(outer)
        content.pack(fill="x", pady=(0, 6))
        left_column = ttk.Frame(content)
        right_column = ttk.Frame(content)
        left_column.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right_column.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        content.columnconfigure(0, weight=1, uniform="workflow")
        content.columnconfigure(1, weight=1, uniform="workflow")

        auth = ttk.LabelFrame(left_column, text="1. BaseSpace account", padding=8, style="Section.TLabelframe")
        auth.pack(fill="x", pady=(0, 6))
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

        project = ttk.LabelFrame(left_column, text="2. Destination project", padding=8, style="Section.TLabelframe")
        project.pack(fill="x", pady=(0, 6))
        self.project_combo = ttk.Combobox(project, textvariable=self.project_var, state="normal")
        self.project_combo.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(project, text="Refresh Projects", command=self.load_projects).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(project, text="Select a project or enter its numeric Project ID.", style="Muted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        project.columnconfigure(0, weight=1)

        source = ttk.LabelFrame(right_column, text="3. FASTQ validation", padding=8, style="Section.TLabelframe")
        source.pack(fill="x", pady=(0, 6))
        ttk.Entry(source, textvariable=self.folder_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(source, text="Browse Folder", command=self.browse_folder).grid(row=0, column=1, padx=(8, 0))
        ttk.Checkbutton(source, text="Recursive", variable=self.recursive_var, command=self.validate_folder).grid(
            row=1, column=0, sticky="w", pady=(7, 0)
        )
        ttk.Label(
            source, text="Invalid filenames are ignored and saved in a validation report.", style="Muted.TLabel"
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))

        stats = ttk.Frame(source)
        stats.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self._stat(stats, 0, "TOTAL FASTQS", self.fastq_count_var)
        self._stat(stats, 1, "VALID", self.valid_fastq_count_var)
        self._stat(stats, 2, "IGNORED", self.invalid_fastq_count_var)
        self._stat(stats, 3, "SAMPLES", self.sample_count_var)
        self._stat(stats, 4, "PAIRS", self.complete_pairs_var)
        self._stat(stats, 5, "MISSING", self.missing_reads_var)
        self._stat(stats, 6, "UPLOAD SIZE", self.total_size_var)

        source.columnconfigure(0, weight=1)

        upload = ttk.LabelFrame(right_column, text="4. Upload", padding=8, style="Section.TLabelframe")
        upload.pack(fill="x", pady=(0, 6))
        self.progress = ttk.Progressbar(upload, variable=self.progress_var, maximum=100)
        self.progress.grid(row=0, column=0, columnspan=4, sticky="ew")
        ttk.Label(upload, textvariable=self.progress_text_var, font=("Segoe UI", 10, "bold")).grid(
            row=0, column=4, sticky="e", padx=(10, 0)
        )
        ttk.Label(upload, textvariable=self.file_progress_var, style="Muted.TLabel").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(5, 0)
        )
        btns = ttk.Frame(upload)
        btns.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(8, 2))
        self.start_btn = ttk.Button(
            btns, text="START UPLOAD", command=self.start_upload, state="disabled", style="Primary.TButton"
        )
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel_upload, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        speed = ttk.Frame(btns)
        speed.pack(side="right")
        ttk.Label(speed, text="Network upload speed", style="CardLabel.TLabel").pack(anchor="e")
        ttk.Label(speed, textvariable=self.live_speed_var, style="CardValue.TLabel").pack(anchor="e")
        for column in range(4):
            upload.columnconfigure(column, weight=1)

        self.logs_frame = ttk.LabelFrame(outer, text="Activity log", padding=8, style="Section.TLabelframe")
        self.logs_frame.pack(fill="x", pady=(0, 2))
        log_toolbar = ttk.Frame(self.logs_frame)
        log_toolbar.pack(fill="x", pady=(0, 6))
        self.log_toggle_btn = ttk.Button(log_toolbar, text="Show log", command=self.toggle_activity_log)
        self.log_toggle_btn.pack(side="left")
        ttk.Button(log_toolbar, text="Open validation report", command=self.open_validation_report).pack(side="left", padx=(6, 0))
        ttk.Button(log_toolbar, text="Save log", command=self.save_activity_log).pack(side="right")
        ttk.Button(log_toolbar, text="Clear", command=self.clear_activity_log).pack(side="right", padx=(0, 6))
        self.log_body = ttk.Frame(self.logs_frame)
        self.log = tk.Text(self.log_body, height=7, wrap="word", font=("Consolas", 9), relief="flat")
        scroll = ttk.Scrollbar(self.log_body, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _stat(self, parent, col, label, variable):
        row = col // 4
        grid_col = col % 4
        frame = ttk.Frame(parent, padding=(8, 6))
        frame.grid(row=row, column=grid_col, sticky="nsew", padx=(0, 4))
        ttk.Label(frame, text=label, style="CardLabel.TLabel").pack(anchor="w")
        ttk.Label(frame, textvariable=variable, style="CardValue.TLabel").pack(anchor="w")
        parent.columnconfigure(grid_col, weight=1)

    def _refresh_status_banner(self, *_):
        if not hasattr(self, "status_banner"):
            return
        status = self.status_var.get()
        lowered = status.lower()
        if "completed" in lowered:
            bg, fg = "#d1fadf", "#05603a"
        elif "uploading" in lowered or "loading" in lowered or "checking" in lowered:
            bg, fg = "#dbeafe", "#1e40af"
        elif "error" in lowered or "failed" in lowered:
            bg, fg = "#fee4e2", "#b42318"
        elif "ready" in lowered and "not ready" not in lowered:
            bg, fg = "#d1fadf", "#05603a"
        elif "cancel" in lowered or "warning" in lowered:
            bg, fg = "#fef0c7", "#93370d"
        else:
            bg, fg = "#e9ecef", "#343a40"
        self.status_banner.configure(text=status.upper(), bg=bg, fg=fg)

    def _update_readiness(self):
        if not hasattr(self, "start_btn") or self.current_process is not None:
            return
        project_ready = False
        try:
            self.selected_project_id()
            project_ready = True
        except ValueError:
            pass
        bs_ready = bool(self.bs_path_var.get().strip())
        validation = self.last_validation or {}
        fastq_ready = (
            validation.get("valid_fastq_count", 0) > 0
            and validation.get("missing_r1", 0) == 0
            and validation.get("missing_r2", 0) == 0
        )
        ready = bs_ready and self.authenticated and project_ready and fastq_ready
        self.start_btn.configure(state="normal" if ready else "disabled", text="START UPLOAD")
        neutral_statuses = {
            "authenticated", "not ready", "ready", "ready to upload",
            "fastq validation passed", "activity log saved",
        }
        current_status = self.status_var.get().lower()
        if ready and (
            current_status in neutral_statuses
            or current_status.startswith("loaded ")
            or current_status.startswith("ready with ")
        ):
            self.status_var.set("Ready to upload")
        elif not ready and not self.authenticated:
            self.status_var.set("Not authenticated — check login to enable upload")
        elif not ready and not project_ready:
            self.status_var.set("Select a destination project")
        elif not ready and not fastq_ready:
            self.status_var.set("Select and validate a complete FASTQ pair")

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

    def save_activity_log(self):
        path = filedialog.asksaveasfilename(
            title="Save activity log",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log.get("1.0", "end-1c"), encoding="utf-8")
            self.status_var.set("Activity log saved")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not save the activity log:\n\n{exc}")

    def open_validation_report(self):
        folder = Path(self.folder_var.get().strip())
        report = folder / "invalid_fastq_filenames.txt"
        if not report.is_file():
            messagebox.showinfo(APP_TITLE, "No invalid-filename report is available for this folder.")
            return
        try:
            if os.name == "nt":
                os.startfile(report)
            else:
                webbrowser.open(report.resolve().as_uri())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not open the validation report:\n\n{exc}")

    def browse_bs(self):
        path = filedialog.askopenfilename(
            title="Select bs.exe",
            filetypes=[("BaseSpace CLI", "bs.exe"), ("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.bs_path_var.set(path)

    def browse_folder(self):
        folder = filedialog.askdirectory(title="Select FASTQ folder")
        if folder:
            self.folder_var.set(folder)
            self.validate_folder()

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

    def append_event_log(self, text):
        self.events.put(("log", text))

    def _append_log(self, text):
        text = ANSI_ESCAPE_RE.sub("", text)
        self.log.insert("end", text)
        self.log.see("end")

    def run_capture(
        self, args, callback=None, purpose="command", open_auth_url=False,
        log_output=True, log_command=True,
    ):
        def worker():
            try:
                exe = self.bs_executable()
                cmd = [exe] + args
                if log_command:
                    self.events.put(("log", "\n> " + subprocess.list2cmdline(cmd) + "\n"))
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags(),
                )
                output = []
                opened = False

                for line in proc.stdout:
                    output.append(line)
                    if log_output:
                        self.events.put(("log", line))
                    if open_auth_url and not opened:
                        m = URL_RE.search(line)
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

    def authenticate(self):
        self.auth_status_var.set("Authenticating…")
        self.status_var.set("Waiting for BaseSpace authentication")
        self.run_capture(["auth"], callback=self._auth_finished, purpose="auth", open_auth_url=True)

    def _auth_finished(self, code, output):
        if code == 0:
            self.auth_status_var.set("Authenticated")
            self.status_var.set("Authenticated")
            self.check_login()
        else:
            self.authenticated = False
            self.auth_status_var.set("Authentication failed")
            self.status_var.set("Authentication failed")
            self._update_readiness()

    def check_login(self):
        self.auth_status_var.set("Checking…")
        self.run_capture(
            ["whoami"], callback=self._whoami_finished, purpose="whoami",
            log_output=False, log_command=False,
        )

    def _whoami_finished(self, code, output):
        if code != 0:
            self._append_log("BaseSpace login check failed.\n")
            self.authenticated = False
            self.auth_status_var.set("Not authenticated")
            self.user_var.set("—")
            self._update_readiness()
            return

        name = ""
        for line in output.splitlines():
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) >= 2:
                key = cols[0].lower()
                val = cols[1]
                if key == "name":
                    name = val

        # Do not expose the account email address in the GUI.
        display = name or "Authenticated BaseSpace user"
        self.user_var.set(display)
        self._append_log(f"BaseSpace login verified for {display}.\n")
        self.authenticated = True
        self.auth_status_var.set("Authenticated")
        self.status_var.set("Authenticated")
        self._update_readiness()

    def load_projects(self):
        self.status_var.set("Loading projects…")
        self.run_capture(
            ["list", "projects"], callback=self._projects_finished, purpose="list projects",
            log_output=False, log_command=False,
        )

    def _projects_finished(self, code, output):
        if code != 0:
            self._append_log("Could not load the BaseSpace project list.\n")
            self.status_var.set("Could not load projects")
            return

        projects = self.parse_projects(output)
        self._append_log(f"Loaded {len(projects)} BaseSpace project(s).\n")
        self.projects = {p.label(): p for p in projects}
        labels = [p.label() for p in projects]
        self.project_combo["values"] = labels

        if labels:
            if self.project_var.get() not in labels:
                self.project_var.set(labels[0])
            self.status_var.set(f"Loaded {len(labels)} projects")
            self._update_readiness()
        else:
            self.status_var.set("No projects parsed; paste Project ID manually")
            messagebox.showwarning(
                APP_TITLE,
                "The project list command succeeded, but the GUI could not parse the table.\n\n"
                "You can still paste the numeric BaseSpace Project ID into the Project box.",
            )

    @staticmethod
    def parse_projects(output):
        found = []
        seen = set()

        for line in output.splitlines():
            if "|" not in line:
                continue

            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            cols = [c for c in cols if c]
            if len(cols) < 2:
                continue

            lowered = [c.lower() for c in cols[:4]]
            if any(value in {"name", "id", "project", "project id", "project name"} for value in lowered):
                continue
            if all(re.fullmatch(r"[-:]+", c) for c in cols):
                continue

            pid = None
            name = None

            if cols[1].isdigit() and cols[0] and not cols[0].isdigit():
                name, pid = cols[0], cols[1]
            elif cols[0].isdigit() and len(cols) > 1:
                pid, name = cols[0], (cols[1] if not cols[1].isdigit() else f"Project {cols[0]}")
            else:
                for i, col in enumerate(cols):
                    if col.isdigit():
                        pid = col
                        name = cols[i - 1] if i > 0 else f"Project {col}"
                        break

            if pid is None or not pid.isdigit():
                continue

            name = (name or f"Project {pid}").strip()
            if name.lower() in {"name", "id", "project", "project id", "project name", ""}:
                continue

            key = (name, pid)
            if key not in seen:
                seen.add(key)
                found.append(Project(name, pid))

        return found

    def selected_project_id(self):
        value = self.project_var.get().strip()
        if value in self.projects:
            return self.projects[value].project_id

        # Allow direct numeric project ID
        if value.isdigit():
            return value

        # Allow "... | 434798366"
        m = re.search(r"\|\s*(\d+)\s*$", value)
        if m:
            return m.group(1)

        raise ValueError("Select a BaseSpace project or paste its numeric Project ID.")

    def fastq_files(self):
        folder = Path(self.folder_var.get().strip())
        if not folder.is_dir():
            return []

        if self.recursive_var.get():
            return sorted(
                p for p in folder.rglob("*")
                if p.is_file() and p.name.lower().endswith(FASTQ_SUFFIXES)
            )
        return sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.name.lower().endswith(FASTQ_SUFFIXES)
        )

    def save_validation_issues(self, folder, invalid_files, pair_issues):
        output_path = folder / "invalid_fastq_filenames.txt"
        try:
            lines = [
                "BaseSpace FASTQ Validation Report",
                "",
                f"Folder: {folder}",
                f"Invalid FASTQ files ignored: {len(invalid_files)}",
                f"Pairing problems: {len(pair_issues)}",
                "",
            ]
            for number, path in enumerate(invalid_files, start=1):
                try:
                    display_name = str(path.relative_to(folder))
                except ValueError:
                    display_name = str(path)
                lines.extend(
                    [
                        f"{number}. {display_name}",
                        "   Reason: Filename does not match the expected Illumina structure",
                        "",
                    ]
                )
            for number, issue in enumerate(pair_issues, start=1):
                lines.extend(
                    [
                        f"Pairing issue {number}: {issue['sample']}",
                        f"   Sample No: S{issue['sample_no']}",
                        f"   Lane: L{issue['lane']}",
                        f"   Set: {issue['set']}",
                        f"   Missing: {issue['missing']}",
                        "",
                    ]
                )
            text = "\n".join(lines)
            output_path.write_text(text, encoding="utf-8")
            if invalid_files or pair_issues:
                self._append_log(f"Saved FASTQ validation issues to: {output_path}\n")
            return output_path
        except Exception as exc:
            self._append_log(f"Could not save FASTQ validation report: {exc}\n")
            return None

    def validate_folder(self):
        folder_text = self.folder_var.get().strip()
        if not folder_text:
            return

        folder = Path(folder_text)
        if not folder.is_dir():
            self.last_validation = None
            self.fastq_count_var.set("0")
            self.valid_fastq_count_var.set("0")
            self.invalid_fastq_count_var.set("0")
            self.sample_count_var.set("0")
            self.complete_pairs_var.set("0")
            self.missing_reads_var.set("0")
            self.total_size_var.set("0 B")
            self.pair_status_var.set("Folder not found")
            self.status_var.set("FASTQ folder not found")
            self._update_readiness()
            return

        files = self.fastq_files()
        total_size = sum(p.stat().st_size for p in files)

        pairs = {}
        samples = set()
        sample_files = {}
        invalid = []

        for p in files:
            m = FASTQ_RE.match(p.name)
            if not m:
                invalid.append(p)
                continue

            g = m.groupdict()
            samples.add(g["sample"])
            sample_files.setdefault(g["sample"], []).append(p)
            key = (str(p.parent), g["sample"], g["sample_no"], g["lane"], g["set"])
            pairs.setdefault(key, set()).add(g["read"])

        missing_r1 = sum(1 for reads in pairs.values() if "1" not in reads)
        missing_r2 = sum(1 for reads in pairs.values() if "2" not in reads)
        complete_pairs = sum(1 for reads in pairs.values() if reads == {"1", "2"})
        pair_issues = []
        for key, reads in pairs.items():
            _, sample, sample_no, lane, set_no = key
            missing = []
            if "1" not in reads:
                missing.append("R1")
            if "2" not in reads:
                missing.append("R2")
            if missing:
                pair_issues.append(
                    {
                        "sample": sample,
                        "sample_no": sample_no,
                        "lane": lane,
                        "set": set_no,
                        "missing": " and ".join(missing),
                    }
                )
        valid_total_size = sum(p.stat().st_size for p in files if p not in invalid)

        self.fastq_count_var.set(str(len(files)))
        self.valid_fastq_count_var.set(str(len(files) - len(invalid)))
        self.invalid_fastq_count_var.set(str(len(invalid)))
        self.sample_count_var.set(str(len(samples)))
        self.complete_pairs_var.set(str(complete_pairs))
        self.missing_reads_var.set(str(missing_r1 + missing_r2))
        self.total_size_var.set(human_size(valid_total_size))

        if not files:
            status = "No FASTQ files"
        elif missing_r1 or missing_r2:
            status = f"Warning: missing R1={missing_r1}, R2={missing_r2}"
        elif invalid:
            status = f"{complete_pairs} pairs; {len(invalid)} non-standard name(s)"
        else:
            status = f"OK: {complete_pairs} complete pair(s)"

        self.pair_status_var.set(status)

        self._append_log("\nFASTQ validation\n")
        self._append_log(f"Folder: {folder}\n")
        self._append_log(f"FASTQ files: {len(files)}\n")
        self._append_log(f"Samples detected: {len(samples)}\n")
        self._append_log(f"Total size: {human_size(total_size)}\n")
        self._append_log(f"Complete R1/R2 pairs: {complete_pairs}\n")
        self._append_log(f"Missing R1: {missing_r1}; Missing R2: {missing_r2}\n")
        if invalid:
            self._append_log(f"Non-standard FASTQ filenames: {len(invalid)}\n")
            self._append_log("These files will be ignored during upload:\n")
            for path in invalid[:20]:
                self._append_log(f"  - {path.relative_to(folder)}\n")
            if len(invalid) > 20:
                self._append_log(f"  ... and {len(invalid) - 20} more\n")
        self.save_validation_issues(folder, invalid, pair_issues)

        result = {
            "valid_fastq_count": len(files) - len(invalid),
            "invalid_fastq_count": len(invalid),
            "invalid_files": invalid,
            "sample_count": len(samples),
            "complete_pairs": complete_pairs,
            "missing_r1": missing_r1,
            "missing_r2": missing_r2,
            "total_size": total_size,
            "valid_total_size": valid_total_size,
            "sample_file_sizes": {
                sample: [p.stat().st_size for p in sorted(paths)]
                for sample, paths in sample_files.items()
            },
        }
        self.last_validation = result
        if not files:
            self.status_var.set("No FASTQ files detected")
        elif result["valid_fastq_count"] == 0:
            self.status_var.set("No valid FASTQ files")
        elif missing_r1 or missing_r2:
            self.status_var.set("FASTQ pairing error")
        elif invalid:
            self.status_var.set(f"Ready with {len(invalid)} ignored file(s)")
        else:
            self.status_var.set("FASTQ validation passed")
        self._update_readiness()
        return result

    def start_upload(self):
        if self.current_process is not None:
            messagebox.showinfo(APP_TITLE, "An upload is already running.")
            return

        try:
            exe = self.bs_executable()
            project_id = self.selected_project_id()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        folder = Path(self.folder_var.get().strip())
        if not folder.is_dir():
            messagebox.showerror(APP_TITLE, "Select a valid FASTQ folder.")
            return

        files = self.fastq_files()
        if not files:
            messagebox.showerror(APP_TITLE, "No .fastq.gz/.fq.gz files were found.")
            return

        validation = self.validate_folder()
        if not validation or validation["valid_fastq_count"] == 0:
            messagebox.showerror(
                APP_TITLE,
                "No valid FASTQ filenames were found.\n\n"
                "Invalid names were saved to invalid_fastq_filenames.txt.",
            )
            return

        ignored_notice = ""
        if validation["invalid_fastq_count"]:
            ignored_notice = (
                f"\nInvalid files ignored: {validation['invalid_fastq_count']}\n"
                "Their names were saved to invalid_fastq_filenames.txt.\n"
            )

        answer = messagebox.askyesno(
            APP_TITLE,
            f"Start BaseSpace upload?\n\n"
            f"Project ID: {project_id}\n"
            f"Folder: {folder}\n"
            f"Valid FASTQ files: {validation['valid_fastq_count']}\n"
            f"{ignored_notice}"
            f"Valid FASTQ size: {human_size(validation['valid_total_size'])}",
        )
        if not answer:
            return

        args = ["upload", "dataset", "-p", project_id]
        # This is intentionally unconditional: invalid names are reported by
        # validation and ignored by the official BaseSpace CLI.
        args.append("--skip-invalid-filenames")
        if self.recursive_var.get():
            args.append("--recursive")
        args.append(str(folder))

        self.progress_var.set(0)
        self.progress_text_var.set("0%")
        self.file_progress_var.set(f"0 of {validation['valid_fastq_count']} files")
        self.upload_total_files = validation["valid_fastq_count"]
        self.upload_total_bytes = validation["valid_total_size"]
        self.upload_completed_files = 0
        self.upload_completed_bytes = 0
        self.upload_current_sample = None
        self.upload_current_group_done = 0
        self.upload_sample_file_sizes = validation["sample_file_sizes"]
        self.upload_completed_samples = set()
        self.live_speed_var.set("0.00 MiB/s")
        self.start_btn.config(state="disabled", text="UPLOADING…")
        self.cancel_btn.config(state="normal")
        self.status_var.set("Uploading")
        self.upload_started_at = time.monotonic()

        if psutil is not None:
            now = time.monotonic()
            sent = psutil.net_io_counters().bytes_sent
            self.net_last_bytes = sent
            self.net_last_time = now
            self._schedule_network_update()

        def worker():
            try:
                cmd = [exe] + args
                self.events.put(("log", "\nUPLOAD COMMAND\n> " + subprocess.list2cmdline(cmd) + "\n\n"))

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    creationflags=creationflags(),
                )
                self.current_process = proc

                # Read raw bytes so both newline and carriage-return progress updates are captured.
                buffer = bytearray()
                while True:
                    chunk = proc.stdout.read(1)
                    if not chunk:
                        if buffer:
                            self.events.put(("upload_line", buffer.decode("utf-8", errors="replace")))
                        break

                    if chunk in (b"\n", b"\r"):
                        if buffer:
                            line = buffer.decode("utf-8", errors="replace")
                            buffer.clear()
                            self.events.put(("upload_line", line))
                    else:
                        buffer.extend(chunk)

                code = proc.wait()
                self.events.put(("upload_done", code))
            except Exception as exc:
                self.events.put(("error", f"Upload failed to start: {exc}"))
                self.events.put(("upload_done", -1))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_upload_line(self, line):
        if not line.strip():
            return

        self._append_log(line + "\n")

        sample_match = CREATE_SAMPLE_RE.search(line)
        if sample_match:
            self._complete_current_sample()
            self.upload_current_sample = sample_match.group("sample").strip()
            self.upload_current_group_done = 0

        fm = FILE_PROGRESS_RE.search(line)
        if fm:
            done = int(fm.group("done"))
            self.upload_current_group_done = done
            sizes = self._current_sample_sizes()
            if sizes:
                current_bytes = sum(sizes[:min(done, len(sizes))])
                self._set_overall_upload_progress(
                    self.upload_completed_bytes + current_bytes,
                    self.upload_completed_files + min(done, len(sizes)),
                )
                if done >= len(sizes):
                    self._complete_current_sample()
            else:
                # Fallback when the CLI sample name cannot be matched to the
                # validated filenames. Keep the overall denominator correct.
                completed = min(self.upload_total_files, self.upload_completed_files + done)
                self._set_overall_upload_progress(None, completed)

        # BaseSpace also emits byte progress for the current file. Weight that
        # value by the validated sizes of every FASTQ in the upload.
        sm = SIZE_PROGRESS_RE.search(line)
        if sm:
            done = bytes_from_unit(sm.group("done"), sm.group("done_unit"))
            total = bytes_from_unit(sm.group("total"), sm.group("total_unit"))
            sizes = self._current_sample_sizes()
            index = self.upload_current_group_done
            if total > 0 and sizes and index < len(sizes):
                prior_bytes = sum(sizes[:index])
                current_bytes = sizes[index] * min(1.0, max(0.0, done / total))
                self._set_overall_upload_progress(
                    self.upload_completed_bytes + prior_bytes + current_bytes,
                    self.upload_completed_files + index,
                )

    def _current_sample_sizes(self):
        if not self.upload_current_sample:
            return []
        return self.upload_sample_file_sizes.get(self.upload_current_sample, [])

    def _complete_current_sample(self):
        sample = self.upload_current_sample
        if not sample or sample in self.upload_completed_samples:
            return
        sizes = self.upload_sample_file_sizes.get(sample, [])
        if sizes:
            self.upload_completed_samples.add(sample)
            self.upload_completed_bytes += sum(sizes)
            self.upload_completed_files += len(sizes)
            self._set_overall_upload_progress(
                self.upload_completed_bytes,
                self.upload_completed_files,
            )
        self.upload_current_sample = None
        self.upload_current_group_done = 0

    def _set_overall_upload_progress(self, bytes_done, files_done):
        files_done = max(0, min(self.upload_total_files, files_done))
        if bytes_done is not None and self.upload_total_bytes > 0:
            pct = 100.0 * bytes_done / self.upload_total_bytes
        elif self.upload_total_files > 0:
            pct = 100.0 * files_done / self.upload_total_files
        else:
            pct = 0.0
        # Reserve 100% for a successful process exit.
        pct = max(float(self.progress_var.get()), max(0.0, min(99.9, pct)))
        self.progress_var.set(pct)
        self.progress_text_var.set(f"{pct:.1f}%")
        self.file_progress_var.set(
            f"{files_done} of {self.upload_total_files} files uploaded | "
            f"{human_size(bytes_done or 0)} of {human_size(self.upload_total_bytes)}"
        )

    def _schedule_network_update(self):
        if self.current_process is None and self.status_var.get() != "Uploading":
            return
        self._update_network_speed()
        self.network_job = self.after(1000, self._schedule_network_update)

    def _update_network_speed(self):
        if psutil is None or self.upload_started_at is None:
            return

        try:
            now = time.monotonic()
            sent = psutil.net_io_counters().bytes_sent

            if self.net_last_time is not None:
                dt = now - self.net_last_time
                delta = max(0, sent - self.net_last_bytes)
                if dt > 0:
                    mib_s = delta / (1024**2) / dt
                    self.live_speed_var.set(f"{mib_s:.2f} MiB/s")

            self.net_last_time = now
            self.net_last_bytes = sent
        except Exception:
            pass

    def cancel_upload(self):
        proc = self.current_process
        if proc is None:
            return

        if not messagebox.askyesno(APP_TITLE, "Cancel the active BaseSpace upload?"):
            return

        self.status_var.set("Cancelling…")
        try:
            proc.terminate()
            self._append_log("\nCancellation requested.\n")
        except Exception as exc:
            self._append_log(f"\nCould not terminate process: {exc}\n")

    def _upload_finished(self, code):
        self.current_process = None
        if self.network_job is not None:
            try:
                self.after_cancel(self.network_job)
            except Exception:
                pass
            self.network_job = None

        self._update_network_speed()
        self.cancel_btn.config(state="disabled")

        if code == 0:
            self.progress_var.set(100)
            self.progress_text_var.set("100%")
            self.file_progress_var.set(
                f"{self.upload_total_files} of {self.upload_total_files} files uploaded | "
                f"{human_size(self.upload_total_bytes)} of {human_size(self.upload_total_bytes)}"
            )
            self.status_var.set("Upload completed")
            self._append_log("\n=== Upload completed successfully ===\n")
            messagebox.showinfo(APP_TITLE, "BaseSpace upload completed successfully.")
        else:
            self.file_progress_var.set("Upload did not complete")
            self.status_var.set(f"Upload stopped / failed (exit code {code})")
            self._append_log(f"\n=== Upload ended with exit code {code} ===\n")
            messagebox.showerror(
                APP_TITLE,
                f"BaseSpace upload did not complete successfully.\n\nExit code: {code}\n"
                "Check the upload log for details.",
            )
        self._update_readiness()

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

                elif kind == "upload_line":
                    self._handle_upload_line(event[1])

                elif kind == "upload_done":
                    self._upload_finished(event[1])

        except queue.Empty:
            pass

        self.after(100, self._process_events)


if __name__ == "__main__":
    root = tk.Tk()
    app = BaseSpaceUploader(root)
    app.pack(fill="both", expand=True)
    root.mainloop()
