"""Local file operations for the JPEG sorting GUI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil


JPEG_SUFFIXES = {".jpg", ".jpeg"}


@dataclass(frozen=True)
class MoveRecord:
    """Everything needed to undo one completed move."""

    original_path: Path
    moved_path: Path


def list_jpegs(folder: Path) -> list[Path]:
    """Return JPEG files directly inside *folder*, sorted by filename."""

    if not folder.is_dir():
        return []
    return sorted(
        (
            item
            for item in folder.iterdir()
            if item.is_file() and item.suffix.lower() in JPEG_SUFFIXES
        ),
        key=lambda item: item.name.casefold(),
    )


def available_destination(destination_folder: Path, filename: str) -> Path:
    """Choose a destination path without overwriting an existing file."""

    candidate = destination_folder / filename
    if not candidate.exists():
        return candidate

    source_name = Path(filename)
    counter = 1
    while True:
        candidate = destination_folder / (
            f"{source_name.stem}_{counter}{source_name.suffix}"
        )
        if not candidate.exists():
            return candidate
        counter += 1


def move_jpeg(source_file: Path, destination_folder: Path) -> MoveRecord:
    """Move one file and return the record required to undo it."""

    if not source_file.is_file():
        raise FileNotFoundError(f"Source image no longer exists: {source_file}")
    if not destination_folder.is_dir():
        raise NotADirectoryError(
            f"Destination folder does not exist: {destination_folder}"
        )

    destination = available_destination(destination_folder, source_file.name)
    shutil.move(str(source_file), str(destination))
    return MoveRecord(original_path=source_file, moved_path=destination)


def undo_move(record: MoveRecord) -> Path:
    """Restore a moved file to its original path."""

    if not record.moved_path.is_file():
        raise FileNotFoundError(
            f"The moved image can no longer be found: {record.moved_path}"
        )
    if record.original_path.exists():
        raise FileExistsError(
            f"Cannot undo because this path already exists: {record.original_path}"
        )

    record.original_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(record.moved_path), str(record.original_path))
    return record.original_path
