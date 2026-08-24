"""Pure home-page calculations kept separate from Tkinter widgets."""

from __future__ import annotations

import math
from typing import Sequence, TypeVar

from app_models import ProjectRecord


T = TypeVar("T")


def filter_and_sort_projects(
    projects: Sequence[ProjectRecord], query: str, sort_mode: str
) -> list[ProjectRecord]:
    folded_query = query.strip().casefold()
    result = [project for project in projects if folded_query in project.name.casefold()]
    if sort_mode == "Project name":
        result.sort(key=lambda project: project.name.casefold())
    else:
        result.sort(key=lambda project: project.last_edited, reverse=True)
    return result


def paginate(items: Sequence[T], page: int, capacity: int) -> tuple[list[T], int, int]:
    safe_capacity = max(1, capacity)
    page_count = max(1, math.ceil(len(items) / safe_capacity))
    safe_page = min(max(0, page), page_count - 1)
    start = safe_page * safe_capacity
    return list(items[start : start + safe_capacity]), safe_page, page_count


def deletion_phrase_matches(typed: str, project_name: str) -> bool:
    return typed == f"DELETE {project_name}"


def progress_values(project: ProjectRecord) -> tuple[int, int, int]:
    total = max(0, project.total_images)
    labeled = max(0, project.labeled_images)
    counted = min(labeled, total) if total else 0
    percentage = round((counted / total) * 100) if total else 0
    return labeled, total, min(100, max(0, percentage))

