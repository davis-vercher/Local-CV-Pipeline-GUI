"""Filesystem services for Outdoor Vision CV project and library management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4

from app_models import AppState, ProjectRecord, utc_now_iso
from app_storage import StateStore


LIBRARY_NAME = "outdoor_vision_CV"
IMAGE_SUFFIXES = {".jpg", ".jpeg"}
INVALID_NAME_CHARACTERS = set('<>:"/\\|?*')
RESERVED_BASE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ValidationError(ValueError):
    """A user-correctable validation failure."""


class UnsafePathError(RuntimeError):
    """A filesystem target failed a destructive-operation safety check."""


@dataclass(frozen=True)
class ScanResult:
    project_id: str
    total_images: int
    latest_modified: str
    missing: bool = False
    error: str | None = None


def windows_path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False)))).casefold()


def validate_project_name(
    name: str,
    *,
    library: Path,
    projects: list[ProjectRecord],
    exclude_project_id: str | None = None,
) -> str:
    """Validate a project name using Windows folder-name rules."""

    if not name:
        raise ValidationError("Enter a project name.")
    if len(name) > 25:
        raise ValidationError("Project names must be 25 characters or fewer.")
    if name != name.strip():
        raise ValidationError("Project names cannot begin or end with spaces.")
    if name.endswith("."):
        raise ValidationError("Project names cannot end with a period.")
    invalid = sorted(character for character in set(name) if character in INVALID_NAME_CHARACTERS)
    if invalid:
        raise ValidationError(
            "Project names cannot contain these characters: " + " ".join(invalid)
        )
    if any(ord(character) < 32 for character in name):
        raise ValidationError("Project names cannot contain control characters.")
    base_name = name.split(".", 1)[0].upper()
    if base_name in RESERVED_BASE_NAMES:
        raise ValidationError(f"'{name}' is a reserved Windows folder name.")

    folded_name = name.casefold()
    for project in projects:
        if project.project_id != exclude_project_id and project.name.casefold() == folded_name:
            raise ValidationError("A project with this name already exists.")

    target = library / name
    if target.exists():
        current = next(
            (
                project
                for project in projects
                if project.project_id == exclude_project_id
            ),
            None,
        )
        if current is None or windows_path_key(target) != windows_path_key(current.folder):
            raise ValidationError("A file or folder with this name already exists in the library.")
    return name


def create_library(parent: Path) -> Path:
    parent = parent.expanduser().resolve()
    if not parent.is_dir():
        raise ValidationError(f"The selected parent folder does not exist:\n{parent}")
    target = parent / LIBRARY_NAME
    if target.exists():
        raise ValidationError(
            f"This location already contains {LIBRARY_NAME}. Choose another location."
        )
    try:
        target.mkdir()
    except OSError as error:
        raise OSError(f"Could not create the project library at:\n{target}\n\n{error}") from error
    return target.resolve()


def recover_library(parent: Path) -> Path:
    """Return the configured library, recreating it when it was deleted externally."""

    parent = parent.expanduser().resolve()
    if not parent.is_dir():
        raise ValidationError(f"The selected parent folder does not exist:\n{parent}")
    target = parent / LIBRARY_NAME
    if target.exists() and not target.is_dir():
        raise ValidationError(
            f"A file named {LIBRARY_NAME} already exists at the selected location."
        )
    if not target.exists():
        try:
            target.mkdir()
        except OSError as error:
            raise OSError(
                f"Could not recreate the project library at:\n{target}\n\n{error}"
            ) from error
    return target.resolve()


def create_project(state: AppState, store: StateStore, name: str) -> ProjectRecord:
    library = Path(state.library_path).resolve()
    if not library.is_dir():
        raise ValidationError(f"The project library is unavailable:\n{library}")
    valid_name = validate_project_name(name, library=library, projects=state.projects)
    target = library / valid_name
    target.mkdir()
    project = ProjectRecord(name=valid_name, path=str(target.resolve()))
    state.projects.append(project)
    try:
        store.save(state)
    except Exception:
        try:
            target.rmdir()
        except OSError as rollback_error:
            raise RuntimeError(
                "The project registry could not be saved and the empty project folder "
                f"could not be removed:\n{target}\n\n{rollback_error}"
            )
        state.projects.remove(project)
        raise
    return project


def _temporary_case_name(folder: Path) -> Path:
    while True:
        candidate = folder.parent / f".__ov_rename_{uuid4().hex}"
        if not candidate.exists():
            return candidate


def rename_project(
    state: AppState, store: StateStore, project: ProjectRecord, new_name: str
) -> ProjectRecord:
    library = Path(state.library_path).resolve()
    old_path = project.folder.resolve()
    if not old_path.is_dir():
        raise FileNotFoundError(f"The project folder is missing:\n{old_path}")
    valid_name = validate_project_name(
        new_name,
        library=library,
        projects=state.projects,
        exclude_project_id=project.project_id,
    )
    if valid_name == project.name:
        return project
    new_path = library / valid_name
    old_name, old_registry_path, old_edited = project.name, project.path, project.last_edited
    intermediate: Path | None = None
    try:
        if valid_name.casefold() == project.name.casefold():
            intermediate = _temporary_case_name(old_path)
            old_path.rename(intermediate)
            intermediate.rename(new_path)
        else:
            old_path.rename(new_path)
        project.name = valid_name
        project.path = str(new_path.resolve())
        project.last_edited = utc_now_iso()
        store.save(state)
    except Exception:
        project.name, project.path, project.last_edited = old_name, old_registry_path, old_edited
        if new_path.exists() and not old_path.exists():
            try:
                new_path.rename(old_path)
            except OSError:
                pass
        elif intermediate is not None and intermediate.exists() and not old_path.exists():
            try:
                intermediate.rename(old_path)
            except OSError:
                pass
        raise
    return project


def _assert_safe_project_target(state: AppState, project: ProjectRecord) -> Path:
    library = Path(state.library_path).resolve()
    target = project.folder.resolve()
    if not library.is_dir():
        raise UnsafePathError("The configured project library is unavailable.")
    if target.parent != library:
        raise UnsafePathError("The project is not a direct child of the configured library.")
    if target == library:
        raise UnsafePathError("The master project library cannot be deleted as a project.")
    if target.name.casefold() != project.name.casefold():
        raise UnsafePathError("The registered project name and folder name do not match.")
    if windows_path_key(target) != windows_path_key(project.folder):
        raise UnsafePathError("The registered project path could not be verified.")
    return target


def delete_project(state: AppState, store: StateStore, project: ProjectRecord) -> None:
    target = _assert_safe_project_target(state, project)
    if not target.is_dir():
        raise FileNotFoundError(f"The project folder is missing:\n{target}")
    shutil.rmtree(target)
    if target.exists():
        raise OSError(f"The project folder still exists after deletion:\n{target}")
    state.projects = [item for item in state.projects if item.project_id != project.project_id]
    store.save(state)


def remove_stale_project(state: AppState, store: StateStore, project: ProjectRecord) -> None:
    if project.folder.exists():
        raise ValidationError("The project folder exists; it is not a stale registry entry.")
    state.projects = [item for item in state.projects if item.project_id != project.project_id]
    store.save(state)


def _is_directory_link(path: Path, entry: os.DirEntry[str] | None = None) -> bool:
    if entry is not None and entry.is_symlink():
        return True
    if path.is_symlink():
        return True
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def scan_project(project: ProjectRecord) -> ScanResult:
    root = project.folder
    if not root.is_dir():
        return ScanResult(project.project_id, project.total_images, project.last_edited, missing=True)
    total = 0
    latest = root.stat().st_mtime
    pending = [root]
    try:
        while pending:
            folder = pending.pop()
            with os.scandir(folder) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    try:
                        info = entry.stat(follow_symlinks=False)
                        latest = max(latest, info.st_mtime)
                        if entry.is_dir(follow_symlinks=False):
                            if not _is_directory_link(path, entry):
                                pending.append(path)
                        elif entry.is_file(follow_symlinks=False) and path.suffix.casefold() in IMAGE_SUFFIXES:
                            total += 1
                    except OSError as error:
                        return ScanResult(
                            project.project_id,
                            total,
                            project.last_edited,
                            error=f"{path}: {error}",
                        )
        latest_iso = datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()
        if _parse_timestamp(project.last_edited) > _parse_timestamp(latest_iso):
            latest_iso = project.last_edited
        return ScanResult(project.project_id, total, latest_iso)
    except OSError as error:
        return ScanResult(
            project.project_id,
            total,
            project.last_edited,
            error=f"{root}: {error}",
        )


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def apply_scan_results(
    state: AppState, store: StateStore, results: list[ScanResult]
) -> list[str]:
    failures: list[str] = []
    by_id = {project.project_id: project for project in state.projects}
    for result in results:
        project = by_id.get(result.project_id)
        if project is None:
            continue
        if result.error:
            failures.append(f"{project.name}: {result.error}")
            continue
        if not result.missing:
            project.total_images = result.total_images
            project.last_edited = result.latest_modified
    store.save(state)
    return failures


def _copy_tree_verified(source: Path, staging: Path) -> None:
    def ignore_reparse_points(folder: str, names: list[str]) -> list[str]:
        return [
            name
            for name in names
            if _is_directory_link(Path(folder) / name)
        ]

    shutil.copytree(
        source,
        staging,
        symlinks=True,
        ignore=ignore_reparse_points,
    )
    source_files: dict[str, int] = {}
    copied_files: dict[str, int] = {}
    for root, directories, files in os.walk(source, followlinks=False):
        directories[:] = [
            name for name in directories if not _is_directory_link(Path(root) / name)
        ]
        for filename in files:
            path = Path(root) / filename
            if not path.is_symlink():
                source_files[str(path.relative_to(source))] = path.stat().st_size
    for root, directories, files in os.walk(staging, followlinks=False):
        directories[:] = [
            name for name in directories if not _is_directory_link(Path(root) / name)
        ]
        for filename in files:
            path = Path(root) / filename
            if not path.is_symlink():
                copied_files[str(path.relative_to(staging))] = path.stat().st_size
    if source_files != copied_files:
        raise OSError("The copied library failed file-count or file-size verification.")


def move_library(state: AppState, store: StateStore, new_parent: Path) -> Path:
    source = Path(state.library_path).resolve()
    new_parent = new_parent.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"The current project library is missing:\n{source}")
    if not new_parent.is_dir():
        raise ValidationError(f"The selected parent folder does not exist:\n{new_parent}")
    destination = new_parent / LIBRARY_NAME
    if windows_path_key(source) == windows_path_key(destination):
        raise ValidationError("The new project-library location is unchanged.")
    if destination.exists():
        raise ValidationError(
            f"The destination already contains {LIBRARY_NAME}; libraries cannot be merged."
        )

    old_library = state.library_path
    old_paths = {project.project_id: project.path for project in state.projects}
    staging = new_parent / f".{LIBRARY_NAME}.moving-{uuid4().hex}"
    moved = False
    try:
        same_volume = source.stat().st_dev == new_parent.stat().st_dev
        if same_volume:
            source.rename(destination)
        else:
            _copy_tree_verified(source, staging)
            staging.rename(destination)
            shutil.rmtree(source)
        moved = True
        state.library_path = str(destination.resolve())
        for project in state.projects:
            project.path = str((destination / project.name).resolve())
        store.save(state)
    except Exception:
        state.library_path = old_library
        for project in state.projects:
            project.path = old_paths[project.project_id]
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if moved and destination.exists() and not source.exists():
            try:
                destination.rename(source)
            except OSError:
                pass
        raise
    return destination
