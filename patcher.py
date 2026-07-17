from __future__ import annotations

import ctypes
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox

from patcher_engine import (
    InvalidServerError,
    PatcherError,
    inspect_swf,
    normalize_server_url,
    patch_swf,
    probe_server,
    restore_swf,
)


APP_TITLE = "Portal 2 - The Final Hours Patcher"
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ASSET_DIR = APP_DIR / "assets"
DEFAULT_SWF_PATHS = (
    Path(
        r"C:\Program Files (x86)\Steam\steamapps\common\The Final Hours of Portal 2"
        r"\applicationStorageDirectory\swf\TheFinalHoursOfPortal2_10-17.swf"
    ),
    Path(
        r"E:\SteamLibrary\steamapps\common\The Final Hours of Portal 2"
        r"\applicationStorageDirectory\swf\TheFinalHoursOfPortal2_10-17.swf"
    ),
)


def find_default_swf():
    return next((path for path in DEFAULT_SWF_PATHS if path.is_file()), DEFAULT_SWF_PATHS[0])

BG = "#1b1b1b"
PANEL = "#262627"
FIELD = "#151719"
BORDER = "#3c3c3d"
TEXT = "#d6d7d8"
MUTED = "#8d969e"
BLUE = "#53accc"
GREEN = "#9acb56"
ORANGE = "#d79a45"
BUTTON = "#364963"
BUTTON_HOVER = "#445b7c"
PROJECT_GITHUB = "https://github.com/nikolan123/TFHoP2-patcher"


class SteamButton(tk.Label):
    def __init__(self, parent, text, command, primary=False):
        self.command = command
        self.enabled = True
        self.normal = "#477d95" if primary else BUTTON
        self.hover = "#5b9bb7" if primary else BUTTON_HOVER
        super().__init__(
            parent,
            text=text.upper(),
            bg=self.normal,
            fg="#f4f6f7",
            font=("Segoe UI", 9),
            padx=16,
            pady=7,
            cursor="hand2",
        )
        self.bind("<Enter>", lambda _e: self.enabled and self.configure(bg=self.hover))
        self.bind("<Leave>", lambda _e: self.enabled and self.configure(bg=self.normal))
        self.bind("<ButtonRelease-1>", lambda _e: self.enabled and self.command())

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(
            bg=self.normal if enabled else "#292d31",
            fg="#f4f6f7" if enabled else "#686e73",
            cursor="hand2" if enabled else "arrow",
        )


class Patcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        try:
            self.iconbitmap(default=str(ASSET_DIR / "patcher.ico"))
        except tk.TclError:
            pass
        self.resizable(False, False)
        self.configure(bg=BG)
        self.overrideredirect(True)
        self.bind("<Map>", self._restore_chrome)

        self.swf = tk.StringVar(value=str(find_default_swf()))
        self.server = tk.StringVar(value="https://TFHoP2.nikolan.net")
        self.status = tk.StringVar(value="Checking installation...")
        self.accept_risk = tk.BooleanVar(value=False)
        self._busy = False
        self._inspection = None
        self._check_after = None
        self.header_image = tk.PhotoImage(file=str(ASSET_DIR / "clientnostretch.png"))
        self.logo_texture = tk.PhotoImage(file=str(ASSET_DIR / "logotexture.png"))

        self._build()
        self._center_window()
        self.swf.trace_add("write", self._schedule_check)
        self.after(50, self.check)

    def _center_window(self):
        self.update_idletasks()
        width = max(540, self.winfo_reqwidth())
        height = self.winfo_reqheight() + 8
        x = (self.winfo_screenwidth() - width) // 2
        y = (self.winfo_screenheight() - height) // 2
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self):
        titlebar = tk.Frame(self, height=28, bg="#25282c", highlightbackground="#0b0c0d", highlightthickness=1)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        titlebar.bind("<ButtonPress-1>", self._drag_start)
        titlebar.bind("<B1-Motion>", self._drag_move)

        title = tk.Label(
            titlebar,
            text="  PORTAL 2 - THE FINAL HOURS PATCHER",
            bg="#25282c",
            fg="#aeb4b8",
            anchor="w",
            font=("Segoe UI", 8),
        )
        title.pack(side="left", fill="both", expand=True)
        title.bind("<ButtonPress-1>", self._drag_start)
        title.bind("<B1-Motion>", self._drag_move)

        self._window_button(titlebar, "×", self.destroy, close=True).pack(side="right", fill="y")

        header = tk.Canvas(self, height=66, bg="#222222", highlightthickness=0)
        header.pack(fill="x")
        header.create_image(0, 0, image=self.header_image, anchor="nw")
        header.create_image(535, -24, image=self.logo_texture, anchor="ne")
        header.create_text(
            16,
            25,
            text="Portal 2 - The Final Hours Patcher",
            anchor="w",
            fill="#e5e5e5",
            font=("Segoe UI", 15),
        )
        github_link = header.create_text(
            17,
            49,
            text="PROJECT GITHUB",
            anchor="w",
            fill=BLUE,
            font=("Segoe UI", 8, "bold"),
        )
        header.create_text(
            123,
            49,
            text="BY NIKO",
            anchor="w",
            fill="#8d969e",
            font=("Segoe UI", 8),
        )
        header.tag_bind(github_link, "<ButtonRelease-1>", lambda _e: webbrowser.open(PROJECT_GITHUB))
        header.tag_bind(github_link, "<Enter>", lambda _e: (header.itemconfigure(github_link, fill="#97c0e3"), header.configure(cursor="hand2")))
        header.tag_bind(github_link, "<Leave>", lambda _e: (header.itemconfigure(github_link, fill=BLUE), header.configure(cursor="")))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=14)
        body.grid_columnconfigure(0, weight=1)

        tk.Label(
            body,
            text="Select the installed ebook and the revival server to use.",
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Segoe UI", 9),
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        self._label(body, "BOOK LOCATION", 1)
        self.swf_entry = self._entry(body, self.swf, 1)
        self.swf_entry.grid(row=2, column=0, sticky="ew", ipady=6)
        self.browse_button = SteamButton(body, "Browse", self.browse)
        self.browse_button.grid(row=2, column=1, padx=(8, 0))

        self._label(body, "REVIVAL SERVER", 3, top=11)
        self.server_entry = self._entry(body, self.server, 4)
        self.server_entry.grid(row=4, column=0, columnspan=2, sticky="ew", ipady=6)

        disclaimer = tk.Label(
            body,
            text=(
                "Unofficial fan preservation project. Not affiliated with or endorsed by "
                "Valve Corporation or Geoff Keighley. Original SWF backups are always created."
            ),
            wraplength=492,
            justify="left",
            anchor="w",
            bg=PANEL,
            fg="#a9afb4",
            font=("Segoe UI", 8),
            padx=10,
            pady=8,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        disclaimer.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(11, 8))

        self.risk_checkbox = tk.Checkbutton(
            body,
            text="I understand this modifies local files and is provided as-is, without warranty.\nI accept responsibility for using it.",
            variable=self.accept_risk,
            command=self._update_patch_state,
            justify="left",
            anchor="w",
            wraplength=465,
            bg=BG,
            activebackground=BG,
            fg=TEXT,
            activeforeground=TEXT,
            selectcolor=FIELD,
            font=("Segoe UI", 8),
            bd=0,
            highlightthickness=0,
        )
        self.risk_checkbox.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 7))

        self.status_label = tk.Label(
            body,
            textvariable=self.status,
            anchor="w",
            justify="left",
            wraplength=492,
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
        )
        self.status_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 9))

        actions = tk.Frame(body, bg=BG)
        actions.grid(row=8, column=0, columnspan=2, sticky="ew")
        self.restore_button = SteamButton(actions, "Restore", self.restore)
        self.restore_button.pack(side="left")
        self.patch_button = SteamButton(actions, "Patch", self.patch, primary=True)
        self.patch_button.pack(side="right")
        self.restore_button.set_enabled(False)
        self.patch_button.set_enabled(False)

    @staticmethod
    def _window_button(parent, text, command, close=False):
        normal = "#25282c"
        hover = "#6b3b3b" if close else "#3b4249"
        button = tk.Label(
            parent,
            text=text,
            width=4,
            bg=normal,
            fg="#b9bec2",
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        button.bind("<Enter>", lambda _e: button.configure(bg=hover, fg="#ffffff"))
        button.bind("<Leave>", lambda _e: button.configure(bg=normal, fg="#b9bec2"))
        button.bind("<ButtonRelease-1>", lambda _e: command())
        return button

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _drag_move(self, event):
        self.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _restore_chrome(self, _event=None):
        if self.state() == "normal":
            self.after_idle(lambda: self.overrideredirect(True))

    def _update_patch_state(self):
        self._update_action_state()

    @staticmethod
    def _label(parent, text, row, top=0):
        tk.Label(
            parent,
            text=text,
            bg=BG,
            fg=BLUE,
            anchor="w",
            font=("Segoe UI", 8, "bold"),
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(top, 4))

    @staticmethod
    def _entry(parent, variable, row):
        entry = tk.Entry(
            parent,
            textvariable=variable,
            bg=FIELD,
            fg=TEXT,
            insertbackground="#ffffff",
            selectbackground=BUTTON,
            relief="flat",
            font=("Segoe UI", 9),
            highlightbackground=BORDER,
            highlightcolor=BLUE,
            highlightthickness=1,
        )
        return entry

    def browse(self):
        initial = Path(self.swf.get()).parent
        selected = filedialog.askopenfilename(
            title="Select The Final Hours SWF",
            initialdir=str(initial) if initial.exists() else None,
            filetypes=(("Flash movie", "*.swf"), ("All files", "*.*")),
        )
        if selected:
            self.swf.set(selected)

    def _schedule_check(self, *_args):
        if self._check_after is not None:
            self.after_cancel(self._check_after)
        self._check_after = self.after(350, self.check)

    def check(self):
        self._check_after = None
        if self._busy:
            return
        try:
            inspection = inspect_swf(self.swf.get().strip())
        except PatcherError as exc:
            self._inspection = None
            self.status.set(str(exc))
            self.status_label.configure(fg=ORANGE)
        else:
            self._inspection = inspection
            if inspection.state == "original":
                message = "Original ebook SWFs detected."
            elif inspection.state == "patched":
                servers = ", ".join(inspection.servers) or "a revival server"
                message = f"Patched to {servers}."
            else:
                message = "Partially patched. Patch again to repair it."
            if inspection.backup_exists:
                message += " Backup available."
            self.status.set(message)
            self.status_label.configure(fg=GREEN)
        self._update_action_state()

    def _update_action_state(self):
        valid = self._inspection is not None
        self.patch_button.set_enabled(valid and self.accept_risk.get() and not self._busy)
        self.restore_button.set_enabled(
            valid and self._inspection.backup_exists and not self._busy
        )

    def _set_busy(self, busy):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.swf_entry.configure(state=state)
        self.server_entry.configure(state=state)
        self.risk_checkbox.configure(state=state)
        self.browse_button.set_enabled(not busy)
        self._update_action_state()

    def patch(self):
        if self._busy or self._inspection is None or not self.accept_risk.get():
            return
        try:
            server = normalize_server_url(self.server.get())
        except InvalidServerError as exc:
            self.status.set(str(exc))
            self.status_label.configure(fg=ORANGE)
            return
        self._set_busy(True)
        self.status.set("Checking the revival server...")
        self.status_label.configure(fg=MUTED)

        def probe():
            status = probe_server(server)
            self.after(0, lambda: self._after_probe(status))

        threading.Thread(target=probe, daemon=True).start()

    def _after_probe(self, server_status):
        if not server_status.reachable:
            proceed = messagebox.askyesno(
                "Revival server unavailable",
                f"The patcher could not reach {server_status.url}.\n\n"
                f"{server_status.detail}\n\nPatch the SWF anyway?",
                parent=self,
            )
            if not proceed:
                self._set_busy(False)
                self.status.set("Patching cancelled. No files were changed.")
                self.status_label.configure(fg=ORANGE)
                return
        self.status.set("Creating original backups and patching all ebook SWFs...")
        swf_path = self.swf.get().strip()
        self._run_operation(
            lambda: patch_swf(swf_path, server_status.url)
        )

    def restore(self):
        if self._busy or self._inspection is None or not self._inspection.backup_exists:
            return
        self._set_busy(True)
        self.status.set("Restoring the original SWF...")
        self.status_label.configure(fg=MUTED)
        swf_path = self.swf.get().strip()
        self._run_operation(lambda: restore_swf(swf_path))

    def _run_operation(self, operation):
        def worker():
            try:
                result = operation()
            except Exception as exc:
                self.after(0, lambda error=exc: self._finish_operation(None, error))
            else:
                self.after(0, lambda: self._finish_operation(result, None))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_operation(self, result, error):
        self._set_busy(False)
        if error is not None:
            if isinstance(error, PatcherError):
                message = str(error)
            else:
                message = f"Unexpected patcher error: {error}"
            self.status.set(message)
            self.status_label.configure(fg=ORANGE)
            self.check()
            self.status.set(message)
            self.status_label.configure(fg=ORANGE)
            return

        try:
            self._inspection = inspect_swf(self.swf.get().strip())
        except PatcherError:
            self._inspection = None
        self._update_action_state()
        self.status.set(result.message)
        self.status_label.configure(fg=GREEN)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    Patcher().mainloop()


if __name__ == "__main__":
    main()
