"""Filesystem, persistence, and queue services for the Intake project tool.

This module deliberately contains no Tkinter code so the safety-critical Intake
behavior can be exercised without launching the desktop application.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
from typing import Callable, Iterable
from uuid import uuid4

from PIL import Image

from app_storage import default_app_data_dir
from project_services import INVALID_NAME_CHARACTERS, RESERVED_BASE_NAMES, windows_path_key


JPEG_SUFFIXES = {".jpg", ".jpeg"}
INTAKE_FOLDER = "intake"
UNSORTED_FOLDER = "unsorted"
SORTED_FOLDER = "sorted"
MANIFEST_SCHEMA_VERSION = 1


class IntakeError(RuntimeError):
    """Base exception for Intake operations."""


class ManifestError(IntakeError):
    """The private queue manifest cannot be trusted."""


class MappingError(IntakeError):
    """The private class mapping cannot be trusted."""


class IntakeValidationError(ValueError):
    """A user-correctable Intake validation problem."""


class ImportSystemError(IntakeError):
    """A destination, storage, access, or persistence failure stopped import."""


class SourceReadError(OSError):
    """A candidate became unreadable after preview."""


class CopyCancelled(IntakeError):
    """Cancellation interrupted the current file before it was finalized."""


class StaleQueueItemError(IntakeError):
    """A controller attempted to change an item already changed elsewhere."""


@dataclass
class QueueItem:
    current_name: str
    batch_id: str
    batch_timestamp: str
    source_relative_path: str
    queue_order: int

    @classmethod
    def from_dict(cls, value: dict) -> "QueueItem":
        return cls(
            current_name=str(value["current_name"]),
            batch_id=str(value["batch_id"]),
            batch_timestamp=str(value["batch_timestamp"]),
            source_relative_path=str(value["source_relative_path"]),
            queue_order=int(value["queue_order"]),
        )


@dataclass
class IntakeManifest:
    project_id: str
    items: list[QueueItem] = field(default_factory=list)
    schema_version: int = MANIFEST_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: dict, expected_project_id: str) -> "IntakeManifest":
        if not isinstance(value, dict) or not isinstance(value.get("items", []), list):
            raise ValueError("The manifest root or items collection is invalid.")
        project_id = str(value.get("project_id", ""))
        if project_id != expected_project_id:
            raise ValueError("The manifest belongs to a different project.")
        return cls(
            project_id=project_id,
            items=[QueueItem.from_dict(item) for item in value.get("items", [])],
            schema_version=int(value.get("schema_version", MANIFEST_SCHEMA_VERSION)),
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "items": [asdict(item) for item in self.items],
        }


@dataclass(frozen=True)
class ImportCandidate:
    source_path: Path
    source_relative_path: str
    size: int
    preview_name: str
    collision_renamed: bool = False


@dataclass
class DiscoveryResult:
    source: Path
    destination: Path
    valid: list[ImportCandidate] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    hidden_or_system_skipped: int = 0
    cancelled: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.valid)

    @property
    def collision_count(self) -> int:
        return sum(1 for item in self.valid if item.collision_renamed)


@dataclass
class ImportResult:
    valid_discovered: int
    copied: int = 0
    invalid: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    bytes_copied: int = 0
    destination: str = ""
    cancelled: bool = False
    systemic_error: str | None = None


@dataclass(frozen=True)
class ClassificationRecord:
    item: QueueItem
    moved_path: Path
    original_unsorted_name: str
    class_name: str


@dataclass(frozen=True)
class ReviewClass:
    name: str
    direct_jpegs: int
    path: Path
    nested_folder_count: int


@dataclass(frozen=True)
class ReviewResult:
    classes: list[ReviewClass]

    @property
    def total_jpegs(self) -> int:
        return sum(item.direct_jpegs for item in self.classes)

    @property
    def nested_folder_count(self) -> int:
        return sum(item.nested_folder_count for item in self.classes)


ProgressCallback = Callable[[int, int, int, int], None]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_intake_structure(project_folder: Path) -> tuple[Path, Path]:
    root = project_folder.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"The project folder is unavailable:\n{root}")
    intake = root / INTAKE_FOLDER
    unsorted = intake / UNSORTED_FOLDER
    sorted_folder = intake / SORTED_FOLDER
    unsorted.mkdir(parents=True, exist_ok=True)
    sorted_folder.mkdir(parents=True, exist_ok=True)
    if not unsorted.is_dir() or not sorted_folder.is_dir():
        raise IntakeError("The required Intake folders could not be created.")
    return unsorted, sorted_folder


def _atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.stem}_",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(value, temporary, indent=2, ensure_ascii=False)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


class IntakeStore:
    """Atomic per-project private persistence keyed only by stable project ID."""

    _locks_guard = threading.Lock()
    _project_locks: dict[str, threading.RLock] = {}

    def __init__(self, project_id: str, app_data_dir: Path | None = None) -> None:
        self.project_id = project_id
        self.folder = (app_data_dir or default_app_data_dir()).resolve() / "intake" / project_id
        self.manifest_path = self.folder / "manifest.json"
        self.mapping_path = self.folder / "mappings.json"
        lock_key = os.path.normcase(str(self.manifest_path)).casefold()
        with self._locks_guard:
            self.operation_lock = self._project_locks.setdefault(lock_key, threading.RLock())

    def load_manifest(self) -> IntakeManifest:
        if not self.manifest_path.is_file():
            return IntakeManifest(project_id=self.project_id)
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return IntakeManifest.from_dict(value, self.project_id)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise ManifestError(
                f"The Intake queue manifest could not be read:\n{self.manifest_path}\n\n{error}"
            ) from error

    def save_manifest(self, manifest: IntakeManifest) -> None:
        if manifest.project_id != self.project_id:
            raise ManifestError("Refusing to save a manifest for a different project.")
        _atomic_json_write(self.manifest_path, manifest.to_dict())

    def load_mappings(self) -> dict[str, str]:
        if not self.mapping_path.is_file():
            return {}
        try:
            value = json.loads(self.mapping_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("project_id") != self.project_id:
                raise ValueError("The mapping belongs to a different project.")
            mappings = value.get("mappings", {})
            if not isinstance(mappings, dict):
                raise ValueError("The mappings value must be an object.")
            result = {str(key): str(name) for key, name in mappings.items()}
            if any(key not in set("0123456789") for key in result):
                raise ValueError("Only number keys 0 through 9 may be mapped.")
            if len({name.casefold() for name in result.values()}) != len(result):
                raise ValueError("A class is assigned to more than one key.")
            return result
        except (OSError, ValueError, TypeError) as error:
            raise MappingError(
                f"The Intake class mapping could not be read:\n{self.mapping_path}\n\n{error}"
            ) from error

    def save_mappings(self, mappings: dict[str, str]) -> None:
        if any(key not in set("0123456789") for key in mappings):
            raise MappingError("Only number keys 0 through 9 may be mapped.")
        if len({name.casefold() for name in mappings.values()}) != len(mappings):
            raise MappingError("Each class can be assigned to only one key.")
        _atomic_json_write(
            self.mapping_path,
            {"schema_version": 1, "project_id": self.project_id, "mappings": mappings},
        )


def validate_class_name(name: str, sorted_folder: Path, exclude_name: str | None = None) -> str:
    if not name:
        raise IntakeValidationError("Enter a class name.")
    if len(name) > 25:
        raise IntakeValidationError("Class names must be 25 characters or fewer.")
    if name != name.strip():
        raise IntakeValidationError("Class names cannot begin or end with spaces.")
    if name.endswith("."):
        raise IntakeValidationError("Class names cannot end with a period.")
    invalid = sorted(character for character in set(name) if character in INVALID_NAME_CHARACTERS)
    if invalid:
        raise IntakeValidationError("Class names cannot contain these characters: " + " ".join(invalid))
    if any(ord(character) < 32 for character in name):
        raise IntakeValidationError("Class names cannot contain control characters.")
    if name.split(".", 1)[0].upper() in RESERVED_BASE_NAMES:
        raise IntakeValidationError(f"'{name}' is a reserved Windows folder name.")
    folded = name.casefold()
    excluded = exclude_name.casefold() if exclude_name else None
    if any(entry.name.casefold() == folded and entry.name.casefold() != excluded for entry in sorted_folder.iterdir()):
        raise IntakeValidationError("A file or folder with this class name already exists.")
    return name


def discover_classes(sorted_folder: Path) -> list[str]:
    return sorted(
        (item.name for item in sorted_folder.iterdir() if item.is_dir() and not _is_reparse(item)),
        key=str.casefold,
    )


def create_class(sorted_folder: Path, name: str) -> Path:
    valid = validate_class_name(name, sorted_folder)
    target = sorted_folder / valid
    # Validate again immediately before changing the filesystem.
    validate_class_name(valid, sorted_folder)
    target.mkdir()
    return target


def rename_class(sorted_folder: Path, old_name: str, new_name: str) -> Path:
    source = _safe_direct_class(sorted_folder, old_name)
    valid = validate_class_name(new_name, sorted_folder, exclude_name=old_name)
    target = sorted_folder / valid
    if valid == old_name:
        return source
    if valid.casefold() == old_name.casefold():
        temporary = sorted_folder / f".__ov_class_{uuid4().hex}"
        source.rename(temporary)
        try:
            temporary.rename(target)
        except Exception:
            temporary.rename(source)
            raise
    else:
        source.rename(target)
    return target


def class_contents(class_folder: Path) -> tuple[int, int]:
    direct_jpegs = 0
    total_files = 0
    for root, directories, files in os.walk(class_folder, followlinks=False):
        directories[:] = [name for name in directories if not _is_reparse(Path(root) / name)]
        total_files += len(files)
        if Path(root) == class_folder:
            direct_jpegs += sum(1 for name in files if Path(name).suffix.casefold() in JPEG_SUFFIXES)
    return direct_jpegs, total_files


def recycle_class(sorted_folder: Path, class_name: str) -> None:
    target = _safe_direct_class(sorted_folder, class_name)
    try:
        from send2trash import send2trash
    except ImportError as error:
        raise IntakeError("Send2Trash is required to use the Windows Recycle Bin.") from error
    send2trash(str(target))
    if target.exists():
        raise IntakeError(f"The class folder still exists after recycling:\n{target}")


def _safe_direct_class(sorted_folder: Path, class_name: str) -> Path:
    root = sorted_folder.resolve()
    target = (root / class_name).resolve()
    if target.parent != root or windows_path_key(target) == windows_path_key(root):
        raise IntakeError("The class folder target is unsafe.")
    if not target.is_dir() or _is_reparse(target):
        raise FileNotFoundError(f"The class folder is missing or inaccessible:\n{target}")
    return target


def _is_reparse(path: Path, entry: os.DirEntry[str] | None = None) -> bool:
    if (entry is not None and entry.is_symlink()) or path.is_symlink():
        return True
    try:
        info = entry.stat(follow_symlinks=False) if entry else path.stat(follow_symlinks=False)
        return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True


def _is_hidden_or_system(path: Path, entry: os.DirEntry[str] | None = None) -> bool:
    if path.name.startswith("."):
        return True
    try:
        info = entry.stat(follow_symlinks=False) if entry else path.stat(follow_symlinks=False)
        attributes = getattr(info, "st_file_attributes", 0)
        return bool(attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
    except (AttributeError, OSError):
        return False


def verify_jpeg(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            if image.format != "JPEG":
                return False
            image.verify()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def available_name(folder: Path, filename: str, reserved: Iterable[str] = ()) -> str:
    occupied = {item.name.casefold() for item in folder.iterdir()}
    occupied.update(name.casefold() for name in reserved)
    candidate = filename
    if candidate.casefold() not in occupied:
        return candidate
    path = Path(filename)
    number = 1
    while True:
        candidate = f"{path.stem}_{number}{path.suffix}"
        if candidate.casefold() not in occupied:
            return candidate
        number += 1


def require_destination_space(destination: Path, required_bytes: int) -> int:
    """Return available bytes or raise with the approved required/available detail."""

    available = shutil.disk_usage(destination).free
    if available < required_bytes:
        raise IntakeValidationError(
            "Destination space is insufficient. "
            f"Required: {required_bytes:,} bytes. Available: {available:,} bytes."
        )
    return available


def _copy_file_exclusive(
    source: Path,
    destination_folder: Path,
    preferred_name: str,
    cancel: threading.Event | None,
    byte_progress: Callable[[int], None] | None,
) -> tuple[Path, int]:
    """Copy without overwrite, reporting bytes and removing incomplete output."""

    try:
        source_stream = source.open("rb")
    except OSError as error:
        raise SourceReadError(str(error)) from error
    destination: Path | None = None
    copied = 0
    try:
        while True:
            name = available_name(destination_folder, preferred_name)
            destination = destination_folder / name
            try:
                destination_stream = destination.open("xb")
                break
            except FileExistsError:
                continue
        try:
            with destination_stream:
                while True:
                    if cancel and cancel.is_set():
                        raise CopyCancelled("Import cancelled.")
                    try:
                        chunk = source_stream.read(1024 * 1024)
                    except OSError as error:
                        raise SourceReadError(str(error)) from error
                    if not chunk:
                        break
                    destination_stream.write(chunk)
                    copied += len(chunk)
                    if byte_progress:
                        byte_progress(copied)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    finally:
        source_stream.close()
    try:
        shutil.copystat(source, destination)
    except OSError:
        # Metadata preservation is best effort where Windows permits it. The
        # byte-for-byte file has already been copied successfully.
        pass
    return destination, copied


def discover_source(
    source: Path,
    destination: Path,
    cancel: threading.Event | None = None,
    progress: Callable[[int], None] | None = None,
) -> DiscoveryResult:
    source = source.expanduser().resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise IntakeValidationError(f"The selected source folder does not exist:\n{source}")
    if source == destination or source in destination.parents or destination in source.parents:
        raise IntakeValidationError("The source folder must be external to intake/unsorted.")
    if os.name == "nt":
        if str(source).startswith("\\\\"):
            raise IntakeValidationError("Select a local Windows folder. UNC folders are not supported.")
        try:
            import ctypes

            drive_type = ctypes.windll.kernel32.GetDriveTypeW(source.anchor)  # type: ignore[attr-defined]
            if drive_type != 3:  # DRIVE_FIXED
                raise IntakeValidationError("Select a folder on a fixed local Windows drive.")
        except AttributeError:
            pass
    result = DiscoveryResult(source=source, destination=destination)
    raw_candidates: list[tuple[Path, str]] = []
    pending = [source]
    examined = 0
    while pending:
        if cancel and cancel.is_set():
            result.cancelled = True
            return result
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if cancel and cancel.is_set():
                        result.cancelled = True
                        return result
                    examined += 1
                    path = Path(entry.path)
                    if _is_hidden_or_system(path, entry):
                        result.hidden_or_system_skipped += 1
                    elif _is_reparse(path, entry):
                        result.hidden_or_system_skipped += 1
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False) and path.suffix.casefold() in JPEG_SUFFIXES:
                        raw_candidates.append((path, str(path.relative_to(source))))
                    if progress and examined % 25 == 0:
                        progress(examined)
        except OSError as error:
            relative = str(folder.relative_to(source)) if folder != source else "."
            result.invalid.append(f"{relative}: {error}")

    reserved: list[str] = []
    for path, relative in sorted(raw_candidates, key=lambda item: item[1].casefold()):
        if cancel and cancel.is_set():
            result.cancelled = True
            return result
        if not verify_jpeg(path):
            result.invalid.append(relative)
            continue
        try:
            size = path.stat().st_size
        except OSError:
            result.invalid.append(relative)
            continue
        final_name = available_name(destination, path.name, reserved)
        reserved.append(final_name)
        result.valid.append(
            ImportCandidate(path, relative, size, final_name, final_name != path.name)
        )
    result.valid.sort(key=lambda item: item.preview_name.casefold())
    if progress:
        progress(examined)
    return result


def copy_discovery(
    discovery: DiscoveryResult,
    store: IntakeStore,
    manifest: IntakeManifest,
    cancel: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> ImportResult:
    result = ImportResult(
        valid_discovered=len(discovery.valid),
        invalid=list(discovery.invalid),
        destination=str(discovery.destination),
    )
    batch_id = str(uuid4())
    batch_timestamp = utc_now_iso()
    next_order = max((item.queue_order for item in manifest.items), default=-1) + 1
    total_bytes = discovery.total_bytes
    last_progress = 0.0
    for index, candidate in enumerate(discovery.valid):
        if cancel and cancel.is_set():
            result.cancelled = True
            break
        def report_bytes(current_file_bytes: int) -> None:
            nonlocal last_progress
            now = time.monotonic()
            if progress and (now - last_progress >= 1.0 or current_file_bytes == candidate.size):
                progress(
                    index,
                    len(discovery.valid),
                    result.bytes_copied + current_file_bytes,
                    total_bytes,
                )
                last_progress = now

        try:
            destination, copied_bytes = _copy_file_exclusive(
                candidate.source_path,
                discovery.destination,
                candidate.source_path.name,
                cancel,
                report_bytes,
            )
        except CopyCancelled:
            result.cancelled = True
            break
        except SourceReadError as error:
            result.failed.append(f"{candidate.source_relative_path}: {error}")
            continue
        except OSError as error:
            result.systemic_error = f"Copying to {discovery.destination} failed: {error}"
            break
        final_name = destination.name
        result.copied += 1
        result.bytes_copied += copied_bytes
        if final_name != candidate.source_path.name:
            result.renamed.append(f"{candidate.source_relative_path} -> {final_name}")
        item = QueueItem(
            current_name=final_name,
            batch_id=batch_id,
            batch_timestamp=batch_timestamp,
            source_relative_path=candidate.source_relative_path,
            queue_order=next_order,
        )
        manifest.items.append(item)
        try:
            store.save_manifest(manifest)
        except OSError as error:
            manifest.items.pop()
            result.systemic_error = (
                f"The copied file could not be registered in private Intake state: {error}"
            )
            break
        next_order += 1
        if progress:
            progress(index + 1, len(discovery.valid), result.bytes_copied, total_bytes)
            last_progress = time.monotonic()
    return result


def active_queue(manifest: IntakeManifest, unsorted: Path) -> list[QueueItem]:
    return sorted(
        (item for item in manifest.items if (unsorted / item.current_name).is_file()),
        key=lambda item: item.queue_order,
    )


def repair_manifest(store: IntakeStore, unsorted: Path) -> tuple[IntakeManifest, list[str]]:
    """Replace a damaged manifest with one validated recovered batch.

    Per the approved product interpretation, only direct, content-valid JPEGs
    are registered. Invalid direct JPEGs are returned for disclosure.
    """

    valid: list[Path] = []
    omitted: list[str] = []
    for path in sorted(unsorted.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.suffix.casefold() not in JPEG_SUFFIXES:
            continue
        if verify_jpeg(path):
            valid.append(path)
        else:
            omitted.append(path.name)
    batch_id = str(uuid4())
    timestamp = utc_now_iso()
    manifest = IntakeManifest(
        project_id=store.project_id,
        items=[
            QueueItem(path.name, batch_id, timestamp, "Recovered: source path unavailable", index)
            for index, path in enumerate(valid)
        ],
    )
    store.save_manifest(manifest)
    return manifest, omitted


def review_dataset(sorted_folder: Path, mappings: dict[str, str]) -> ReviewResult:
    """Count the physical master dataset without descending into class folders."""

    classes = discover_classes(sorted_folder)
    mapped_order = [
        name
        for key in "0123456789"
        for name in [mappings.get(key)]
        if name is not None and name in classes
    ]
    mapped_keys = {name.casefold() for name in mapped_order}
    ordered = mapped_order + [name for name in classes if name.casefold() not in mapped_keys]
    results: list[ReviewClass] = []
    for name in ordered:
        folder = sorted_folder / name
        direct_jpegs = 0
        nested = 0
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    nested += 1
                elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.casefold() in JPEG_SUFFIXES:
                    direct_jpegs += 1
        results.append(ReviewClass(name, direct_jpegs, folder, nested))
    return ReviewResult(results)


def _renumber(manifest: IntakeManifest) -> None:
    manifest.items.sort(key=lambda item: item.queue_order)
    for index, item in enumerate(manifest.items):
        item.queue_order = index


def _item_identity(item: QueueItem) -> tuple[str, str, str]:
    return item.batch_id, item.current_name.casefold(), item.source_relative_path.casefold()


def _latest_item(manifest: IntakeManifest, requested: QueueItem) -> QueueItem:
    identity = _item_identity(requested)
    item = next((entry for entry in manifest.items if _item_identity(entry) == identity), None)
    if item is None:
        raise StaleQueueItemError(
            f"The queued image '{requested.current_name}' was already changed by another Intake operation."
        )
    return item


def _sync_manifest(target: IntakeManifest, source: IntakeManifest) -> None:
    target.schema_version = source.schema_version
    target.project_id = source.project_id
    target.items = source.items


def skip_item(store: IntakeStore, manifest: IntakeManifest, item: QueueItem) -> None:
    with store.operation_lock:
        latest = store.load_manifest()
        live_item = _latest_item(latest, item)
        maximum = max((entry.queue_order for entry in latest.items), default=0)
        live_item.queue_order = maximum + 1
        _renumber(latest)
        store.save_manifest(latest)
        _sync_manifest(manifest, latest)


def classify_item(
    store: IntakeStore,
    manifest: IntakeManifest,
    unsorted: Path,
    sorted_folder: Path,
    item: QueueItem,
    class_name: str,
) -> ClassificationRecord:
    with store.operation_lock:
        latest = store.load_manifest()
        live_item = _latest_item(latest, item)
        source = (unsorted / live_item.current_name).resolve()
        if source.parent != unsorted.resolve() or not source.is_file():
            raise StaleQueueItemError(
                f"The queued image '{live_item.current_name}' is no longer in intake/unsorted."
            )
        destination_folder = _safe_direct_class(sorted_folder, class_name)
        destination = destination_folder / available_name(destination_folder, source.name)
        source.rename(destination)
        latest.items = [entry for entry in latest.items if entry is not live_item]
        _renumber(latest)
        try:
            store.save_manifest(latest)
        except Exception:
            destination.rename(source)
            raise
        _sync_manifest(manifest, latest)
        return ClassificationRecord(live_item, destination, source.name, class_name)


def undo_classification(
    store: IntakeStore,
    manifest: IntakeManifest,
    unsorted: Path,
    record: ClassificationRecord,
) -> tuple[QueueItem, bool]:
    with store.operation_lock:
        latest = store.load_manifest()
        if not record.moved_path.is_file():
            raise StaleQueueItemError(
                f"The sorted image '{record.moved_path.name}' was already changed by another Intake operation."
            )
        restored_name = available_name(unsorted, record.original_unsorted_name)
        restored = unsorted / restored_name
        record.moved_path.rename(restored)
        item = record.item
        old_name = item.current_name
        item.current_name = restored_name
        for entry in latest.items:
            entry.queue_order += 1
        item.queue_order = 0
        latest.items.append(item)
        try:
            store.save_manifest(latest)
        except Exception:
            item.current_name = old_name
            restored.rename(record.moved_path)
            raise
        _sync_manifest(manifest, latest)
        return item, restored_name != record.original_unsorted_name
