"""Keyboard-driven, local-only JPEG sorting desktop application."""

from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from file_operations import MoveRecord, list_jpegs, move_jpeg, undo_move

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:  # Handled with a friendly dialog in main().
    Image = ImageOps = ImageTk = None  # type: ignore[assignment]


APP_TITLE = "JPEG Dataset Sorter"
APP_FOLDER = Path(__file__).resolve().parent
CONFIG_FILE = APP_FOLDER / "sorter_config.json"
VIEW_BACKGROUND = "#17191c"


class JPEGSorterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1200x850")
        self.root.minsize(780, 600)

        self.source_var = tk.StringVar()
        self.destination_vars = {str(key): tk.StringVar() for key in range(10)}
        self.name_vars = {str(key): tk.StringVar() for key in range(10)}
        self.status_var = tk.StringVar(value="Configure folders, then select Start.")
        self.filename_var = tk.StringVar(value="No image loaded")
        self.progress_var = tk.StringVar(value="")

        self.running = False
        self.current_path: Path | None = None
        self.current_pil_image = None
        self.display_photo = None
        self.last_move: MoveRecord | None = None
        self.completed_moves = 0
        self.session_total = 0
        self.resize_job: str | None = None

        self._configure_styles()
        self._build_ui()
        self._load_config()
        self.root.bind_all("<KeyPress>", self._handle_keypress, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Heading.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Key.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 7))
        style.configure("Start.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 9))

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.main = ttk.Frame(self.root, padding=12)
        self.main.grid(row=0, column=0, sticky="nsew")
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(1, weight=1)

        self.config_frame = ttk.LabelFrame(
            self.main, text=" Configuration ", padding=(16, 12)
        )
        self.config_frame.grid(row=0, column=0, sticky="new")
        self.config_frame.columnconfigure(0, weight=1)

        title = ttk.Label(
            self.config_frame,
            text="Sort JPEG images with the number keys",
            style="Title.TLabel",
        )
        title.grid(row=0, column=0, sticky="w", pady=(0, 10))

        source_frame = ttk.Frame(self.config_frame)
        source_frame.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        source_frame.columnconfigure(1, weight=1)
        ttk.Label(source_frame, text="Source folder:", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Entry(source_frame, textvariable=self.source_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8)
        )
        ttk.Button(source_frame, text="Browse...", command=self._choose_source).grid(
            row=0, column=2
        )

        ttk.Label(
            self.config_frame,
            text="Name each class and assign its destination folder (leave unused keys blank):",
            style="Heading.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(0, 7))

        mappings = ttk.Frame(self.config_frame)
        mappings.grid(row=3, column=0, sticky="ew")
        mappings.columnconfigure(2, weight=1)
        ttk.Label(mappings, text="Key", style="Heading.TLabel").grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Label(mappings, text="Class name", style="Heading.TLabel").grid(
            row=0, column=1, sticky="w", padx=(0, 8)
        )
        ttk.Label(mappings, text="Destination folder", style="Heading.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 8)
        )
        for key in range(10):
            self._build_mapping_row(mappings, str(key), key + 1)

        actions = ttk.Frame(self.config_frame)
        actions.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Label(
            actions,
            text="Files are moved locally. Destination files are never overwritten.",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            actions, text="Start", command=self._start, style="Start.TButton"
        ).grid(row=0, column=1, sticky="e")

        self.viewer = ttk.Frame(self.main)
        self.viewer.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.viewer.columnconfigure(0, weight=1)
        self.viewer.rowconfigure(1, weight=1)

        viewer_header = ttk.Frame(self.viewer)
        viewer_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        viewer_header.columnconfigure(0, weight=1)
        ttk.Label(
            viewer_header, textvariable=self.filename_var, style="Heading.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(viewer_header, textvariable=self.progress_var).grid(
            row=0, column=1, sticky="e"
        )

        self.image_panel = tk.Frame(
            self.viewer,
            background=VIEW_BACKGROUND,
            highlightthickness=1,
            highlightbackground="#4a4d52",
        )
        self.image_panel.grid(row=1, column=0, sticky="nsew")
        self.image_panel.columnconfigure(0, weight=1)
        self.image_panel.rowconfigure(0, weight=1)
        self.image_label = tk.Label(
            self.image_panel,
            text="Select a source folder and assign destination keys above.",
            foreground="#f2f2f2",
            background=VIEW_BACKGROUND,
            font=("Segoe UI", 14),
            justify="center",
        )
        self.image_label.grid(row=0, column=0, sticky="nsew")
        self.image_panel.bind("<Configure>", self._schedule_image_resize)

        controls = ttk.Frame(self.viewer)
        controls.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        controls.columnconfigure(0, weight=1)

        ttk.Label(controls, text="Number key legend:", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 3)
        )
        self.key_buttons = ttk.Frame(controls)
        self.key_buttons.grid(row=1, column=0, sticky="w")

        utility_buttons = ttk.Frame(controls)
        utility_buttons.grid(row=1, column=1, sticky="e")
        self.undo_button = ttk.Button(
            utility_buttons, text="Undo last move", command=self._undo, state="disabled"
        )
        self.undo_button.grid(row=0, column=0, padx=(0, 7))
        ttk.Button(
            utility_buttons,
            text="Edit configuration",
            command=self._edit_configuration,
        ).grid(row=0, column=1)

        ttk.Label(self.viewer, textvariable=self.status_var, anchor="w").grid(
            row=3, column=0, sticky="ew", pady=(7, 0)
        )
        self._refresh_key_buttons()

    def _build_mapping_row(self, parent: ttk.Frame, key: str, row: int) -> None:
        ttk.Label(parent, text=key, width=3, anchor="center", style="Heading.TLabel").grid(
            row=row, column=0, padx=(0, 8), pady=2
        )
        ttk.Entry(parent, textvariable=self.name_vars[key], width=22).grid(
            row=row, column=1, sticky="ew", padx=(0, 8), pady=2
        )
        ttk.Entry(parent, textvariable=self.destination_vars[key]).grid(
            row=row, column=2, sticky="ew", padx=(0, 8), pady=2
        )
        ttk.Button(
            parent,
            text="Browse...",
            command=lambda selected_key=key: self._choose_destination(selected_key),
        ).grid(row=row, column=3, padx=(0, 4), pady=2)
        ttk.Button(
            parent,
            text="Clear",
            command=lambda selected_key=key: self._clear_mapping(selected_key),
        ).grid(row=row, column=4, pady=2)

    def _clear_mapping(self, key: str) -> None:
        self.name_vars[key].set("")
        self.destination_vars[key].set("")

    def _choose_source(self) -> None:
        selected = filedialog.askdirectory(
            title="Select source folder", initialdir=self.source_var.get() or None
        )
        if selected:
            self.source_var.set(selected)

    def _choose_destination(self, key: str) -> None:
        selected = filedialog.askdirectory(
            title=f"Select destination folder for key {key}",
            initialdir=self.destination_vars[key].get() or None,
        )
        if selected:
            self.destination_vars[key].set(selected)

    def _mapping(self) -> dict[str, Path]:
        return {
            key: Path(variable.get().strip()).expanduser().resolve()
            for key, variable in self.destination_vars.items()
            if variable.get().strip()
        }

    def _validate_configuration(self) -> tuple[Path, dict[str, Path]] | None:
        source_text = self.source_var.get().strip()
        if not source_text:
            messagebox.showerror(APP_TITLE, "Select a source folder first.")
            return None
        source = Path(source_text).expanduser().resolve()
        if not source.is_dir():
            messagebox.showerror(APP_TITLE, f"Source folder does not exist:\n{source}")
            return None

        mapping = self._mapping()
        if not mapping:
            messagebox.showerror(
                APP_TITLE, "Assign at least one number key to a destination folder."
            )
            return None

        invalid = [(key, folder) for key, folder in mapping.items() if not folder.is_dir()]
        if invalid:
            key, folder = invalid[0]
            messagebox.showerror(
                APP_TITLE, f"Destination for key {key} does not exist:\n{folder}"
            )
            return None

        if source in mapping.values():
            messagebox.showerror(
                APP_TITLE, "The source folder cannot also be a destination folder."
            )
            return None

        normalized_destinations = [str(folder).casefold() for folder in mapping.values()]
        if len(normalized_destinations) != len(set(normalized_destinations)):
            messagebox.showerror(
                APP_TITLE, "Each number key must use a different destination folder."
            )
            return None
        return source, mapping

    def _start(self) -> None:
        validated = self._validate_configuration()
        if validated is None:
            return
        source, _ = validated
        images = list_jpegs(source)
        if not images:
            messagebox.showinfo(
                APP_TITLE, "The selected source folder contains no .jpg or .jpeg files."
            )
            return

        self._save_config()
        self.running = True
        self.completed_moves = 0
        self.session_total = len(images)
        self.last_move = None
        self.undo_button.configure(state="disabled")
        self.config_frame.grid_remove()
        self.viewer.grid_configure(row=0, pady=0)
        self.main.rowconfigure(0, weight=1)
        self.main.rowconfigure(1, weight=0)
        self._refresh_key_buttons()
        self._load_next_image()
        self.root.focus_set()

    def _edit_configuration(self) -> None:
        self.running = False
        self.current_path = None
        self.current_pil_image = None
        self.display_photo = None
        self.last_move = None
        self.undo_button.configure(state="disabled")
        self.config_frame.grid()
        self.viewer.grid_configure(row=1, pady=(10, 0))
        self.main.rowconfigure(0, weight=0)
        self.main.rowconfigure(1, weight=1)
        self.filename_var.set("No image loaded")
        self.progress_var.set("")
        self.status_var.set("Update the configuration, then select Start.")
        self.image_label.configure(
            image="",
            text="Select Start when the folder assignments are ready.",
        )
        self._refresh_key_buttons()

    def _load_next_image(self, preferred: Path | None = None) -> None:
        source_text = self.source_var.get().strip()
        source = Path(source_text).expanduser().resolve()
        remaining = list_jpegs(source)
        if preferred is not None and preferred in remaining:
            next_path = preferred
        elif remaining:
            next_path = remaining[0]
        else:
            self._finish_session()
            return

        self.current_path = next_path
        self.filename_var.set(next_path.name)
        self._update_progress(len(remaining))
        try:
            with Image.open(next_path) as opened:
                corrected = ImageOps.exif_transpose(opened)
                self.current_pil_image = corrected.convert("RGB").copy()
            self.status_var.set("Press an assigned number key to move this image.")
            self._render_current_image()
        except Exception as error:
            self.current_pil_image = None
            self.display_photo = None
            self.image_label.configure(
                image="",
                text=f"Unable to display this image.\n\n{next_path.name}\n\n{error}",
            )
            self.status_var.set(
                "The file can still be moved with a mapped key, or fix/remove it outside the app."
            )

    def _render_current_image(self) -> None:
        if self.current_pil_image is None:
            return
        width = max(self.image_panel.winfo_width() - 24, 100)
        height = max(self.image_panel.winfo_height() - 24, 100)
        resized = self.current_pil_image.copy()
        resized.thumbnail((width, height), Image.Resampling.LANCZOS)
        self.display_photo = ImageTk.PhotoImage(resized)
        self.image_label.configure(image=self.display_photo, text="")

    def _schedule_image_resize(self, _event: tk.Event) -> None:
        if not self.running or self.current_pil_image is None:
            return
        if self.resize_job is not None:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(100, self._finish_image_resize)

    def _finish_image_resize(self) -> None:
        self.resize_job = None
        self._render_current_image()

    def _handle_keypress(self, event: tk.Event) -> None:
        if not self.running:
            return
        key = event.char
        if key in self._mapping():
            self._classify(key)

    def _classify(self, key: str) -> None:
        if not self.running or self.current_path is None:
            return
        destination = self._mapping().get(key)
        if destination is None:
            return

        current = self.current_path
        self.running = False  # Prevent repeated key events during the disk operation.
        try:
            record = move_jpeg(current, destination)
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"Could not move the image:\n\n{error}")
            self.running = True
            return

        self.last_move = record
        self.completed_moves += 1
        self.undo_button.configure(state="normal")
        self.status_var.set(f"Moved {record.moved_path.name} to {destination}")
        self.current_path = None
        self.current_pil_image = None
        self.display_photo = None
        self.running = True
        self._load_next_image()

    def _undo(self) -> None:
        if self.last_move is None:
            return
        record = self.last_move
        try:
            restored = undo_move(record)
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"Could not undo the last move:\n\n{error}")
            return

        self.last_move = None
        self.completed_moves = max(0, self.completed_moves - 1)
        self.undo_button.configure(state="disabled")
        self.running = True
        self.status_var.set(f"Restored {restored.name} to the source folder.")
        self._refresh_key_buttons()
        self._load_next_image(preferred=restored)
        self.root.focus_set()

    def _finish_session(self) -> None:
        self.running = False
        self.current_path = None
        self.current_pil_image = None
        self.display_photo = None
        self.filename_var.set("All images sorted")
        self.progress_var.set(f"Moved: {self.completed_moves}")
        self.status_var.set("The source folder contains no more .jpg or .jpeg files.")
        self.image_label.configure(
            image="",
            text="Done!\n\nThe source folder contains no more JPEG images.",
        )
        self._refresh_key_buttons()

    def _update_progress(self, remaining: int) -> None:
        total = max(self.session_total, self.completed_moves + remaining)
        self.progress_var.set(
            f"Moved: {self.completed_moves}    Remaining: {remaining}    Total: {total}"
        )

    def _refresh_key_buttons(self) -> None:
        for child in self.key_buttons.winfo_children():
            child.destroy()
        mapping = self._mapping()
        for index, (key, folder) in enumerate(mapping.items()):
            class_name = self.name_vars[key].get().strip() or str(folder.name or folder)
            if len(class_name) > 22:
                class_name = f"{class_name[:19]}..."
            label = f"{key}:  {class_name}"
            button = ttk.Button(
                self.key_buttons,
                text=label,
                style="Key.TButton",
                command=lambda selected_key=key: self._classify(selected_key),
            )
            button.grid(row=index // 5, column=index % 5, padx=(0, 5), pady=2)
            if not self.running:
                button.configure(state="disabled")

    def _load_config(self) -> None:
        if not CONFIG_FILE.is_file():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self.source_var.set(str(data.get("source", "")))
            destinations = data.get("destinations", {})
            names = data.get("names", {})
            if not isinstance(destinations, dict):
                destinations = {}
            if not isinstance(names, dict):
                names = {}
            for key, variable in self.destination_vars.items():
                variable.set(str(destinations.get(key, "")))
                self.name_vars[key].set(str(names.get(key, "")))
            self._refresh_key_buttons()
        except (OSError, ValueError, TypeError):
            # A damaged preference file should never prevent the app from opening.
            pass

    def _save_config(self) -> None:
        data = {
            "source": self.source_var.get().strip(),
            "destinations": {
                key: variable.get().strip()
                for key, variable in self.destination_vars.items()
                if variable.get().strip()
            },
            "names": {
                key: self.name_vars[key].get().strip()
                for key, variable in self.destination_vars.items()
                if variable.get().strip() and self.name_vars[key].get().strip()
            },
        }
        try:
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as error:
            messagebox.showwarning(
                APP_TITLE,
                f"The configuration could not be saved, but sorting can continue:\n\n{error}",
            )

    def _close(self) -> None:
        if self.resize_job is not None:
            self.root.after_cancel(self.resize_job)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    if Image is None:
        root.withdraw()
        messagebox.showerror(
            APP_TITLE,
            "Pillow is required to display JPEG images.\n\n"
            "Open a command prompt in this folder and run:\n"
            "python -m pip install -r requirements.txt",
        )
        root.destroy()
        return
    JPEGSorterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
