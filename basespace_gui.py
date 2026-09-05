"""Unified graphical interface for BaseSpace uploads and downloads."""

import tkinter as tk
from tkinter import ttk

from basespace_downloader import BaseSpaceDownloader
from basespace_uploader import BaseSpaceUploader


APP_TITLE = "BaseSpace Transfer Manager"


class BaseSpaceTransferManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x780")
        self.minsize(1000, 680)

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Manager.TNotebook", tabmargins=(6, 5, 6, 0))
        style.configure("Manager.TNotebook.Tab", font=("Segoe UI", 10, "bold"), padding=(20, 7))
        style.map(
            "Manager.TNotebook.Tab",
            foreground=[("selected", "#123b5d"), ("!selected", "#5f6b7a")],
        )

        header = tk.Frame(self, bg="#123b5d", padx=18, pady=8)
        header.pack(fill="x")
        tk.Label(
            header,
            text=APP_TITLE,
            bg="#123b5d",
            fg="white",
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Upload and download sequencing data",
            bg="#123b5d",
            fg="#d7e7f3",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(1, 0))

        notebook = ttk.Notebook(self, style="Manager.TNotebook")
        notebook.pack(fill="both", expand=True)

        upload_tab = ttk.Frame(notebook)
        download_tab = ttk.Frame(notebook)
        notebook.add(upload_tab, text="  Upload FASTQs  ")
        notebook.add(download_tab, text="  Download Files  ")

        self.uploader = BaseSpaceUploader(upload_tab, show_header=False, configure_window=False)
        self.uploader.pack(fill="both", expand=True)

        self.downloader = BaseSpaceDownloader(download_tab, show_header=False, configure_window=False)
        self.downloader.pack(fill="both", expand=True)

        self._syncing = False
        self._bind_shared_state()

    def _bind_shared_state(self):
        """Keep common connection choices synchronized across both tabs."""
        self.downloader.bs_path_var.set(self.uploader.bs_path_var.get())

        self._link_variables(self.uploader.bs_path_var, self.downloader.bs_path_var)
        self._link_variables(self.uploader.project_var, self.downloader.project_var)

        self.uploader.auth_status_var.trace_add(
            "write", lambda *_: self._sync_authentication(self.uploader, self.downloader)
        )
        self.downloader.auth_status_var.trace_add(
            "write", lambda *_: self._sync_authentication(self.downloader, self.uploader)
        )

    def _link_variables(self, first, second):
        first.trace_add("write", lambda *_: self._copy_variable(first, second))
        second.trace_add("write", lambda *_: self._copy_variable(second, first))

    def _copy_variable(self, source, target):
        if self._syncing or source.get() == target.get():
            return
        self._syncing = True
        try:
            target.set(source.get())
        finally:
            self._syncing = False

    def _sync_authentication(self, source, target):
        if self._syncing:
            return
        status = source.auth_status_var.get()
        if status != "Authenticated":
            return
        self._syncing = True
        try:
            target.authenticated = True
            target.auth_status_var.set(status)
            target.user_var.set(source.user_var.get())
            if hasattr(target, "_update_readiness"):
                target._update_readiness()
        finally:
            self._syncing = False


if __name__ == "__main__":
    app = BaseSpaceTransferManager()
    app.mainloop()
