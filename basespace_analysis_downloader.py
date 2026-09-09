import csv
import io
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "BaseSpace Analysis Folder Downloader"
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def creationflags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def safe_relative_path(remote_path):
    """Return a safe local relative path for a BaseSpace POSIX path."""
    parts = [p for p in PurePosixPath(remote_path.replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
    safe_parts = []
    for part in parts:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part).rstrip(". ") or "unnamed"
        safe_parts.append(cleaned)
    return Path(*safe_parts) if safe_parts else Path("unnamed_file")


@dataclass
class Dataset:
    dataset_id: str
    name: str

    @property
    def label(self):
        return f"{self.name}  |  {self.dataset_id}"


@dataclass
class Project:
    project_id: str
    name: str

    @property
    def label(self):
        return f"{self.name}  |  {self.project_id}"


@dataclass
class Analysis:
    analysis_id: str
    name: str
    status: str = ""

    @property
    def label(self):
        suffix = f"  [{self.status}]" if self.status else ""
        return f"{self.name}  |  {self.analysis_id}{suffix}"


@dataclass
class RemoteFile:
    file_id: str
    path: str
    dataset_id: str = ""
    dataset_name: str = ""


class AnalysisFolderDownloader(ttk.Frame):
    def __init__(self, master=None, show_header=True, configure_window=True):
        super().__init__(master)
        self.show_header = show_header
        if configure_window:
            window = self.winfo_toplevel()
            window.title(APP_TITLE)
            window.geometry("1120x760")
            window.minsize(920, 650)

        self.events = queue.Queue()
        self.current_process = None
        self.cancel_requested = threading.Event()
        self.projects = {}
        self.analyses = {}
        self.datasets = {}
        self.files = {}

        self.bs_path_var = tk.StringVar(value=self.default_bs_path())
        self.project_var = tk.StringVar(value="517776259")
        self.analysis_var = tk.StringVar()
        self.dataset_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Select a project, then load its analyses")
        self.selection_var = tk.StringVar(value="0 files selected")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="0 / 0")

        self._build_ui()
        self.after(100, self._process_events)

    def default_bs_path(self):
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        candidate = base / "bs.exe"
        if candidate.exists():
            return str(candidate)
        return shutil.which("bs.exe") or shutil.which("bs") or str(candidate)

    def bs_executable(self):
        raw = self.bs_path_var.get().strip().strip('"')
        if Path(raw).is_file():
            return raw
        found = shutil.which(raw)
        if found:
            return found
        raise FileNotFoundError(f"Cannot find BaseSpace CLI: {raw}")

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Muted.TLabel", foreground="#5f6b7a")
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(15, 7))

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        if self.show_header:
            ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
            ttk.Label(
                outer,
                text="Choose project  →  choose analysis  →  choose dataset  →  select folders/files  →  download",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(2, 10))

        self.banner = tk.Label(
            outer, textvariable=self.status_var, anchor="w", padx=14, pady=9,
            font=("Segoe UI", 10, "bold"), bg="#e8f0fe", fg="#174ea6",
        )
        self.banner.pack(fill="x", pady=(0, 10))

        setup = ttk.LabelFrame(outer, text="1. Project, analysis and destination", padding=10, style="Section.TLabelframe")
        setup.pack(fill="x", pady=(0, 10))
        ttk.Label(setup, text="bs.exe").grid(row=0, column=0, sticky="w")
        ttk.Entry(setup, textvariable=self.bs_path_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(setup, text="Browse", command=self.browse_bs).grid(row=0, column=2)
        ttk.Label(setup, text="Project").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.project_combo = ttk.Combobox(setup, textvariable=self.project_var, state="normal")
        self.project_combo.grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        self.load_projects_btn = ttk.Button(setup, text="Load Projects", command=self.load_projects)
        self.load_projects_btn.grid(row=1, column=2, pady=(8, 0))
        ttk.Label(setup, text="Analysis").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.analysis_combo = ttk.Combobox(setup, textvariable=self.analysis_var, state="normal")
        self.analysis_combo.grid(row=2, column=1, sticky="ew", padx=8, pady=(8, 0))
        self.load_analyses_btn = ttk.Button(
            setup, text="Load Project Analyses", command=self.load_analyses, style="Primary.TButton"
        )
        self.load_analyses_btn.grid(row=2, column=2, pady=(8, 0))
        ttk.Label(setup, text="Output folder").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(setup, textvariable=self.output_var).grid(row=3, column=1, sticky="ew", padx=8, pady=(8, 0))
        ttk.Button(setup, text="Browse", command=self.browse_output).grid(row=3, column=2, pady=(8, 0))
        self.load_outputs_btn = ttk.Button(
            setup, text="Load Selected Analysis Outputs", command=self.load_outputs, state="disabled"
        )
        self.load_outputs_btn.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
        setup.columnconfigure(1, weight=1)

        dataset_box = ttk.LabelFrame(outer, text="2. Analysis output datasets", padding=10, style="Section.TLabelframe")
        dataset_box.pack(fill="x", pady=(0, 10))
        self.dataset_combo = ttk.Combobox(dataset_box, textvariable=self.dataset_var, state="readonly")
        self.dataset_combo.grid(row=0, column=0, sticky="ew")
        self.dataset_combo.bind("<<ComboboxSelected>>", lambda _event: self.load_dataset_files())
        self.load_files_btn = ttk.Button(dataset_box, text="Load Folder Tree", command=self.load_dataset_files, state="disabled")
        self.load_files_btn.grid(row=0, column=1, padx=(8, 0))
        dataset_box.columnconfigure(0, weight=1)

        tree_box = ttk.LabelFrame(
            outer, text="3. Select and download folders or files (Ctrl/Shift-click for multiple)",
            padding=10, style="Section.TLabelframe",
        )
        tree_box.pack(fill="both", expand=True, pady=(0, 10))
        tools = ttk.Frame(tree_box)
        tools.pack(fill="x", pady=(0, 7))
        ttk.Button(tools, text="Select All", command=self.select_all).pack(side="left")
        ttk.Button(tools, text="Select None", command=lambda: self.tree.selection_remove(self.tree.selection())).pack(side="left", padx=6)
        self.download_btn = ttk.Button(
            tools, text="START DOWNLOAD", command=self.start_download,
            style="Primary.TButton", state="disabled",
        )
        self.download_btn.pack(side="left", padx=(12, 6))
        self.cancel_btn = ttk.Button(tools, text="Cancel", command=self.cancel_download, state="disabled")
        self.cancel_btn.pack(side="left")
        ttk.Label(tools, textvariable=self.selection_var, style="Muted.TLabel").pack(side="right")

        progress_row = ttk.Frame(tree_box)
        progress_row.pack(fill="x", pady=(0, 7))
        ttk.Progressbar(progress_row, variable=self.progress_var, maximum=100).pack(side="left", fill="x", expand=True)
        ttk.Label(progress_row, textvariable=self.progress_text_var, width=10, anchor="e").pack(side="right", padx=(8, 0))

        tree_wrap = ttk.Frame(tree_box)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, columns=("kind", "id"), show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Folder / file path")
        self.tree.heading("kind", text="Type")
        self.tree.heading("id", text="File ID")
        self.tree.column("#0", width=650, anchor="w")
        self.tree.column("kind", width=90, anchor="center", stretch=False)
        self.tree.column("id", width=150, anchor="center", stretch=False)
        yscroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.update_selection())

    def browse_bs(self):
        path = filedialog.askopenfilename(title="Select BaseSpace CLI", filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if path:
            self.bs_path_var.set(path)

    def browse_output(self):
        path = filedialog.askdirectory(title="Select download folder")
        if path:
            self.output_var.set(path)

    def run_async(self, command, event_name):
        def worker():
            try:
                proc = subprocess.run(
                    command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    creationflags=creationflags(),
                )
                output = ANSI_ESCAPE_RE.sub("", (proc.stdout or "") + (proc.stderr or ""))
                self.events.put((event_name, proc.returncode, output))
            except Exception as exc:
                self.events.put((event_name, -1, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def numeric_id(raw, entity):
        raw = raw.strip().rstrip("/, ")
        match = re.search(rf"/{entity}/(\d+)(?:/|$)", raw)
        if not match:
            match = re.fullmatch(r"(\d+)", raw)
        if not match:
            match = re.search(r"\|\s*(\d+)\s*$", raw)
        return match.group(1) if match else ""

    def selected_project_id(self):
        project = self.projects.get(self.project_var.get())
        return project.project_id if project else self.numeric_id(self.project_var.get(), "projects")

    def selected_analysis_id(self):
        analysis = self.analyses.get(self.analysis_var.get())
        return analysis.analysis_id if analysis else self.numeric_id(self.analysis_var.get(), "analyses")

    def load_projects(self):
        try:
            exe = self.bs_executable()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.status_var.set("Loading accessible BaseSpace projects…")
        self.load_projects_btn.configure(state="disabled")
        self.run_async(
            [exe, "list", "projects", "-F", "Id", "-F", "Name", "-f", "csv"],
            "projects_done",
        )

    def _projects_done(self, code, output):
        self.load_projects_btn.configure(state="normal")
        if code != 0:
            self.status_var.set("Could not load projects")
            messagebox.showerror(APP_TITLE, output.strip() or "BaseSpace returned an error.")
            return
        projects = []
        for row in csv.DictReader(io.StringIO(output)):
            project_id = (row.get("Id") or "").strip()
            if project_id:
                projects.append(Project(project_id, (row.get("Name") or project_id).strip()))
        previous_id = self.selected_project_id()
        self.projects = {project.label: project for project in projects}
        labels = list(self.projects)
        self.project_combo["values"] = labels
        matching = next((label for label, project in self.projects.items() if project.project_id == previous_id), None)
        if matching:
            self.project_var.set(matching)
        elif labels:
            self.project_var.set(labels[0])
        self.status_var.set(f"Loaded {len(projects)} project(s); select one and load its analyses")

    def load_analyses(self):
        project_id = self.selected_project_id()
        if not project_id:
            messagebox.showerror(APP_TITLE, "Select a project or enter its numeric project ID/URL first.")
            return
        try:
            exe = self.bs_executable()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.status_var.set(f"Loading analyses from project {project_id}…")
        self.load_analyses_btn.configure(state="disabled")
        self.load_outputs_btn.configure(state="disabled")
        self.run_async(
            [
                exe, "list", "appsessions", "--project-id", project_id,
                "-F", "Id", "-F", "Name", "-F", "ExecutionStatus", "-f", "csv",
            ],
            "analyses_done",
        )

    def _analyses_done(self, code, output):
        self.load_analyses_btn.configure(state="normal")
        if code != 0:
            self.status_var.set("Could not load project analyses")
            messagebox.showerror(APP_TITLE, output.strip() or "BaseSpace returned an error.")
            return
        analyses = []
        for row in csv.DictReader(io.StringIO(output)):
            analysis_id = (row.get("Id") or "").strip()
            if analysis_id:
                analyses.append(Analysis(
                    analysis_id,
                    (row.get("Name") or analysis_id).strip(),
                    (row.get("ExecutionStatus") or "").strip(),
                ))
        self.analyses = {analysis.label: analysis for analysis in analyses}
        labels = list(self.analyses)
        self.analysis_combo["values"] = labels
        self.analysis_var.set(labels[0] if labels else "")
        self.load_outputs_btn.configure(state="normal" if labels else "disabled")
        if labels:
            self.status_var.set(f"Found {len(labels)} analysis/analyses in project {self.selected_project_id()}")
        else:
            self.status_var.set("No analyses were found in the selected project")

    def load_outputs(self):
        project_id = self.selected_project_id()
        analysis_id = self.selected_analysis_id()
        if not project_id:
            messagebox.showerror(APP_TITLE, "Select a project first.")
            return
        if not analysis_id:
            messagebox.showerror(APP_TITLE, "Select an analysis from the chosen project.")
            return
        try:
            exe = self.bs_executable()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.status_var.set("Loading analysis output datasets…")
        self.load_outputs_btn.configure(state="disabled")
        self.run_async([exe, "await", "appsession", analysis_id, "--terse"], "outputs_done")

    def _outputs_done(self, code, output):
        self.load_outputs_btn.configure(state="normal")
        if code != 0:
            self.status_var.set("Could not load analysis outputs")
            messagebox.showerror(
                APP_TITLE,
                (output.strip() or "BaseSpace returned an error.")
                + "\n\nThe analysis may have been deleted, its underlying data may have been removed, "
                  "or it may not be accessible in the current account/workgroup.",
            )
            return
        ids = list(dict.fromkeys(re.findall(r"\bds\.[A-Za-z0-9]+", output)))
        if not ids:
            self.status_var.set("No output datasets found")
            messagebox.showinfo(APP_TITLE, "This analysis did not return output dataset IDs.")
            return
        self.status_var.set(f"Loading names for {len(ids)} output dataset(s)…")

        def worker():
            datasets = []
            try:
                exe = self.bs_executable()
                for dataset_id in ids:
                    proc = subprocess.run(
                        [exe, "get", "dataset", "-i", dataset_id, "-F", "Id", "-F", "Name", "-f", "csv"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=creationflags(),
                    )
                    name = dataset_id
                    if proc.returncode == 0:
                        rows = list(csv.DictReader(io.StringIO(ANSI_ESCAPE_RE.sub("", proc.stdout))))
                        if rows:
                            name = rows[0].get("Name") or dataset_id
                    datasets.append(Dataset(dataset_id, name))
                self.events.put(("names_done", datasets, None))
            except Exception as exc:
                self.events.put(("names_done", None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _names_done(self, datasets, error):
        if error:
            self.status_var.set("Could not load dataset names")
            messagebox.showerror(APP_TITLE, error)
            return
        self.datasets = {d.label: d for d in datasets}
        labels = ["All output datasets"] + list(self.datasets)
        self.dataset_combo["values"] = labels
        self.dataset_var.set("All output datasets")
        self.load_files_btn.configure(state="normal")
        self.status_var.set(f"Found {len(datasets)} output dataset(s); loading their folder tree…")
        self.load_dataset_files()

    def load_dataset_files(self):
        selection = self.dataset_var.get()
        if selection == "All output datasets":
            datasets = list(self.datasets.values())
        else:
            dataset = self.datasets.get(selection)
            datasets = [dataset] if dataset else []
        if not datasets:
            return
        try:
            exe = self.bs_executable()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        description = "all output datasets" if len(datasets) > 1 else datasets[0].name
        self.status_var.set(f"Loading folder tree from {description}…")
        self.load_files_btn.configure(state="disabled")

        def worker():
            files = []
            errors = []
            for dataset in datasets:
                proc = subprocess.run(
                    [exe, "contents", "dataset", "-i", dataset.dataset_id, "-F", "Id", "-F", "Path", "-f", "csv"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=creationflags(),
                )
                output = ANSI_ESCAPE_RE.sub("", (proc.stdout or "") + (proc.stderr or ""))
                if proc.returncode != 0:
                    errors.append(f"{dataset.name}: {output.strip()}")
                    continue
                for row in csv.DictReader(io.StringIO(output)):
                    file_id = (row.get("Id") or "").strip()
                    path = (row.get("Path") or row.get("FilePath") or "").strip()
                    if file_id and path:
                        files.append(RemoteFile(file_id, path, dataset.dataset_id, dataset.name))
            self.events.put(("files_done", 0 if files or not errors else 1, files, errors))

        threading.Thread(target=worker, daemon=True).start()

    def _files_done(self, code, files, errors=None):
        self.load_files_btn.configure(state="normal")
        if code != 0:
            self.status_var.set("Could not load dataset files")
            messagebox.showerror(APP_TITLE, "\n\n".join(errors or []) or "BaseSpace returned an error.")
            return
        self._populate_tree(files)
        dataset_count = len({f.dataset_id for f in files})
        self.status_var.set(f"Loaded {len(files)} file(s) from {dataset_count} output dataset folder(s)")
        if errors:
            messagebox.showwarning(APP_TITLE, "Some dataset folders could not be loaded:\n\n" + "\n".join(errors))

    def _populate_tree(self, files):
        self.tree.delete(*self.tree.get_children())
        self.files.clear()
        folders = {}
        dataset_nodes = {}
        for remote in sorted(files, key=lambda f: (f.dataset_name.lower(), f.path.lower())):
            dataset_key = remote.dataset_id or remote.dataset_name
            if dataset_key not in dataset_nodes:
                label = remote.dataset_name or remote.dataset_id or "Dataset"
                dataset_nodes[dataset_key] = self.tree.insert(
                    "", "end", text=label, values=("Dataset folder", remote.dataset_id), open=False,
                )
                folders[(dataset_key, "")] = dataset_nodes[dataset_key]
            parts = list(PurePosixPath(remote.path.replace("\\", "/")).parts)
            parent_key = ""
            for index, part in enumerate(parts[:-1]):
                folder_key = "/".join(parts[:index + 1])
                combined_key = (dataset_key, folder_key)
                if combined_key not in folders:
                    parent_iid = folders[(dataset_key, parent_key)]
                    iid = self.tree.insert(parent_iid, "end", text=part, values=("Folder", ""), open=True)
                    folders[combined_key] = iid
                parent_key = folder_key
            iid = self.tree.insert(folders[(dataset_key, parent_key)], "end", text=parts[-1], values=("File", remote.file_id))
            self.files[iid] = remote
        self.update_selection()

    def selected_files(self):
        selected = set()

        def collect(iid):
            if iid in self.files:
                selected.add(iid)
            for child in self.tree.get_children(iid):
                collect(child)

        for iid in self.tree.selection():
            collect(iid)
        return [self.files[iid] for iid in selected]

    def update_selection(self):
        count = len(self.selected_files())
        self.selection_var.set(f"{count} file{'s' if count != 1 else ''} selected")
        self.download_btn.configure(state="normal" if count else "disabled")

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())
        self.update_selection()

    def start_download(self):
        files = self.selected_files()
        if not files:
            return
        output = self.output_var.get().strip()
        if not output:
            output = str(Path(__file__).resolve().parent / f"BaseSpace_Analysis_{self.selected_analysis_id()}")
            self.output_var.set(output)
        root = Path(output).resolve()
        root.mkdir(parents=True, exist_ok=True)
        try:
            exe = self.bs_executable()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.cancel_requested.clear()
        self.download_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress_var.set(0)
        self.progress_text_var.set(f"0 / {len(files)}")
        self.status_var.set("Downloading selected files…")

        def worker():
            failures = []
            for index, remote in enumerate(files, 1):
                if self.cancel_requested.is_set():
                    break
                relative = safe_relative_path(remote.path)
                dataset_folder = safe_relative_path(remote.dataset_name or remote.dataset_id or "Dataset")
                destination = root / dataset_folder / relative.parent
                destination.mkdir(parents=True, exist_ok=True)
                command = [exe, "download", "file", "--no-metadata", "-i", remote.file_id, "-o", str(destination)]
                try:
                    self.current_process = subprocess.Popen(
                        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", creationflags=creationflags(),
                    )
                    output_text, _ = self.current_process.communicate()
                    if self.current_process.returncode != 0:
                        failures.append((remote.path, ANSI_ESCAPE_RE.sub("", output_text or "").strip()))
                except Exception as exc:
                    failures.append((remote.path, str(exc)))
                finally:
                    self.current_process = None
                self.events.put(("progress", index, len(files), remote.path))
            self.events.put(("download_done", failures, self.cancel_requested.is_set(), str(root)))
        threading.Thread(target=worker, daemon=True).start()

    def cancel_download(self):
        self.cancel_requested.set()
        if self.current_process and self.current_process.poll() is None:
            self.current_process.terminate()
        self.status_var.set("Cancelling…")

    def _download_done(self, failures, cancelled, output):
        self.cancel_btn.configure(state="disabled")
        self.update_selection()
        if cancelled:
            self.status_var.set("Download cancelled")
            return
        if failures:
            report = Path(output) / "failed_downloads.tsv"
            report.write_text("Path\tError\n" + "\n".join(f"{p}\t{e}" for p, e in failures), encoding="utf-8")
            self.status_var.set(f"Completed with {len(failures)} failed file(s)")
            messagebox.showwarning(APP_TITLE, f"Some files failed. See:\n{report}")
        else:
            self.progress_var.set(100)
            self.status_var.set(f"Download completed: {output}")
            messagebox.showinfo(APP_TITLE, f"Selected folders/files downloaded to:\n{output}")

    def _process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                name = event[0]
                if name == "outputs_done":
                    self._outputs_done(event[1], event[2])
                elif name == "projects_done":
                    self._projects_done(event[1], event[2])
                elif name == "analyses_done":
                    self._analyses_done(event[1], event[2])
                elif name == "names_done":
                    self._names_done(event[1], event[2])
                elif name == "files_done":
                    self._files_done(event[1], event[2], event[3])
                elif name == "progress":
                    _, index, total, path = event
                    self.progress_var.set(index * 100 / total)
                    self.progress_text_var.set(f"{index} / {total}")
                    self.status_var.set(f"Downloaded: {path}")
                elif name == "download_done":
                    self._download_done(event[1], event[2], event[3])
        except queue.Empty:
            pass
        self.after(100, self._process_events)


def main():
    root = tk.Tk()
    app = AnalysisFolderDownloader(root)
    app.pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
