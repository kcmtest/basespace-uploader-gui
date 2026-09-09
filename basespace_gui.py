"""Unified graphical interface for BaseSpace data transfers."""

import tkinter as tk
from tkinter import ttk

from basespace_analysis_downloader import AnalysisFolderDownloader
from basespace_downloader import BaseSpaceDownloader
from basespace_uploader import BaseSpaceUploader


APP_TITLE = "BaseSpace Transfer Manager"
NAVY = "#123b5d"
NAVY_LIGHT = "#1d5278"
SURFACE = "#f4f6f8"
MUTED = "#607080"


class BaseSpaceTransferManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1360x840")
        self.minsize(1080, 700)
        self.configure(bg=SURFACE)

        self._syncing = False
        self.pages = {}
        self.nav_buttons = {}
        self.page_details = {
            "upload": ("Upload FASTQs", "Validate paired-end FASTQs and upload them to a BaseSpace project"),
            "files": ("Download by File Type", "Find project files by extension or name and download selected results"),
            "analysis": ("Download Analysis Folders", "Browse an analysis by dataset folder and download exact selections"),
        }

        self._configure_styles()
        self._build_shell()
        self._build_pages()
        self._bind_shared_state()
        self.show_page("upload")

    def _configure_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Workspace.TFrame", background=SURFACE)

    def _build_shell(self):
        header = tk.Frame(self, bg=NAVY, padx=22, pady=12)
        header.pack(fill="x")
        brand = tk.Frame(header, bg=NAVY)
        brand.pack(side="left")
        tk.Label(
            brand, text=APP_TITLE, bg=NAVY, fg="white",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            brand, text="Sequencing data transfers",
            bg=NAVY, fg="#c9ddec", font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(1, 0))
        tk.Label(
            header, text="BaseSpace CLI", bg="#0d704b", fg="white",
            font=("Segoe UI", 9, "bold"), padx=10, pady=5,
        ).pack(side="right", anchor="e")

        body = tk.Frame(self, bg=SURFACE)
        body.pack(fill="both", expand=True)
        self.sidebar = tk.Frame(body, bg=NAVY, width=225)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        tk.Label(
            self.sidebar, text="WORKFLOWS", bg=NAVY, fg="#9fb9cc",
            font=("Segoe UI", 8, "bold"), anchor="w", padx=18, pady=14,
        ).pack(fill="x")

        nav_items = (
            ("upload", "↑  Upload FASTQs", "Validate and transfer"),
            ("files", "↓  Download Files", "Filter by file type"),
            ("analysis", "▤  Analysis Folders", "Browse output datasets"),
        )
        for key, title, detail in nav_items:
            button = tk.Button(
                self.sidebar, text=f"{title}\n    {detail}", command=lambda name=key: self.show_page(name),
                bg=NAVY, fg="white", activebackground=NAVY_LIGHT, activeforeground="white",
                relief="flat", bd=0, anchor="w", justify="left", padx=18, pady=10,
                font=("Segoe UI", 10, "bold"), cursor="hand2",
            )
            button.pack(fill="x")
            self.nav_buttons[key] = button

        tk.Label(
            self.sidebar,
            text="Select a workflow above.\nConnection and project choices\nare shared between pages.",
            bg=NAVY, fg="#9fb9cc", justify="left", anchor="sw",
            font=("Segoe UI", 8), padx=18, pady=18,
        ).pack(side="bottom", fill="x")

        right = tk.Frame(body, bg=SURFACE)
        right.pack(side="left", fill="both", expand=True)
        page_header = tk.Frame(
            right, bg="white", padx=18, pady=10,
            highlightthickness=1, highlightbackground="#dfe4e8",
        )
        page_header.pack(fill="x")
        self.page_title = tk.Label(
            page_header, bg="white", fg="#1c2b36",
            font=("Segoe UI", 15, "bold"), anchor="w",
        )
        self.page_title.pack(anchor="w")
        self.page_subtitle = tk.Label(
            page_header, bg="white", fg=MUTED, font=("Segoe UI", 9), anchor="w",
        )
        self.page_subtitle.pack(anchor="w", pady=(1, 0))

        self.page_host = ttk.Frame(right, style="Workspace.TFrame")
        self.page_host.pack(fill="both", expand=True)
        self.page_host.rowconfigure(0, weight=1)
        self.page_host.columnconfigure(0, weight=1)

    def _build_pages(self):
        upload_page = ttk.Frame(self.page_host)
        files_page = ttk.Frame(self.page_host)
        analysis_page = ttk.Frame(self.page_host)
        for page in (upload_page, files_page, analysis_page):
            page.grid(row=0, column=0, sticky="nsew")

        self.uploader = BaseSpaceUploader(upload_page, show_header=False, configure_window=False)
        self.uploader.pack(fill="both", expand=True)
        self.downloader = BaseSpaceDownloader(files_page, show_header=False, configure_window=False)
        self.downloader.pack(fill="both", expand=True)
        self.analysis_downloader = AnalysisFolderDownloader(
            analysis_page, show_header=False, configure_window=False,
        )
        self.analysis_downloader.pack(fill="both", expand=True)
        self.pages = {"upload": upload_page, "files": files_page, "analysis": analysis_page}

    def show_page(self, name):
        self.pages[name].tkraise()
        title, subtitle = self.page_details[name]
        self.page_title.configure(text=title)
        self.page_subtitle.configure(text=subtitle)
        for key, button in self.nav_buttons.items():
            button.configure(bg=NAVY_LIGHT if key == name else NAVY)

    def _bind_shared_state(self):
        tools = (self.uploader, self.downloader, self.analysis_downloader)
        common_bs_path = self.uploader.bs_path_var.get()
        for tool in tools[1:]:
            tool.bs_path_var.set(common_bs_path)
        self._link_group([tool.bs_path_var for tool in tools])
        self._link_group([tool.project_var for tool in tools])

        self.uploader.auth_status_var.trace_add(
            "write", lambda *_: self._sync_authentication(self.uploader, self.downloader)
        )
        self.downloader.auth_status_var.trace_add(
            "write", lambda *_: self._sync_authentication(self.downloader, self.uploader)
        )

    def _link_group(self, variables):
        for source in variables:
            source.trace_add("write", lambda *_args, current=source: self._copy_to_group(current, variables))

    def _copy_to_group(self, source, variables):
        if self._syncing:
            return
        self._syncing = True
        try:
            value = source.get()
            for target in variables:
                if target is not source and target.get() != value:
                    target.set(value)
        finally:
            self._syncing = False

    def _sync_authentication(self, source, target):
        if self._syncing or source.auth_status_var.get() != "Authenticated":
            return
        self._syncing = True
        try:
            target.authenticated = True
            target.auth_status_var.set("Authenticated")
            target.user_var.set(source.user_var.get())
            if hasattr(target, "_update_readiness"):
                target._update_readiness()
        finally:
            self._syncing = False


if __name__ == "__main__":
    app = BaseSpaceTransferManager()
    app.mainloop()
