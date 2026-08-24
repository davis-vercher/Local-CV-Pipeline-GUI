from pathlib import Path
import unittest

from app_models import ProjectRecord
from home_logic import (
    deletion_phrase_matches,
    filter_and_sort_projects,
    paginate,
    progress_values,
)


class HomeLogicTests(unittest.TestCase):
    def test_search_is_case_insensitive_and_sort_is_alphabetical(self) -> None:
        projects = [
            ProjectRecord(name="zebra Study", path="z", last_edited="2026-01-01T00:00:00+00:00"),
            ProjectRecord(name="Hog Detector", path="h", last_edited="2026-03-01T00:00:00+00:00"),
            ProjectRecord(name="hog Survey", path="s", last_edited="2026-02-01T00:00:00+00:00"),
        ]
        result = filter_and_sort_projects(projects, "HOG", "Project name")
        self.assertEqual([item.name for item in result], ["Hog Detector", "hog Survey"])

    def test_default_sort_is_newest_first(self) -> None:
        older = ProjectRecord(name="Older", path="o", last_edited="2026-01-01T00:00:00+00:00")
        newer = ProjectRecord(name="Newer", path="n", last_edited="2026-02-01T00:00:00+00:00")
        self.assertEqual(
            [item.name for item in filter_and_sort_projects([older, newer], "", "Last edited")],
            ["Newer", "Older"],
        )

    def test_pagination_clamps_page_after_deletion(self) -> None:
        visible, page, count = paginate(list(range(6)), page=3, capacity=5)
        self.assertEqual(visible, [5])
        self.assertEqual(page, 1)
        self.assertEqual(count, 2)

    def test_deletion_phrase_is_exact(self) -> None:
        self.assertTrue(deletion_phrase_matches("DELETE Hog", "Hog"))
        self.assertFalse(deletion_phrase_matches("delete Hog", "Hog"))
        self.assertFalse(deletion_phrase_matches("DELETE  Hog", "Hog"))

    def test_progress_is_safe_for_zero_and_inconsistent_counts(self) -> None:
        empty = ProjectRecord(name="Empty", path=str(Path("empty")))
        self.assertEqual(progress_values(empty), (0, 0, 0))
        inconsistent = ProjectRecord(
            name="High", path=str(Path("high")), total_images=2, labeled_images=9
        )
        self.assertEqual(progress_values(inconsistent), (9, 2, 100))
        negative = ProjectRecord(
            name="Negative", path=str(Path("negative")), total_images=4, labeled_images=-3
        )
        self.assertEqual(progress_values(negative), (0, 4, 0))


if __name__ == "__main__":
    unittest.main()
