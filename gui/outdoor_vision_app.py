"""Outdoor Vision CV Windows desktop application home page."""

from __future__ import annotations

from datetime import datetime
import math
import os
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from app_models import AppState, ProjectRecord
from app_storage import StateLoadError, StateStore
from home_logic import (
    deletion_phrase_matches,
    filter_and_sort_projects,
    paginate,
    progress_values,
)
from project_services import (
    LIBRARY_NAME,
    ValidationError,
    apply_scan_results,
    create_library,
    create_project,
    delete_project,
    move_library,
    recover_library,
    remove_stale_project,
    rename_project,
    scan_project,
    validate_project_name,
)

try:
    from tkinterdnd2 import TkinterDnD
except ImportError:
    TkinterDnD = None


APP_TITLE = "Outdoor Vision CV"
BG = "#17181b"
SURFACE = "#202226"
SURFACE_ALT = "#292c31"
SURFACE_HOVER = "#30343a"
BORDER = "#3b3e45"
TEXT = "#f1f3f5"
MUTED = "#a1a7b0"
SUBTLE = "#737983"
ACCENT = "#8b7cf6"
ACCENT_HOVER = "#9d91ff"
DANGER = "#df6262"
SUCCESS = "#62b685"
MISSING = "#242529"
FONT = "Segoe UI"
CARD_HEIGHT = 94


def format_local_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        local = parsed.astimezone()
        return local.strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    except (ValueError, TypeError):
        return "Unknown"


