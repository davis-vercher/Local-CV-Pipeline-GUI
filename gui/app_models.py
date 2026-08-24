"""Persistent model objects for the Outdoor Vision CV desktop app."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp suitable for persistence."""

    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProjectRecord:
    """A project known to the app-level registry."""

    name: str
    path: str
    last_edited: str = field(default_factory=utc_now_iso)
    total_images: int = 0
    labeled_images: int = 0
    project_id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def folder(self) -> Path:
        return Path(self.path)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectRecord":
        return cls(
            project_id=str(data.get("project_id") or uuid4()),
            name=str(data["name"]),
            path=str(data["path"]),
            last_edited=str(data.get("last_edited") or utc_now_iso()),
            total_images=int(data.get("total_images", 0)),
            labeled_images=int(data.get("labeled_images", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AppState:
    """The private application state stored outside user project folders."""

    library_path: str = ""
    projects: list[ProjectRecord] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppState":
        raw_projects = data.get("projects", [])
        if not isinstance(raw_projects, list):
            raise ValueError("The project registry must be a list.")
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            library_path=str(data.get("library_path", "")),
            projects=[ProjectRecord.from_dict(item) for item in raw_projects],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "library_path": self.library_path,
            "projects": [project.to_dict() for project in self.projects],
        }
