"""Atomic private state persistence for the Outdoor Vision CV desktop app."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from app_models import AppState, ProjectRecord


APP_DATA_ENV = "OUTDOOR_VISION_APP_DATA"
APP_DATA_FOLDER = "OutdoorVisionCV"
STATE_FILENAME = "app_state.json"


class StateLoadError(RuntimeError):
    """Raised when saved app data exists but cannot be interpreted safely."""


def default_app_data_dir() -> Path:
    override = os.environ.get(APP_DATA_ENV)
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DATA_FOLDER
    return Path.home() / ".outdoor_vision_cv"


class StateStore:
    """Load and atomically save the app-level configuration and registry."""

    def __init__(self, app_data_dir: Path | None = None) -> None:
        self.app_data_dir = (app_data_dir or default_app_data_dir()).resolve()
        self.state_path = self.app_data_dir / STATE_FILENAME

    def load(self) -> AppState:
        if not self.state_path.is_file():
            return AppState()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("The root value must be an object.")
            state = AppState.from_dict(raw)
            raw_projects = raw.get("projects", [])
            if any(isinstance(item, dict) and not item.get("project_id") for item in raw_projects):
                # Persist generated IDs once so private per-project tool state has
                # a stable identity across all future launches.
                self.save(state)
            return state
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise StateLoadError(
                f"Saved application data could not be read from:\n{self.state_path}\n\n{error}"
            ) from error

    def save(self, state: AppState) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.app_data_dir,
                prefix="app_state_",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.state_path)
        except OSError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def update_project_statistics(
        self,
        project_id: str,
        *,
        total_images: int | None = None,
        labeled_images: int | None = None,
        last_edited: str | None = None,
    ) -> ProjectRecord:
        """Public integration point for future project tools."""

        state = self.load()
        project = next(
            (item for item in state.projects if item.project_id == project_id), None
        )
        if project is None:
            raise KeyError(f"Unknown project id: {project_id}")
        if total_images is not None:
            project.total_images = int(total_images)
        if labeled_images is not None:
            project.labeled_images = int(labeled_images)
        if last_edited is not None:
            project.last_edited = last_edited
        self.save(state)
        return project
