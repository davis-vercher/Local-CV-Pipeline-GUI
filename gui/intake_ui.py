"""Tkinter screens for the project-integrated Intake workflow."""

from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from PIL import Image, ImageOps, ImageTk

from app_models import ProjectRecord
from intake_services import (
    ClassificationRecord,
    DiscoveryResult,
    IntakeManifest,
    IntakeStore,
    IntakeValidationError,
    ManifestError,
    MappingError,
    StaleQueueItemError,
    active_queue,
    class_contents,
    classify_item,
    copy_discovery,
    create_class,
    discover_classes,
    discover_source,
    ensure_intake_structure,
    recycle_class,
    rename_class,
    repair_manifest,
    require_destination_space,
    review_dataset,
    skip_item,
    undo_classification,
    validate_class_name,
)


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
DANGER = "#df6262"
SUCCESS = "#62b685"
FONT = "Segoe UI"
UNASSIGNED = "— Unassigned —"


class IntakeUI:
    """Owns Intake session state while delegating shared navigation to the app."""

    def __init__(self, app: Any, project: ProjectRecord) -> None:
        self.app = app
        self.root: tk.Tk = app.root
        self.project = project
        self.store = IntakeStore(project.project_id, app.store.app_data_dir)
        self.unsorted, self.sorted_folder = ensure_intake_structure(project.folder)
        self.manifest: IntakeManifest | None = None
        self.manifest_error: str | None = None
        self.mappings: dict[str, str] = {}
        self.mapping_error: str | None = None
        self.cancel_event: threading.Event | None = None
        self.worker_queue: queue.Queue[tuple[str, Any]] | None = None
        self.poll_job: str | None = None
        self.key_binding: str | None = None
        self.resize_job: str | None = None
        self.current_photo: ImageTk.PhotoImage | None = None
        self.current_image: Image.Image | None = None
        self.undo_history: list[ClassificationRecord] = []
        self.session_total = 0
        self.session_counts: Counter[str] = Counter()
        self.sorting_busy = False
        self.worker_active = False
        self.disposed = False
        self._load_private_state()
        self.show_landing()

    def _load_private_state(self) -> None:
        try:
            self.manifest = self.store.load_manifest()
            self.manifest_error = None
        except ManifestError as error:
            self.manifest = None
            self.manifest_error = str(error)
        try:
            self.mappings = self.store.load_mappings()
            self.mapping_error = None
        except MappingError as error:
            self.mappings = {}
            self.mapping_error = str(error)
            messagebox.showerror(
                APP_TITLE,
                f"Class mappings could not be loaded. Intake will not overwrite them.\n\n{error}",
                parent=self.root,
            )

    def _clear(self) -> None:
        if self.poll_job:
            self.root.after_cancel(self.poll_job)
            self.poll_job = None
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
            self.resize_job = None
        if self.key_binding:
            self.root.unbind("<KeyPress>", self.key_binding)
            self.key_binding = None
        self.current_photo = None
        self.current_image = None
        self.app._clear_root()

    def dispose(self) -> None:
        """Detach controller-wide callbacks before another app screen takes over."""

        if self.disposed:
            return
        self.disposed = True
        if self.poll_job:
            self.root.after_cancel(self.poll_job)
            self.poll_job = None
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
            self.resize_job = None
        if self.key_binding:
            self.root.unbind("<KeyPress>", self.key_binding)
            self.key_binding = None
        self.current_photo = None
        self.current_image = None

    def _screen(self, title: str, subtitle: str = "") -> ttk.Frame:
        self._clear()
        shell = ttk.Frame(self.root, padding=(28, 20, 28, 18))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text=title, font=(FONT, 22, "bold")).pack(anchor="w")
        if subtitle:
            ttk.Label(header, text=subtitle, foreground=MUTED).pack(anchor="w", pady=(3, 0))
        return shell

    def _scrollable(self, parent: tk.Widget) -> ttk.Frame:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(frame, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        body = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        return body

    def _dialog(self, title: str, width: int, height: int) -> tk.Toplevel:
        dialog = tk.Toplevel(self.root)
        dialog.title(f"{APP_TITLE} - {title}")
        dialog.configure(bg=SURFACE)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        return dialog

    def _run_worker(
        self,
        work: Callable[[Callable[[Any], None]], Any],
        done: Callable[[Any, Exception | None], None],
        progress: Callable[[Any], None] | None = None,
    ) -> None:
        messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker_queue = messages
        self.worker_active = True

        def report(value: Any) -> None:
            messages.put(("progress", value))

        def runner() -> None:
            try:
                messages.put(("done", work(report)))
            except Exception as error:
                messages.put(("error", error))

        threading.Thread(target=runner, daemon=True).start()

        def poll() -> None:
            self.poll_job = None
            try:
                while True:
                    kind, value = messages.get_nowait()
                    if kind == "progress" and progress:
                        progress(value)
                    elif kind == "done":
                        self.worker_active = False
                        done(value, None)
                        return
                    elif kind == "error":
                        self.worker_active = False
                        done(None, value)
                        return
            except queue.Empty:
                self.poll_job = self.root.after(60, poll)

        self.poll_job = self.root.after(20, poll)

    def show_landing(self) -> None:
        self.app.app_state = "intake_landing"
        shell = self._screen("Intake", f"{self.project.name}  •  Project-managed JPEG intake and sorting")
        top = ttk.Frame(shell)
        top.pack(fill="x", pady=(0, 12))
        ttk.Button(top, text="←  Back to Project Tools", command=self._back_to_tools).pack(side="left")
        ttk.Button(top, text="Review Master Dataset", command=self.show_review).pack(side="right")

        if self.manifest_error:
            warning = tk.Frame(shell, bg="#35282a", highlightbackground=DANGER, highlightthickness=1)
            warning.pack(fill="x", pady=(0, 12))
            tk.Label(
                warning,
                text="The Intake queue manifest is corrupt or unreadable. Sorting is blocked.",
                bg="#35282a", fg=TEXT, font=(FONT, 10, "bold"), anchor="w",
            ).pack(side="left", padx=14, pady=12)
            ttk.Button(warning, text="Repair Intake Queue", command=self._repair_queue).pack(side="right", padx=12, pady=8)
        if self.mapping_error:
            warning = tk.Frame(shell, bg="#35282a", highlightbackground=DANGER, highlightthickness=1)
            warning.pack(fill="x", pady=(0, 12))
            tk.Label(
                warning,
                text="The saved class mapping is corrupt or unreadable. Mapping changes are blocked.",
                bg="#35282a", fg=TEXT, font=(FONT, 10, "bold"), anchor="w",
            ).pack(side="left", padx=14, pady=12)
            ttk.Button(warning, text="Reset Class Mappings", command=self._reset_mappings).pack(side="right", padx=12, pady=8)

        content = self._scrollable(shell)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        import_card = tk.Frame(content, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        import_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(import_card, text="Import JPEGs", bg=SURFACE, fg=TEXT, font=(FONT, 15, "bold")).pack(anchor="w", padx=18, pady=(18, 5))
        tk.Label(
            import_card,
            text="Drop one local folder here, or browse. Nested JPEGs are discovered recursively; originals are never changed.",
            bg=SURFACE, fg=MUTED, justify="left", wraplength=410,
        ).pack(anchor="w", padx=18)
        drop = tk.Label(
            import_card,
            text="Drop one folder here\n\nor\n",
            bg=SURFACE_ALT, fg=TEXT, relief="flat", bd=0,
            highlightbackground=BORDER, highlightthickness=1,
            font=(FONT, 12, "bold"), height=8, cursor="hand2",
        )
        drop.pack(fill="x", padx=18, pady=(18, 8))
        drop.bind("<Button-1>", lambda _event: self._browse_source())
        browse = ttk.Button(import_card, text="Browse for Folder", command=self._browse_source)
        browse.pack(padx=18, pady=(0, 18))
        if self.manifest_error:
            browse.configure(state="disabled")
            drop.configure(text="Repair the Intake queue before importing", cursor="arrow")
            drop.unbind("<Button-1>")
        else:
            self._register_drop_target(drop)

        queue_count = len(active_queue(self.manifest, self.unsorted)) if self.manifest else 0
        queue_frame = tk.Frame(import_card, bg=SURFACE_ALT)
        queue_frame.pack(fill="x", padx=18, pady=(6, 18))
        tk.Label(queue_frame, text="Unsorted queue", bg=SURFACE_ALT, fg=MUTED).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(queue_frame, text=f"{queue_count} registered image{'s' if queue_count != 1 else ''}", bg=SURFACE_ALT, fg=TEXT, font=(FONT, 16, "bold")).pack(anchor="w", padx=12, pady=(2, 10))

        classes_card = tk.Frame(content, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        classes_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tk.Label(classes_card, text="Classes and number keys", bg=SURFACE, fg=TEXT, font=(FONT, 15, "bold")).pack(anchor="w", padx=18, pady=(18, 3))
        classes = discover_classes(self.sorted_folder)
        mapping_area = tk.Frame(classes_card, bg=SURFACE)
        mapping_area.pack(fill="x", padx=18, pady=(6, 8))
        for row, key in enumerate("0123456789"):
            tk.Label(mapping_area, text=key, bg=SURFACE, fg=TEXT, font=(FONT, 10, "bold"), width=2).grid(row=row, column=0, sticky="w", pady=2)
            current = self.mappings.get(key, UNASSIGNED)
            if current not in classes:
                current = UNASSIGNED
            variable = tk.StringVar(value=current)
            combo = ttk.Combobox(mapping_area, textvariable=variable, values=(UNASSIGNED, *classes), state="readonly", width=27)
            combo.grid(row=row, column=1, sticky="ew", padx=(5, 0), pady=2)
            combo.bind("<<ComboboxSelected>>", lambda _event, k=key, v=variable: self._change_mapping(k, v.get()))
        mapping_area.columnconfigure(1, weight=1)

        mapped_names = {name.casefold() for name in self.mappings.values()}
        unmapped = [name for name in classes if name.casefold() not in mapped_names]
        tk.Label(classes_card, text="Unmapped Classes", bg=SURFACE, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", padx=18, pady=(5, 2))
        unmapped_frame = tk.Frame(classes_card, bg=SURFACE)
        unmapped_frame.pack(fill="x", padx=18)
        if not unmapped:
            tk.Label(unmapped_frame, text="None", bg=SURFACE, fg=SUBTLE).pack(anchor="w")
        for name in unmapped:
            row = tk.Frame(unmapped_frame, bg=SURFACE)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=name, bg=SURFACE, fg=TEXT, anchor="w").pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="Assign Key", command=lambda value=name: self._assign_unmapped(value)).pack(side="right")

        class_row = tk.Frame(classes_card, bg=SURFACE)
        class_row.pack(fill="x", padx=18, pady=(12, 8))
        self.class_list = tk.Listbox(
            class_row, height=4, bg=SURFACE_ALT, fg=TEXT, selectbackground=ACCENT,
            selectforeground="#ffffff", highlightbackground=BORDER, relief="flat",
            exportselection=False,
        )
        self.class_list.pack(fill="x")
        for name in classes:
            self.class_list.insert("end", name)
        actions = tk.Frame(classes_card, bg=SURFACE)
        actions.pack(fill="x", padx=18, pady=(0, 12))
        ttk.Button(actions, text="New Class", command=self._new_class).pack(side="left")
        ttk.Button(actions, text="Rename", command=self._rename_selected_class).pack(side="left", padx=6)
        ttk.Button(actions, text="Delete", command=self._delete_selected_class).pack(side="left")

        has_mapping = any(name in classes for name in self.mappings.values())
        can_sort = queue_count > 0 and has_mapping and self.manifest is not None
        start = ttk.Button(classes_card, text="Start / Resume Sorting", style="Accent.TButton", command=self.start_sorting)
        start.pack(fill="x", padx=18, pady=(4, 8))
        if not can_sort:
            start.configure(state="disabled")
        guidance = "Ready to sort."
        if self.manifest_error:
            guidance = "Repair the Intake queue before sorting."
        elif queue_count == 0:
            guidance = "Import images before sorting."
        elif not has_mapping:
            guidance = "Assign at least one class to a number key."
        tk.Label(classes_card, text=guidance, bg=SURFACE, fg=MUTED, wraplength=390).pack(anchor="w", padx=18, pady=(0, 16))

    def _register_drop_target(self, widget: tk.Widget) -> None:
        try:
            from tkinterdnd2 import DND_FILES

            widget.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            widget.dnd_bind("<<Drop>>", self._folder_dropped)  # type: ignore[attr-defined]
        except (ImportError, AttributeError, tk.TclError):
            # Browse remains a complete fallback if the optional Tk DND extension
            # could not initialize on this Windows installation.
            return

    def _folder_dropped(self, event: tk.Event) -> None:
        paths = [Path(value) for value in self.root.tk.splitlist(str(event.data))]  # type: ignore[attr-defined]
        if len(paths) != 1 or not paths[0].is_dir():
            messagebox.showerror(APP_TITLE, "Select or drop one folder at a time.", parent=self.root)
            return
        self._begin_scan(paths[0])

    def _browse_source(self) -> None:
        chosen = filedialog.askdirectory(title="Select one JPEG source folder", parent=self.root)
        if chosen:
            self._begin_scan(Path(chosen))

    def _begin_scan(self, source: Path) -> None:
        resolved_source = source.expanduser().resolve()
        project_root = self.project.folder.resolve()
        if resolved_source == project_root or project_root in resolved_source.parents:
            messagebox.showerror(APP_TITLE, "Select a source folder outside this project.", parent=self.root)
            return
        self.app.app_state = "intake_scanning"
        shell = self._screen("Scanning source folder", str(source))
        card = tk.Frame(shell, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)
        status = tk.StringVar(value="Discovering and validating JPEG files…")
        count = tk.StringVar(value="0 filesystem entries examined")
        tk.Label(card, textvariable=status, bg=SURFACE, fg=TEXT, font=(FONT, 15, "bold")).pack(pady=(150, 12))
        tk.Label(card, textvariable=count, bg=SURFACE, fg=MUTED).pack()
        bar = ttk.Progressbar(card, mode="indeterminate", length=420)
        bar.pack(pady=20)
        bar.start(12)
        self.cancel_event = threading.Event()
        ttk.Button(card, text="Cancel", command=self.cancel_event.set).pack()

        def work(report: Callable[[Any], None]) -> DiscoveryResult:
            return discover_source(source, self.unsorted, self.cancel_event, report)

        def progress(value: Any) -> None:
            count.set(f"{int(value)} filesystem entries examined")

        def done(result: DiscoveryResult | None, error: Exception | None) -> None:
            bar.stop()
            if error:
                messagebox.showerror(APP_TITLE, f"The source folder could not be scanned:\n\n{error}", parent=self.root)
                self.show_landing()
            elif result is None or result.cancelled:
                self.show_landing()
            else:
                self.show_preview(result)

        self._run_worker(work, done, progress)

    def show_preview(self, discovery: DiscoveryResult) -> None:
        self.app.app_state = "intake_preview"
        shell = self._screen("Import Preview", "Review the scan before any files are copied")
        card = tk.Frame(shell, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)
        grid = tk.Frame(card, bg=SURFACE)
        grid.pack(fill="x", padx=24, pady=22)
        values = (
            ("Source", str(discovery.source)),
            ("Destination", str(discovery.destination)),
            ("Valid JPEGs", str(len(discovery.valid))),
            ("Invalid / unreadable", str(len(discovery.invalid))),
            ("Hidden / system / reparse skipped", str(discovery.hidden_or_system_skipped)),
            ("Filename collisions", str(discovery.collision_count)),
            ("Estimated copy bytes", f"{discovery.total_bytes:,}"),
        )
        for row, (label, value) in enumerate(values):
            tk.Label(grid, text=label, bg=SURFACE, fg=MUTED, anchor="w", width=28).grid(row=row, column=0, sticky="nw", pady=6)
            tk.Label(grid, text=value, bg=SURFACE, fg=TEXT, anchor="w", justify="left", wraplength=650).grid(row=row, column=1, sticky="w", pady=6)
        tk.Label(
            card,
            text="Intake copies files and leaves the originals unchanged. Copies are flattened into intake/unsorted.",
            bg=SURFACE, fg=TEXT, wraplength=820, justify="left",
        ).pack(anchor="w", padx=24, pady=(4, 14))
        if discovery.invalid:
            ttk.Button(card, text="Show invalid files", command=lambda: self._show_details("Invalid or unreadable JPEGs", discovery.invalid)).pack(anchor="w", padx=24)
        buttons = tk.Frame(card, bg=SURFACE)
        buttons.pack(side="bottom", fill="x", padx=24, pady=22)
        ttk.Button(buttons, text="Cancel", command=self.show_landing).pack(side="right")
        import_button = ttk.Button(buttons, text="Import", style="Accent.TButton", command=lambda: self._begin_copy(discovery))
        import_button.pack(side="right", padx=(0, 8))
        try:
            free = require_destination_space(self.unsorted, 0)
        except OSError as error:
            import_button.configure(state="disabled")
            tk.Label(card, text=f"Available space could not be determined: {error}", bg=SURFACE, fg=DANGER).pack(anchor="w", padx=24)
        else:
            if free < discovery.total_bytes:
                import_button.configure(state="disabled")
                tk.Label(
                    card,
                    text=f"Insufficient space. Required: {discovery.total_bytes:,} bytes. Available: {free:,} bytes.",
                    bg=SURFACE, fg=DANGER,
                ).pack(anchor="w", padx=24)

    def _begin_copy(self, discovery: DiscoveryResult) -> None:
        # Recheck capacity at the action boundary because free space can change
        # while the preview is displayed.
        try:
            require_destination_space(self.unsorted, discovery.total_bytes)
        except IntakeValidationError as error:
            messagebox.showerror(
                APP_TITLE,
                f"Import cannot start because destination space is insufficient.\n\n{error}",
                parent=self.root,
            )
            return
        except OSError as error:
            messagebox.showerror(
                APP_TITLE,
                f"Import cannot start because destination space could not be checked:\n\n{error}",
                parent=self.root,
            )
            return
        if self.manifest is None:
            messagebox.showerror(APP_TITLE, "Repair the Intake queue before importing.", parent=self.root)
            return
        self.app.app_state = "intake_copying"
        shell = self._screen("Copying JPEGs", str(self.unsorted))
        card = tk.Frame(shell, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)
        status = tk.StringVar(value=f"0 of {len(discovery.valid)} files copied")
        bytes_var = tk.StringVar(value=f"0 of {discovery.total_bytes:,} bytes")
        tk.Label(card, textvariable=status, bg=SURFACE, fg=TEXT, font=(FONT, 15, "bold")).pack(pady=(145, 8))
        tk.Label(card, textvariable=bytes_var, bg=SURFACE, fg=MUTED).pack()
        bar = ttk.Progressbar(card, maximum=max(1, len(discovery.valid)), length=500)
        bar.pack(pady=22)
        self.cancel_event = threading.Event()
        ttk.Button(card, text="Cancel", command=self.cancel_event.set).pack()

        def work(report: Callable[[Any], None]) -> Any:
            return copy_discovery(
                discovery,
                self.store,
                self.manifest,  # type: ignore[arg-type]
                self.cancel_event,
                lambda a, b, c, d: report((a, b, c, d)),
            )

        def progress(value: Any) -> None:
            copied, total, byte_count, total_bytes = value
            bar.configure(value=copied)
            status.set(f"{copied} of {total} files copied")
            bytes_var.set(f"{byte_count:,} of {total_bytes:,} bytes")

        def done(result: Any, error: Exception | None) -> None:
            if error:
                messagebox.showerror(APP_TITLE, f"Import failed:\n\n{error}", parent=self.root)
                self.show_landing()
                return
            self.app.refresh_project_record(self.project)
            self.show_results(result)

        self._run_worker(work, done, progress)

    def show_results(self, result: Any) -> None:
        self.app.app_state = "intake_results"
        shell = self._screen("Import Results", "Successful partial copies remain in the project queue")
        card = tk.Frame(shell, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)
        summary = (
            ("Valid discovered", result.valid_discovered),
            ("Copied", result.copied),
            ("Invalid / unreadable skipped", len(result.invalid)),
            ("Failed during copying", len(result.failed)),
            ("Collision-renamed", len(result.renamed)),
            ("Total bytes copied", f"{result.bytes_copied:,}"),
            ("Destination", result.destination),
        )
        body = tk.Frame(card, bg=SURFACE)
        body.pack(fill="x", padx=24, pady=22)
        for row, (label, value) in enumerate(summary):
            tk.Label(body, text=label, bg=SURFACE, fg=MUTED, width=27, anchor="w").grid(row=row, column=0, sticky="nw", pady=5)
            tk.Label(body, text=str(value), bg=SURFACE, fg=TEXT, anchor="w", wraplength=650, justify="left").grid(row=row, column=1, sticky="w", pady=5)
        if result.cancelled or result.systemic_error:
            warning = (
                "The import was cancelled." if result.cancelled else f"The import stopped: {result.systemic_error}"
            )
            warning += (
                "\n\nFiles copied before the stop remain in intake/unsorted. Before reattempting or sorting, consider clearing everything in "
                "intake/unsorted. That also removes earlier unsorted imports, but does not affect intake/sorted."
            )
            tk.Label(card, text=warning, bg=SURFACE, fg=DANGER, justify="left", wraplength=820).pack(anchor="w", padx=24, pady=(0, 10))
        details = tk.Frame(card, bg=SURFACE)
        details.pack(fill="x", padx=24)
        if result.invalid:
            ttk.Button(details, text="Invalid details", command=lambda: self._show_details("Invalid or unreadable", result.invalid)).pack(side="left", padx=(0, 6))
        if result.failed:
            ttk.Button(details, text="Failed details", command=lambda: self._show_details("Failed during copying", result.failed)).pack(side="left", padx=(0, 6))
        if result.renamed:
            ttk.Button(details, text="Renamed details", command=lambda: self._show_details("Collision-renamed files", result.renamed)).pack(side="left")
        buttons = tk.Frame(card, bg=SURFACE)
        buttons.pack(side="bottom", fill="x", padx=24, pady=22)
        ttk.Button(buttons, text="Back to Intake", command=self.show_landing).pack(side="right")
        ttk.Button(buttons, text="Import Another Folder", command=self._browse_source).pack(side="right", padx=8)
        queue_ready = self.manifest is not None and active_queue(self.manifest, self.unsorted)
        classes = discover_classes(self.sorted_folder)
        valid_mapping = any(name in classes for name in self.mappings.values())
        start = ttk.Button(buttons, text="Start Sorting", style="Accent.TButton", command=self.start_sorting)
        start.pack(side="right")
        if not queue_ready or not valid_mapping:
            start.configure(state="disabled")

    def _show_details(self, title: str, values: list[str]) -> None:
        dialog = self._dialog(title, 720, 470)
        tk.Label(dialog, text=title, bg=SURFACE, fg=TEXT, font=(FONT, 17, "bold")).pack(anchor="w", padx=22, pady=(20, 10))
        text = tk.Text(dialog, bg=SURFACE_ALT, fg=TEXT, insertbackground=TEXT, wrap="word", relief="flat")
        text.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        text.insert("1.0", "\n".join(values))
        text.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(anchor="e", padx=22, pady=(0, 18))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def _repair_queue(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "Repair Intake Queue will validate direct JPEG files in intake/unsorted and register valid files as one recovered batch. Original source paths and historical batch ordering cannot be recovered. Continue?",
            parent=self.root,
        ):
            return
        try:
            self.manifest, omitted = repair_manifest(self.store, self.unsorted)
            self.manifest_error = None
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"The Intake queue could not be repaired:\n\n{error}", parent=self.root)
            return
        message = f"Queue repaired with {len(self.manifest.items)} valid image(s)."
        if omitted:
            message += f"\n\n{len(omitted)} invalid JPEG file(s) were omitted:\n" + "\n".join(omitted)
        messagebox.showinfo(APP_TITLE, message, parent=self.root)
        self.show_landing()

    def _change_mapping(self, key: str, selection: str) -> None:
        if self.mapping_error:
            messagebox.showerror(APP_TITLE, "Reset the damaged class mappings before making changes.", parent=self.root)
            self.show_landing()
            return
        old = dict(self.mappings)
        if selection == UNASSIGNED:
            self.mappings.pop(key, None)
        else:
            duplicate = next((mapped_key for mapped_key, name in self.mappings.items() if mapped_key != key and name.casefold() == selection.casefold()), None)
            if duplicate is not None:
                messagebox.showerror(APP_TITLE, f"'{selection}' is already assigned to key {duplicate}.", parent=self.root)
                self.show_landing()
                return
            self.mappings[key] = selection
        try:
            self.store.save_mappings(self.mappings)
        except Exception as error:
            self.mappings = old
            messagebox.showerror(APP_TITLE, f"The mapping could not be saved:\n\n{error}", parent=self.root)
        self.show_landing()

    def _reset_mappings(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "Reset the damaged mapping file? All prior number-key assignments will be lost, but class folders and images are not changed.",
            parent=self.root,
        ):
            return
        try:
            self.store.save_mappings({})
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"Class mappings could not be reset:\n\n{error}", parent=self.root)
            return
        self.mappings = {}
        self.mapping_error = None
        self.show_landing()

    def _assign_unmapped(self, class_name: str) -> None:
        if self.mapping_error:
            messagebox.showerror(APP_TITLE, "Reset the damaged class mappings before making changes.", parent=self.root)
            return
        free_keys = [key for key in "0123456789" if key not in self.mappings]
        if not free_keys:
            messagebox.showinfo(APP_TITLE, "All ten number keys are already assigned.", parent=self.root)
            return
        dialog = self._dialog("Assign Key", 420, 235)
        tk.Label(dialog, text=f"Assign a key to {class_name}", bg=SURFACE, fg=TEXT, font=(FONT, 16, "bold")).pack(anchor="w", padx=22, pady=(22, 8))
        selected = tk.StringVar(value=free_keys[0])
        combo = ttk.Combobox(dialog, textvariable=selected, values=free_keys, state="readonly")
        combo.pack(fill="x", padx=22, pady=8)
        row = tk.Frame(dialog, bg=SURFACE)
        row.pack(side="bottom", fill="x", padx=22, pady=20)
        ttk.Button(row, text="Cancel", command=dialog.destroy).pack(side="right")

        def save() -> None:
            old = dict(self.mappings)
            self.mappings[selected.get()] = class_name
            try:
                self.store.save_mappings(self.mappings)
            except Exception as error:
                self.mappings = old
                messagebox.showerror(APP_TITLE, f"The mapping could not be saved:\n\n{error}", parent=dialog)
                return
            dialog.destroy()
            self.show_landing()

        ttk.Button(row, text="Assign", style="Accent.TButton", command=save).pack(side="right", padx=(0, 8))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        combo.focus_set()

    def _selected_class(self) -> str | None:
        selected = self.class_list.curselection()
        if not selected:
            messagebox.showinfo(APP_TITLE, "Select a class first.", parent=self.root)
            return None
        return str(self.class_list.get(selected[0]))

    def _class_name_dialog(self, title: str, initial: str, action: Callable[[str], None]) -> None:
        dialog = self._dialog(title, 500, 270)
        tk.Label(dialog, text=title, bg=SURFACE, fg=TEXT, font=(FONT, 17, "bold")).pack(anchor="w", padx=22, pady=(22, 8))
        tk.Label(dialog, text="Class name (1-25 characters)", bg=SURFACE, fg=MUTED).pack(anchor="w", padx=22)
        value = tk.StringVar(value=initial)
        entry = ttk.Entry(dialog, textvariable=value)
        entry.pack(fill="x", padx=22, pady=(7, 6))
        error_var = tk.StringVar()
        tk.Label(dialog, textvariable=error_var, bg=SURFACE, fg=DANGER, wraplength=450, justify="left").pack(anchor="w", padx=22)
        buttons = tk.Frame(dialog, bg=SURFACE)
        buttons.pack(side="bottom", fill="x", padx=22, pady=20)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        confirm = ttk.Button(buttons, text="Save", style="Accent.TButton")
        confirm.pack(side="right", padx=(0, 8))

        def validate(*_args: Any) -> bool:
            try:
                validate_class_name(value.get(), self.sorted_folder, exclude_name=initial or None)
                error_var.set("")
                confirm.configure(state="normal")
                return True
            except IntakeValidationError as error:
                error_var.set(str(error))
                confirm.configure(state="disabled")
                return False

        def submit() -> None:
            if not validate():
                return
            try:
                action(value.get())
            except Exception as error:
                messagebox.showerror(APP_TITLE, str(error), parent=dialog)
                return
            dialog.destroy()
            self.show_landing()

        confirm.configure(command=submit)
        value.trace_add("write", validate)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Return>", lambda _event: submit())
        entry.focus_set()
        entry.selection_range(0, "end")
        validate()

    def _new_class(self) -> None:
        self._class_name_dialog("New Class", "", lambda name: create_class(self.sorted_folder, name))

    def _rename_selected_class(self) -> None:
        if self.mapping_error:
            messagebox.showerror(APP_TITLE, "Reset the damaged class mappings before renaming a class.", parent=self.root)
            return
        old_name = self._selected_class()
        if not old_name:
            return

        def perform(new_name: str) -> None:
            rename_class(self.sorted_folder, old_name, new_name)
            changed = False
            for key, value in list(self.mappings.items()):
                if value.casefold() == old_name.casefold():
                    self.mappings[key] = new_name
                    changed = True
            if changed:
                try:
                    self.store.save_mappings(self.mappings)
                except Exception:
                    # Restore the physical folder so mapping and disk never diverge
                    # due to a private-state persistence failure.
                    rename_class(self.sorted_folder, new_name, old_name)
                    for key, value in list(self.mappings.items()):
                        if value.casefold() == new_name.casefold():
                            self.mappings[key] = old_name
                    raise

        self._class_name_dialog("Rename Class", old_name, perform)

    def _delete_selected_class(self) -> None:
        if self.mapping_error:
            messagebox.showerror(APP_TITLE, "Reset the damaged class mappings before deleting a class.", parent=self.root)
            return
        name = self._selected_class()
        if not name:
            return
        folder = self.sorted_folder / name
        direct_jpegs, total_files = class_contents(folder)
        if total_files == 0 and not any(folder.iterdir()):
            if not messagebox.askyesno(APP_TITLE, f"Send the empty class '{name}' to the Windows Recycle Bin?", parent=self.root):
                return
        else:
            phrase = f"DELETE {name}"
            dialog = self._dialog("Delete Class", 650, 400)
            tk.Label(dialog, text="Recycle nonempty class", bg=SURFACE, fg=TEXT, font=(FONT, 17, "bold")).pack(anchor="w", padx=24, pady=(22, 8))
            details = f"Class: {name}\nPath: {folder}\nDirect JPEGs: {direct_jpegs}\nTotal files (including nested/non-JPEG): {total_files}\n\nThe class mapping will be removed only after successful recycling."
            tk.Label(dialog, text=details, bg=SURFACE, fg=MUTED, justify="left", wraplength=590).pack(anchor="w", padx=24)
            tk.Label(dialog, text=f"Type {phrase} to confirm:", bg=SURFACE, fg=TEXT).pack(anchor="w", padx=24, pady=(16, 4))
            typed = tk.StringVar()
            entry = ttk.Entry(dialog, textvariable=typed)
            entry.pack(fill="x", padx=24)
            row = tk.Frame(dialog, bg=SURFACE)
            row.pack(side="bottom", fill="x", padx=24, pady=20)
            ttk.Button(row, text="Cancel", command=dialog.destroy).pack(side="right")
            confirm = ttk.Button(row, text="Recycle Class", style="Danger.TButton", state="disabled")
            confirm.pack(side="right", padx=(0, 8))
            typed.trace_add("write", lambda *_: confirm.configure(state="normal" if typed.get() == phrase else "disabled"))
            confirm.configure(command=lambda: (dialog.destroy(), self._perform_delete(name)))
            dialog.bind("<Escape>", lambda _event: dialog.destroy())
            entry.focus_set()
            return
        self._perform_delete(name)

    def _perform_delete(self, name: str) -> None:
        old_mapping = dict(self.mappings)

        def work(_report: Callable[[Any], None]) -> None:
            recycle_class(self.sorted_folder, name)

        def done(_result: Any, error: Exception | None) -> None:
            if error:
                messagebox.showerror(APP_TITLE, f"The class could not be recycled. Nothing was deleted permanently.\n\n{error}", parent=self.root)
                self.show_landing()
                return
            self.mappings = {key: value for key, value in self.mappings.items() if value.casefold() != name.casefold()}
            try:
                self.store.save_mappings(self.mappings)
            except Exception as mapping_error:
                # The folder has already been recycled. Do not claim complete
                # success; retain the old in-memory mapping so recovery is explicit.
                self.mappings = old_mapping
                messagebox.showerror(APP_TITLE, f"The class was recycled, but its mapping could not be updated:\n\n{mapping_error}", parent=self.root)
            self.app.refresh_project_record(self.project)
            self.show_landing()

        shell = self._screen("Recycling class", str(self.sorted_folder / name))
        ttk.Label(shell, text="Sending the class and all contents to the Windows Recycle Bin…", font=(FONT, 14, "bold")).pack(pady=180)
        self._run_worker(work, done)

    def start_sorting(self) -> None:
        if self.manifest is None:
            return
        queue_items = active_queue(self.manifest, self.unsorted)
        classes = discover_classes(self.sorted_folder)
        valid_mappings = {key: name for key, name in self.mappings.items() if name in classes}
        if not queue_items or not valid_mappings:
            self.show_landing()
            return
        self.session_total = len(queue_items)
        self.session_counts.clear()
        self.undo_history.clear()
        self.show_sorting()

    def show_sorting(self) -> None:
        if self.manifest is None:
            self.show_landing()
            return
        queue_items = active_queue(self.manifest, self.unsorted)
        if not queue_items:
            self.show_completion()
            return
        classes = discover_classes(self.sorted_folder)
        valid_mappings = {key: name for key, name in self.mappings.items() if name in classes}
        if not valid_mappings:
            messagebox.showerror(APP_TITLE, "Every mapped class is missing or inaccessible. Sorting has stopped.", parent=self.root)
            self.show_landing()
            return
        self.app.app_state = "intake_sorting"
        shell = self._screen("Sort Images", self.project.name)
        self.current_item = queue_items[0]
        self.current_queue_length = len(queue_items)
        toolbar = ttk.Frame(shell)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="←  Back to Project", command=self._leave_sorting).pack(side="left")
        ttk.Button(toolbar, text="Open Image Externally", command=self._open_current_external).pack(side="right")
        ttk.Button(toolbar, text="Skip", command=self._skip_current).pack(side="right", padx=8)
        undo = ttk.Button(toolbar, text="Undo", command=self._undo)
        undo.pack(side="right")
        if not self.undo_history:
            undo.configure(state="disabled")

        info = tk.Frame(shell, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        info.pack(fill="x", pady=(0, 10))
        self.sort_filename = tk.StringVar(value=self.current_item.current_name)
        self.sort_details = tk.StringVar(value="Loading image…")
        tk.Label(info, textvariable=self.sort_filename, bg=SURFACE, fg=TEXT, font=(FONT, 13, "bold"), anchor="w").pack(fill="x", padx=14, pady=(10, 2))
        tk.Label(info, textvariable=self.sort_details, bg=SURFACE, fg=MUTED, anchor="w", justify="left").pack(fill="x", padx=14, pady=(0, 10))

        self.viewer = tk.Frame(shell, bg="#111214", highlightbackground=BORDER, highlightthickness=1)
        self.viewer.pack(fill="both", expand=True)
        self.image_label = tk.Label(self.viewer, text="Loading image…", bg="#111214", fg=MUTED, justify="center")
        self.image_label.place(relx=0.5, rely=0.5, anchor="center")
        self.viewer.bind("<Configure>", self._schedule_render)

        keybar = tk.Frame(shell, bg=BG)
        keybar.pack(fill="x", pady=(10, 0))
        for column, key in enumerate("0123456789"):
            if key not in valid_mappings:
                continue
            button = ttk.Button(keybar, text=f"{key}: {valid_mappings[key]}", command=lambda k=key: self._classify(k))
            button.grid(row=column // 5, column=column % 5, sticky="ew", padx=3, pady=3)
        for column in range(5):
            keybar.columnconfigure(column, weight=1)
        self.key_binding = self.root.bind("<KeyPress>", self._handle_sort_key, add="+")
        self._load_sort_image()

    def _load_sort_image(self) -> None:
        path = self.unsorted / self.current_item.current_name
        self.sorting_busy = True

        def work(_report: Callable[[Any], None]) -> tuple[Image.Image, tuple[int, int]]:
            with Image.open(path) as opened:
                dimensions = opened.size
                corrected = ImageOps.exif_transpose(opened).convert("RGB").copy()
            return corrected, dimensions

        def done(result: Any, error: Exception | None) -> None:
            self.sorting_busy = False
            sorted_count = sum(self.session_counts.values())
            base = (
                f"Queue position 1 of {self.current_queue_length}  •  Remaining {self.current_queue_length}  •  "
                f"Session start {self.session_total}  •  Sorted this session {sorted_count}\n"
                f"Batch {self.current_item.batch_timestamp}  •  Source {self.current_item.source_relative_path}"
            )
            if error:
                self.current_image = None
                self.image_label.configure(text=f"Unable to display this image.\n\n{error}\n\nIt can still be classified, skipped, or opened externally.", image="")
                self.sort_details.set(base + "  •  Dimensions unavailable")
            else:
                self.current_image, dimensions = result
                self.sort_details.set(base + f"  •  {dimensions[0]} × {dimensions[1]}")
                self._render_image()

        self._run_worker(work, done)

    def _schedule_render(self, _event: tk.Event) -> None:
        if self.current_image is None:
            return
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(100, self._render_image)

    def _render_image(self) -> None:
        self.resize_job = None
        if self.current_image is None:
            return
        width = max(100, self.viewer.winfo_width() - 24)
        height = max(100, self.viewer.winfo_height() - 24)
        resized = self.current_image.copy()
        resized.thumbnail((width, height), Image.Resampling.LANCZOS)
        self.current_photo = ImageTk.PhotoImage(resized)
        self.image_label.configure(image=self.current_photo, text="")

    def _handle_sort_key(self, event: tk.Event) -> None:
        if self.disposed or self.app.intake_ui is not self:
            return
        if self.app.app_state != "intake_sorting" or self.sorting_busy:
            return
        if self.root.focus_displayof() is None:
            return
        focused = self.root.focus_get()
        if focused is not None and focused.winfo_class() in {"Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"}:
            return
        key = event.char
        if key not in "0123456789":
            return
        if self.root.grab_current() not in (None, self.root):
            return
        classes = discover_classes(self.sorted_folder)
        if key not in self.mappings or self.mappings[key] not in classes:
            previous = self.sort_details.get()
            current_name = self.current_item.current_name
            self.sort_details.set(f"No class assigned to key {key}.")

            def restore_feedback() -> None:
                if (
                    self.app.app_state == "intake_sorting"
                    and self.current_item.current_name == current_name
                    and not self.sorting_busy
                ):
                    self.sort_details.set(previous)

            self.root.after(1400, restore_feedback)
            return
        self._classify(key)

    def _classify(self, key: str) -> None:
        if self.disposed or self.app.intake_ui is not self:
            return
        if self.sorting_busy or self.manifest is None:
            return
        class_name = self.mappings.get(key)
        if not class_name:
            return
        self.sorting_busy = True
        item = self.current_item

        def work(_report: Callable[[Any], None]) -> ClassificationRecord:
            return classify_item(self.store, self.manifest, self.unsorted, self.sorted_folder, item, class_name)

        def done(record: ClassificationRecord | None, error: Exception | None) -> None:
            self.sorting_busy = False
            if error:
                if isinstance(error, StaleQueueItemError):
                    try:
                        self.manifest = self.store.load_manifest()
                    except ManifestError:
                        self._load_private_state()
                    if not self.disposed and self.app.intake_ui is self:
                        self.show_sorting()
                    return
                messagebox.showerror(APP_TITLE, f"The image could not be classified:\n\n{error}", parent=self.root)
                # If all mapped folders disappeared, the next render safely exits.
                self.show_sorting()
                return
            self.undo_history.append(record)  # type: ignore[arg-type]
            self.session_counts[class_name] += 1
            self.app.refresh_project_record(self.project)
            self.show_sorting()

        self._run_worker(work, done)

    def _skip_current(self) -> None:
        if self.sorting_busy or self.manifest is None:
            return
        try:
            skip_item(self.store, self.manifest, self.current_item)
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"The queue order could not be saved:\n\n{error}", parent=self.root)
            return
        self.show_sorting()

    def _undo(self) -> None:
        if self.sorting_busy or not self.undo_history or self.manifest is None:
            return
        record = self.undo_history[-1]
        self.sorting_busy = True

        def work(_report: Callable[[Any], None]) -> Any:
            return undo_classification(self.store, self.manifest, self.unsorted, record)

        def done(result: Any, error: Exception | None) -> None:
            self.sorting_busy = False
            if error:
                messagebox.showerror(APP_TITLE, f"The classification could not be undone:\n\n{error}", parent=self.root)
                return
            self.undo_history.pop()
            self.session_counts[record.class_name] -= 1
            self.app.refresh_project_record(self.project)
            if result[1]:
                messagebox.showinfo(APP_TITLE, f"The original unsorted filename was occupied. The image was restored as {result[0].current_name}.", parent=self.root)
            self.show_sorting()

        self._run_worker(work, done)

    def _open_current_external(self) -> None:
        try:
            os.startfile(str(self.unsorted / self.current_item.current_name))  # type: ignore[attr-defined]
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"The image could not be opened externally:\n\n{error}", parent=self.root)

    def _leave_sorting(self) -> None:
        if self.worker_active:
            messagebox.showinfo(APP_TITLE, "Please wait for the current image operation to finish.", parent=self.root)
            return
        if self.undo_history and not messagebox.askyesno(
            APP_TITLE,
            "Leaving Intake will clear the Undo history. Sorted files will remain in their class folders. Continue?",
            parent=self.root,
        ):
            return
        self.undo_history.clear()
        self.app.refresh_project_record(self.project)
        self.app.show_project_tools(self.project)

    def show_completion(self) -> None:
        self.app.app_state = "intake_complete"
        shell = self._screen("Sorting Complete", f"{self.session_total} queued image(s) processed")
        card = tk.Frame(shell, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)
        tk.Label(card, text="Session results", bg=SURFACE, fg=TEXT, font=(FONT, 18, "bold")).pack(pady=(80, 18))
        lines = [f"{name}: {count}" for name, count in sorted(self.session_counts.items(), key=lambda item: item[0].casefold()) if count]
        tk.Label(card, text="\n".join(lines) if lines else "No images were classified.", bg=SURFACE, fg=MUTED, justify="left", font=(FONT, 11)).pack()
        if self.undo_history:
            ttk.Button(card, text="Undo Last Classification", command=self._undo).pack(pady=(22, 4))
        actions = tk.Frame(card, bg=SURFACE)
        actions.pack(side="bottom", pady=36)
        ttk.Button(actions, text="Import More Images", command=self._completion_import).pack(side="left", padx=5)
        ttk.Button(actions, text="Review Master Dataset", command=self._completion_review).pack(side="left", padx=5)
        ttk.Button(actions, text="Return to Project Tools", style="Accent.TButton", command=self._completion_return).pack(side="left", padx=5)

    def _completion_import(self) -> None:
        self.undo_history.clear()
        self.show_landing()
        self._browse_source()

    def _completion_review(self) -> None:
        self.undo_history.clear()
        self.show_review()

    def _completion_return(self) -> None:
        self.undo_history.clear()
        self.app.refresh_project_record(self.project)
        self.app.show_project_tools(self.project)

    def show_review(self) -> None:
        self.app.app_state = "intake_review_loading"
        shell = self._screen("Review Master Dataset", str(self.sorted_folder))
        ttk.Button(shell, text="←  Back to Intake", command=self.show_landing).pack(anchor="w")
        loading = ttk.Label(shell, text="Counting direct JPEG files…", font=(FONT, 14, "bold"))
        loading.pack(pady=190)

        def work(_report: Callable[[Any], None]) -> Any:
            return review_dataset(self.sorted_folder, self.mappings)

        def done(result: Any, error: Exception | None) -> None:
            if error:
                messagebox.showerror(APP_TITLE, f"The master dataset could not be reviewed:\n\n{error}", parent=self.root)
                self.show_landing()
                return
            self._render_review(result)

        self._run_worker(work, done)

    def _render_review(self, result: Any) -> None:
        self.app.app_state = "intake_review"
        shell = self._screen("Review Master Dataset", str(self.sorted_folder))
        toolbar = ttk.Frame(shell)
        toolbar.pack(fill="x", pady=(0, 12))
        ttk.Button(toolbar, text="←  Back to Intake", command=self.show_landing).pack(side="left")
        ttk.Button(toolbar, text="Refresh", command=self.show_review).pack(side="right")
        ttk.Button(toolbar, text="Open sorted in File Explorer", command=lambda: self._open_folder(self.sorted_folder)).pack(side="right", padx=8)
        summary = tk.Frame(shell, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        summary.pack(fill="x", pady=(0, 10))
        tk.Label(summary, text=f"{result.total_jpegs} total direct JPEG image{'s' if result.total_jpegs != 1 else ''}", bg=SURFACE, fg=TEXT, font=(FONT, 17, "bold")).pack(anchor="w", padx=16, pady=14)
        if result.nested_folder_count:
            tk.Label(summary, text=f"Warning: {result.nested_folder_count} nested folder(s) are ignored. Only direct JPEG files count.", bg=SURFACE, fg=DANGER).pack(anchor="w", padx=16, pady=(0, 12))
        scroll_body = self._scrollable(shell)
        list_frame = tk.Frame(scroll_body, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        list_frame.pack(fill="both", expand=True)
        if not result.classes:
            tk.Label(list_frame, text="No class folders yet.", bg=SURFACE, fg=MUTED).pack(pady=80)
        for item in result.classes:
            row = tk.Frame(list_frame, bg=SURFACE_ALT)
            row.pack(fill="x", padx=12, pady=(10, 0))
            tk.Label(row, text=item.name, bg=SURFACE_ALT, fg=TEXT, font=(FONT, 11, "bold"), anchor="w").pack(side="left", padx=12, pady=10)
            ttk.Button(row, text="Open in Explorer", command=lambda path=item.path: self._open_folder(path)).pack(side="right", padx=8, pady=6)
            tk.Label(row, text=f"{item.direct_jpegs} JPEGs", bg=SURFACE_ALT, fg=MUTED).pack(side="right", padx=10)

    def _open_folder(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"File Explorer could not open this folder:\n{path}\n\n{error}", parent=self.root)

    def _back_to_tools(self) -> None:
        self.app.refresh_project_record(self.project)
        self.app.show_project_tools(self.project)

    def confirm_close(self) -> bool:
        if self.worker_active:
            if self.app.app_state in {"intake_scanning", "intake_copying"} and self.cancel_event is not None:
                if messagebox.askyesno(APP_TITLE, "An Intake operation is active. Cancel it before closing?", parent=self.root):
                    self.cancel_event.set()
                return False
            messagebox.showinfo(APP_TITLE, "Please wait for the active Intake operation to finish before closing.", parent=self.root)
            return False
        if not self.undo_history:
            return True
        confirmed = messagebox.askyesno(
            APP_TITLE,
            "Leaving Intake will clear the Undo history. Sorted files will remain in their class folders. Close the app?",
            parent=self.root,
        )
        if confirmed:
            self.undo_history.clear()
        return confirmed