class OutdoorVisionApp:
    def __init__(self, root: tk.Tk, store: StateStore | None = None) -> None:
        self.root = root
        self.store = store or StateStore()
        self.state = AppState()
        self.app_state = "home_loading"
        self.current_page = 0
        self.page_capacity = 5
        self.card_height = CARD_HEIGHT
        self.resize_job: str | None = None
        self.active_dialog: tk.Toplevel | None = None
        self.intake_ui = None
        self.stats_refresh_running = False
        self.stats_refresh_pending = False

        self.root.title(APP_TITLE)
        self.root.geometry("1120x790")
        self.root.minsize(920, 720)
        self.root.configure(background=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._configure_styles()

        try:
            self.state = self.store.load()
        except StateLoadError as error:
            messagebox.showerror(
                APP_TITLE,
                f"Outdoor Vision CV could not load its saved application data.\n\n{error}\n\n"
                "Rename or remove the damaged app_state.json file, then reopen the app.",
            )
            self.app_state = "error"
            self._show_fatal_state(str(error))
            return

        if not self.state.library_path:
            self._show_library_setup(recovery=False)
        elif not Path(self.state.library_path).is_dir():
            self._show_library_setup(recovery=True)
        else:
            self._build_home()

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=(FONT, 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure(
            "TButton",
            background=SURFACE_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            padding=(12, 7),
        )
        style.map(
            "TButton",
            background=[("active", SURFACE_HOVER), ("disabled", SURFACE)],
            foreground=[("disabled", SUBTLE)],
        )
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            padding=(15, 8),
            font=(FONT, 10, "bold"),
        )
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER)])
        style.configure(
            "Danger.TButton",
            background=DANGER,
            foreground="#ffffff",
            bordercolor=DANGER,
            padding=(12, 7),
            font=(FONT, 10, "bold"),
        )
        style.configure(
            "TEntry",
            fieldbackground=SURFACE_ALT,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=BORDER,
            padding=7,
        )
        style.configure(
            "TCombobox",
            fieldbackground=SURFACE_ALT,
            background=SURFACE_ALT,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor=BORDER,
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", SURFACE_ALT)],
            foreground=[("readonly", TEXT)],
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#383b41",
            background=ACCENT,
            bordercolor="#383b41",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

    def _clear_root(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()

    def _show_fatal_state(self, detail: str) -> None:
        self._clear_root()
        frame = ttk.Frame(self.root, padding=40)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Application data needs attention", font=(FONT, 22, "bold")).pack(pady=(90, 12))
        ttk.Label(frame, text=detail, foreground=MUTED, justify="center", wraplength=720).pack()

    def _show_library_setup(self, recovery: bool) -> None:
        self.app_state = "library_recovery_required" if recovery else "first_run_setup"
        self._clear_root()
        shell = ttk.Frame(self.root, padding=44)
        shell.pack(fill="both", expand=True)
        card = tk.Frame(shell, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        card.place(relx=0.5, rely=0.47, anchor="center", width=680, height=420)
        title = "Reconnect your project library" if recovery else "Welcome to Outdoor Vision CV"
        description = (
            "The saved project library is missing or unavailable. Choose the parent folder where "
            f"{LIBRARY_NAME} should live. If it was deleted, the app will recreate an empty library. "
            "Existing project records will be preserved as missing projects so you can remove them safely."
            if recovery
            else "Choose an accessible location for your project library. The app will create one folder named "
            f"{LIBRARY_NAME} and remember it for future launches."
        )
        tk.Label(card, text=title, bg=SURFACE, fg=TEXT, font=(FONT, 22, "bold")).pack(anchor="w", padx=34, pady=(34, 10))
        tk.Label(card, text=description, bg=SURFACE, fg=MUTED, font=(FONT, 10), justify="left", wraplength=600).pack(anchor="w", padx=34)
        parent_var = tk.StringVar()
        preview_var = tk.StringVar(value=f"Select a parent folder to continue.")
        row = tk.Frame(card, bg=SURFACE)
        row.pack(fill="x", padx=34, pady=(28, 8))
        entry = ttk.Entry(row, textvariable=parent_var, state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def browse() -> None:
            chosen = filedialog.askdirectory(title="Choose project-library parent folder")
            if chosen:
                parent_var.set(chosen)
                preview_var.set(str(Path(chosen) / LIBRARY_NAME))

        ttk.Button(row, text="Browse...", command=browse).pack(side="right")
        tk.Label(card, textvariable=preview_var, bg=SURFACE, fg=SUBTLE, font=(FONT, 9), anchor="w").pack(fill="x", padx=34)

        def continue_setup() -> None:
            if not parent_var.get():
                messagebox.showerror(APP_TITLE, "Choose a parent folder first.", parent=self.root)
                return
            parent = Path(parent_var.get())
            previous_library_path = self.state.library_path
            previous_project_paths = {
                project.project_id: project.path for project in self.state.projects
            }
            created_library = False
            try:
                if recovery:
                    created_library = not (parent / LIBRARY_NAME).exists()
                    library = recover_library(parent)
                    self.state.library_path = str(library.resolve())
                    for project in self.state.projects:
                        project.path = str((library / project.name).resolve())
                else:
                    library = create_library(parent)
                    created_library = True
                    self.state.library_path = str(library)
                try:
                    self.store.save(self.state)
                except Exception:
                    if created_library and library.is_dir():
                        try:
                            library.rmdir()
                        except OSError:
                            pass
                    self.state.library_path = previous_library_path
                    for project in self.state.projects:
                        project.path = previous_project_paths[project.project_id]
                    raise
            except Exception as error:
                messagebox.showerror(APP_TITLE, str(error), parent=self.root)
                return
            self._build_home()

        ttk.Button(card, text="Continue", style="Accent.TButton", command=continue_setup).pack(anchor="e", padx=34, pady=(54, 0))

    def _build_home(self) -> None:
        self._dispose_intake()
        self.app_state = "home_ready"
        self.current_page = 0
        self._clear_root()
        self.search_var = tk.StringVar()
        self.sort_var = tk.StringVar(value="Last edited")
        self.status_var = tk.StringVar(value="Ready")

        self.home = ttk.Frame(self.root, padding=(30, 22, 30, 16))
        self.home.pack(fill="both", expand=True)
        self.home.columnconfigure(0, weight=1)
        self.home.rowconfigure(2, weight=1)

        header = ttk.Frame(self.home)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Outdoor Vision CV", font=(FONT, 22, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Project library", foreground=MUTED).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.settings_button = ttk.Button(header, text="Settings", command=self._show_settings)
        self.settings_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 10))
        self.new_button = ttk.Button(header, text="+  New Project", style="Accent.TButton", command=self._show_new_project)
        self.new_button.grid(row=0, column=2, rowspan=2, sticky="e")

        toolbar = ttk.Frame(self.home)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="Search", foreground=MUTED).grid(
            row=0, column=0, padx=(0, 8)
        )
        search = ttk.Entry(toolbar, textvariable=self.search_var)
        search.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        search.insert(0, "")
        self.search_var.trace_add("write", lambda *_: self._filters_changed())
        self.sort_combo = ttk.Combobox(
            toolbar,
            textvariable=self.sort_var,
            values=("Last edited", "Project name"),
            state="readonly",
            width=17,
        )
        self.sort_combo.grid(row=0, column=2, padx=(0, 10))
        self.sort_combo.bind("<<ComboboxSelected>>", lambda _event: self._filters_changed())
        self.refresh_button = ttk.Button(toolbar, text="Refresh", command=self._refresh_statistics)
        self.refresh_button.grid(row=0, column=3)

        self.list_shell = tk.Frame(self.home, bg=BG)
        self.list_shell.grid(row=2, column=0, sticky="nsew")
        self.list_shell.bind("<Configure>", self._schedule_capacity_update)

        footer = ttk.Frame(self.home)
        footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var, foreground=MUTED).grid(row=0, column=0, sticky="w")
        self.prev_button = ttk.Button(footer, text="Previous", command=lambda: self._change_page(-1))
        self.prev_button.grid(row=0, column=1, padx=(0, 8))
        self.page_label = ttk.Label(footer, text="Page 1 of 1", anchor="center", width=14)
        self.page_label.grid(row=0, column=2)
        self.next_button = ttk.Button(footer, text="Next", command=lambda: self._change_page(1))
        self.next_button.grid(row=0, column=3, padx=(8, 0))
        self._render_projects()

    def _filtered_projects(self) -> list[ProjectRecord]:
        return filter_and_sort_projects(
            self.state.projects, self.search_var.get(), self.sort_var.get()
        )

    def _filters_changed(self) -> None:
        self.current_page = 0
        self._render_projects()

    def _schedule_capacity_update(self, _event: tk.Event) -> None:
        if self.resize_job is not None:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(120, self._recalculate_capacity)

    def _recalculate_capacity(self) -> None:
        self.resize_job = None
        available = max(1, self.list_shell.winfo_height())
        capacity = max(5, available // (CARD_HEIGHT + 8))
        card_height = CARD_HEIGHT
        if capacity == 5 and available < (CARD_HEIGHT * 5 + 8 * 4):
            card_height = max(72, (available - 8 * 4) // 5)
        if capacity != self.page_capacity or card_height != self.card_height:
            first_index = self.current_page * self.page_capacity
            self.page_capacity = capacity
            self.card_height = card_height
            self.current_page = first_index // self.page_capacity
            self._render_projects()

    def _render_projects(self) -> None:
        if not hasattr(self, "list_shell"):
            return
        for child in self.list_shell.winfo_children():
            child.destroy()
        projects = self._filtered_projects()
        if not self.state.projects:
            self._render_empty_state("No projects yet", "Create your first computer-vision project to get started.")
            self._update_pagination(0)
            return
        if not projects:
            self._render_empty_state("No matching projects", "Try a different project name or clear the search field.")
            self._update_pagination(0)
            return
        visible, self.current_page, _page_count = paginate(
            projects, self.current_page, self.page_capacity
        )
        for index, project in enumerate(visible):
            self._build_project_card(project, index, index == len(visible) - 1)
        self._update_pagination(len(projects))

    def _render_empty_state(self, title: str, message: str) -> None:
        card = tk.Frame(self.list_shell, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)
        tk.Label(card, text=title, bg=SURFACE, fg=TEXT, font=(FONT, 16, "bold")).place(relx=0.5, rely=0.43, anchor="center")
        tk.Label(card, text=message, bg=SURFACE, fg=MUTED, font=(FONT, 10)).place(relx=0.5, rely=0.51, anchor="center")

    def _build_project_card(
        self, project: ProjectRecord, index: int, is_last: bool
    ) -> None:
        missing = not project.folder.is_dir()
        background = MISSING if missing else SURFACE
        card = tk.Frame(
            self.list_shell,
            bg=background,
            height=self.card_height,
            cursor="arrow" if missing else "hand2",
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        card.pack(fill="x", pady=(0, 0 if is_last else 8))
        card.pack_propagate(False)
        card.columnconfigure(0, weight=1)

        left = tk.Frame(card, bg=background)
        left.pack(side="left", fill="both", expand=True, padx=(18, 8), pady=12)
        name = tk.Label(left, text=project.name, bg=background, fg=SUBTLE if missing else TEXT, font=(FONT, 13, "bold"), anchor="w")
        name.pack(fill="x")
        if missing:
            path_label = tk.Label(left, text="Project Folder Missing", bg=background, fg=DANGER, font=(FONT, 9, "bold"), anchor="w")
        else:
            path_label = tk.Label(left, text=project.path, bg=background, fg=ACCENT, font=(FONT, 8, "underline"), anchor="w", cursor="hand2")
            path_label.bind("<Button-1>", lambda _event, p=project: self._open_folder(p))
        path_label.pack(anchor="w", pady=(4, 0))
        tk.Label(left, text=f"Last edited {format_local_timestamp(project.last_edited)}", bg=background, fg=SUBTLE, font=(FONT, 8), anchor="w").pack(fill="x", pady=(5, 0))

        labeled, total, percentage = progress_values(project)
        stats = tk.Frame(card, bg=background, width=265)
        stats.pack(side="left", fill="y", padx=8, pady=15)
        stats.pack_propagate(False)
        tk.Label(stats, text=f"{total} image{'s' if total != 1 else ''}", bg=background, fg=MUTED, font=(FONT, 9), anchor="w").pack(fill="x")
        ttk.Progressbar(stats, value=percentage, maximum=100).pack(fill="x", pady=(8, 5))
        tk.Label(stats, text=f"{labeled} / {total} labeled  -  {percentage}%", bg=background, fg=SUBTLE, font=(FONT, 8), anchor="w").pack(fill="x")

        ellipsis = tk.Button(
            card,
            text="⋯",
            bg=background,
            fg=TEXT,
            activebackground=SURFACE_HOVER,
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            font=(FONT, 18),
            cursor="hand2",
            command=lambda p=project: self._show_project_menu(p),
        )
        ellipsis.pack(side="right", padx=(4, 14))
        if not missing:
            interactive_widgets = {path_label, ellipsis}
            card_widgets = [card]
            for widget in card_widgets:
                card_widgets.extend(widget.winfo_children())
            for widget in card_widgets:
                if widget in interactive_widgets:
                    continue
                widget.bind("<Button-1>", lambda _event, p=project: self._open_project(p))

    def _update_pagination(self, project_count: int) -> None:
        pages = max(1, math.ceil(project_count / self.page_capacity))
        self.current_page = min(self.current_page, pages - 1)
        self.page_label.configure(text=f"Page {self.current_page + 1} of {pages}")
        self.prev_button.configure(state="normal" if self.current_page > 0 else "disabled")
        self.next_button.configure(state="normal" if self.current_page < pages - 1 else "disabled")

    def _change_page(self, delta: int) -> None:
        projects = self._filtered_projects()
        pages = max(1, math.ceil(len(projects) / self.page_capacity))
        self.current_page = min(max(0, self.current_page + delta), pages - 1)
        self._render_projects()

    def _set_busy(self, state: str, message: str) -> None:
        self.app_state = state
        self.status_var.set(message)
        for control in (self.new_button, self.settings_button, self.refresh_button, self.sort_combo):
            control.configure(state="disabled")

    def _clear_busy(self, message: str = "Ready") -> None:
        self.app_state = "home_ready"
        self.status_var.set(message)
        for control in (self.new_button, self.settings_button, self.refresh_button, self.sort_combo):
            control.configure(state="normal" if control is not self.sort_combo else "readonly")

    def _refresh_statistics(self) -> None:
        if self.app_state != "home_ready":
            return
        self._set_busy("refreshing_statistics", "Refreshing project statistics...")
        projects = list(self.state.projects)

        def work() -> None:
            results = [scan_project(project) for project in projects]
            self.root.after(0, lambda: self._finish_refresh(results))

        threading.Thread(target=work, daemon=True).start()

    def _finish_refresh(self, results) -> None:
        try:
            failures = apply_scan_results(self.state, self.store, results)
        except Exception as error:
            self._clear_busy("Refresh failed")
            messagebox.showerror(APP_TITLE, f"Project statistics could not be saved:\n\n{error}", parent=self.root)
            return
        self._render_projects()
        if failures:
            self._clear_busy(f"Refresh completed with {len(failures)} issue(s)")
            messagebox.showwarning(APP_TITLE, "Some projects could not be scanned:\n\n" + "\n".join(failures), parent=self.root)
        else:
            self._clear_busy(f"Refreshed {len(results)} project(s)")

    def _show_new_project(self) -> None:
        self._show_name_dialog("New Project", "Create Project", self._create_named_project)

    def _create_named_project(self, name: str) -> None:
        create_project(self.state, self.store, name)
        self.current_page = 0
        self._render_projects()
        self.status_var.set(f"Created project '{name}'")

    def _show_name_dialog(
        self, title: str, action_label: str, action: Callable[[str], None], initial: str = "", exclude_id: str | None = None
    ) -> None:
        if self.app_state != "home_ready":
            return
        dialog = self._dialog(title, 520, 300)
        tk.Label(dialog, text=title, bg=SURFACE, fg=TEXT, font=(FONT, 17, "bold")).pack(anchor="w", padx=24, pady=(22, 8))
        tk.Label(dialog, text="Project name (1-25 characters)", bg=SURFACE, fg=MUTED).pack(anchor="w", padx=24)
        name_var = tk.StringVar(value=initial)
        entry = ttk.Entry(dialog, textvariable=name_var)
        entry.pack(fill="x", padx=24, pady=(7, 8))
        tk.Label(dialog, text=f"Location: {self.state.library_path}", bg=SURFACE, fg=SUBTLE, wraplength=470, justify="left").pack(anchor="w", padx=24)
        error_var = tk.StringVar()
        error_label = tk.Label(dialog, textvariable=error_var, bg=SURFACE, fg=DANGER, wraplength=470, justify="left")
        error_label.pack(anchor="w", padx=24, pady=(9, 0))
        buttons = tk.Frame(dialog, bg=SURFACE)
        buttons.pack(side="bottom", fill="x", padx=24, pady=20)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        submit = ttk.Button(buttons, text=action_label, style="Accent.TButton")
        submit.pack(side="right", padx=(0, 8))

        def validate(*_args) -> bool:
            try:
                validate_project_name(
                    name_var.get(),
                    library=Path(self.state.library_path),
                    projects=self.state.projects,
                    exclude_project_id=exclude_id,
                )
                error_var.set("")
                submit.configure(state="normal")
                return True
            except ValidationError as error:
                error_var.set(str(error))
                submit.configure(state="disabled")
                return False

        def submit_action() -> None:
            if not validate():
                return
            try:
                action(name_var.get())
            except Exception as error:
                messagebox.showerror(APP_TITLE, str(error), parent=dialog)
                return
            dialog.destroy()

        submit.configure(command=submit_action)
        name_var.trace_add("write", validate)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Return>", lambda _event: submit_action())
        entry.focus_set()
        entry.selection_range(0, "end")
        validate()

    def _show_project_menu(self, project: ProjectRecord) -> None:
        menu = tk.Menu(self.root, tearoff=False, bg=SURFACE_ALT, fg=TEXT, activebackground=ACCENT, activeforeground="#ffffff")
        if project.folder.is_dir():
            menu.add_command(label="Rename", command=lambda: self._show_rename(project))
            menu.add_command(label="Delete", command=lambda: self._show_delete(project))
        else:
            menu.add_command(label="Remove from list", command=lambda: self._confirm_remove_stale(project))
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _show_rename(self, project: ProjectRecord) -> None:
        self._show_name_dialog(
            "Rename Project",
            "Rename",
            lambda name: self._rename_project(project, name),
            initial=project.name,
            exclude_id=project.project_id,
        )

    def _rename_project(self, project: ProjectRecord, name: str) -> None:
        rename_project(self.state, self.store, project, name)
        self._render_projects()
        self.status_var.set(f"Renamed project to '{name}'")

    def _show_delete(self, project: ProjectRecord) -> None:
        dialog = self._dialog("Permanently Delete Project", 610, 380)
        phrase = f"DELETE {project.name}"
        tk.Label(dialog, text="Permanently delete this project?", bg=SURFACE, fg=TEXT, font=(FONT, 17, "bold")).pack(anchor="w", padx=24, pady=(22, 10))
        tk.Label(dialog, text="This permanently deletes the folder and everything inside it:", bg=SURFACE, fg=MUTED).pack(anchor="w", padx=24)
        tk.Label(dialog, text=str(project.folder), bg=SURFACE, fg=DANGER, wraplength=555, justify="left").pack(anchor="w", padx=24, pady=(6, 18))
        tk.Label(dialog, text=f"Type {phrase} to confirm:", bg=SURFACE, fg=TEXT).pack(anchor="w", padx=24)
        typed = tk.StringVar()
        entry = ttk.Entry(dialog, textvariable=typed)
        entry.pack(fill="x", padx=24, pady=(7, 10))
        buttons = tk.Frame(dialog, bg=SURFACE)
        buttons.pack(side="bottom", fill="x", padx=24, pady=20)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        confirm = ttk.Button(buttons, text="Delete Permanently", style="Danger.TButton", state="disabled")
        confirm.pack(side="right", padx=(0, 8))

        def update_state(*_args) -> None:
            confirm.configure(state="normal" if typed.get() == phrase else "disabled")

        def delete_now() -> None:
            if not deletion_phrase_matches(typed.get(), project.name):
                return
            confirm.configure(state="disabled")
            try:
                delete_project(self.state, self.store, project)
            except Exception as error:
                messagebox.showerror(APP_TITLE, f"The project could not be deleted:\n\n{error}", parent=dialog)
                update_state()
                return
            dialog.destroy()
            self._render_projects()
            self.status_var.set(f"Deleted project '{project.name}'")
            messagebox.showinfo(APP_TITLE, f"Project '{project.name}' was permanently deleted.", parent=self.root)

        typed.trace_add("write", update_state)
        confirm.configure(command=delete_now)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        entry.focus_set()

    def _confirm_remove_stale(self, project: ProjectRecord) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            f"Remove '{project.name}' from the project list?\n\nThe folder is already missing, so no files will be deleted.",
            parent=self.root,
        ):
            return
        try:
            remove_stale_project(self.state, self.store, project)
        except Exception as error:
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
            return
        self._render_projects()
        self.status_var.set(f"Removed stale project '{project.name}'")

    def _open_folder(self, project: ProjectRecord) -> None:
        try:
            os.startfile(str(project.folder))  # type: ignore[attr-defined]
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"File Explorer could not open this folder:\n{project.folder}\n\n{error}", parent=self.root)

    def _open_project(self, project: ProjectRecord) -> None:
        if self.app_state != "home_ready" or not project.folder.is_dir():
            return
        self.show_project_tools(project)

    def show_project_tools(self, project: ProjectRecord) -> None:
        """Show the reusable project workspace and its available tool cards."""

        self._dispose_intake()
        self.app_state = "project_tools"
        self._clear_root()
        shell = ttk.Frame(self.root, padding=34)
        shell.pack(fill="both", expand=True)
        ttk.Button(shell, text="←  Back to Projects", command=self._return_home).pack(anchor="w")
        ttk.Label(shell, text=project.name, font=(FONT, 24, "bold")).pack(anchor="w", pady=(28, 3))
        ttk.Label(shell, text=project.path, foreground=MUTED).pack(anchor="w")
        ttk.Label(shell, text="Project Tools", font=(FONT, 14, "bold")).pack(anchor="w", pady=(30, 10))

        card = tk.Frame(
            shell,
            bg=SURFACE,
            highlightbackground=BORDER,
            highlightthickness=1,
            cursor="hand2",
            height=150,
            takefocus=True,
        )
        card.pack(fill="x")
        card.pack_propagate(False)
        title = tk.Label(card, text="Intake", bg=SURFACE, fg=TEXT, font=(FONT, 17, "bold"), anchor="w", cursor="hand2")
        title.pack(fill="x", padx=22, pady=(27, 7))
        description = tk.Label(
            card,
            text="Import JPEG images into the project, create classes, and sort images into the master dataset.",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 10),
            anchor="w",
            cursor="hand2",
        )
        description.pack(fill="x", padx=22)
        for widget in (card, title, description):
            widget.bind("<Button-1>", lambda _event, p=project: self._open_intake(p))
        card.bind("<Return>", lambda _event, p=project: self._open_intake(p))
        card.bind("<space>", lambda _event, p=project: self._open_intake(p))
        card.bind("<FocusIn>", lambda _event: card.configure(highlightbackground=ACCENT, highlightthickness=2))
        card.bind("<FocusOut>", lambda _event: card.configure(highlightbackground=BORDER, highlightthickness=1))

    def _open_intake(self, project: ProjectRecord) -> None:
        try:
            from intake_ui import IntakeUI

            self._dispose_intake()
            self.intake_ui = IntakeUI(self, project)
        except Exception as error:
            self.intake_ui = None
            messagebox.showerror(APP_TITLE, f"Intake could not be opened:\n\n{error}", parent=self.root)
            self.show_project_tools(project)

    def _dispose_intake(self) -> None:
        controller = self.intake_ui
        if controller is not None:
            controller.dispose()
        self.intake_ui = None

    def _return_home(self) -> None:
        self._build_home()
        self._refresh_statistics()

    def refresh_project_record(self, project: ProjectRecord) -> None:
        """Recount one project off the UI thread, coalescing rapid requests."""

        if self.stats_refresh_running:
            self.stats_refresh_pending = True
            return
        self.stats_refresh_running = True

        def work() -> None:
            result = scan_project(project)
            self.root.after(0, lambda: finish(result))

        def finish(result) -> None:
            try:
                apply_scan_results(self.state, self.store, [result])
            except Exception:
                # The initiating Intake operation has already reported its own
                # success or failure. A later manual Refresh remains available.
                pass
            self.stats_refresh_running = False
            if self.stats_refresh_pending:
                self.stats_refresh_pending = False
                self.refresh_project_record(project)

        threading.Thread(target=work, daemon=True).start()

    def _show_settings(self) -> None:
        if self.app_state != "home_ready":
            return
        dialog = self._dialog("Settings", 680, 410)
        tk.Label(dialog, text="Settings", bg=SURFACE, fg=TEXT, font=(FONT, 19, "bold")).pack(anchor="w", padx=26, pady=(24, 4))
        tk.Label(dialog, text="Project library location", bg=SURFACE, fg=TEXT, font=(FONT, 11, "bold")).pack(anchor="w", padx=26, pady=(20, 4))
        tk.Label(dialog, text=self.state.library_path, bg=SURFACE, fg=MUTED, wraplength=620, justify="left").pack(anchor="w", padx=26)
        parent_var = tk.StringVar()
        row = tk.Frame(dialog, bg=SURFACE)
        row.pack(fill="x", padx=26, pady=(22, 5))
        entry = ttk.Entry(row, textvariable=parent_var, state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def browse() -> None:
            chosen = filedialog.askdirectory(title="Choose new project-library parent", parent=dialog)
            if chosen:
                parent_var.set(chosen)

        ttk.Button(row, text="Browse...", command=browse).pack(side="right")
        tk.Label(dialog, text=f"The complete {LIBRARY_NAME} folder and all projects will move together. Existing destination libraries are never merged or overwritten.", bg=SURFACE, fg=SUBTLE, wraplength=620, justify="left").pack(anchor="w", padx=26, pady=(5, 0))
        buttons = tk.Frame(dialog, bg=SURFACE)
        buttons.pack(side="bottom", fill="x", padx=26, pady=22)
        close_button = ttk.Button(buttons, text="Close", command=dialog.destroy)
        close_button.pack(side="right")
        move_button = ttk.Button(buttons, text="Move Library", style="Accent.TButton")
        move_button.pack(side="right", padx=(0, 8))

        def start_move() -> None:
            if not parent_var.get():
                messagebox.showerror(APP_TITLE, "Choose a new parent folder first.", parent=dialog)
                return
            destination = Path(parent_var.get()) / LIBRARY_NAME
            if not messagebox.askyesno(
                APP_TITLE,
                f"Move the complete project library to:\n{destination}\n\nContinue?",
                parent=dialog,
            ):
                return
            move_button.configure(state="disabled")
            close_button.configure(state="disabled")
            dialog.protocol("WM_DELETE_WINDOW", lambda: None)
            self._set_busy("moving_library", "Moving project library...")

            def work() -> None:
                try:
                    moved_to = move_library(self.state, self.store, Path(parent_var.get()))
                    self.root.after(0, lambda: finish_move(moved_to, None))
                except Exception as error:
                    self.root.after(0, lambda caught=error: finish_move(None, caught))

            threading.Thread(target=work, daemon=True).start()

        def finish_move(moved_to: Path | None, error: Exception | None) -> None:
            if error:
                self._clear_busy("Library move failed")
                move_button.configure(state="normal")
                close_button.configure(state="normal")
                dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
                messagebox.showerror(APP_TITLE, f"The project library could not be moved:\n\n{error}", parent=dialog)
                return
            dialog.destroy()
            self._clear_busy(f"Project library moved to {moved_to}")
            self._render_projects()

        move_button.configure(command=start_move)
        dialog.bind("<Escape>", lambda _event: dialog.destroy() if self.app_state == "home_ready" else None)

    def _dialog(self, title: str, width: int, height: int) -> tk.Toplevel:
        dialog = tk.Toplevel(self.root)
        dialog.title(f"{APP_TITLE} - {title}")
        dialog.configure(bg=SURFACE)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        return dialog

    def _close(self) -> None:
        if self.intake_ui is not None and not self.intake_ui.confirm_close():
            return
        self._dispose_intake()
        if self.resize_job is not None:
            self.root.after_cancel(self.resize_job)
        self.root.destroy()


def main() -> None:
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    OutdoorVisionApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
