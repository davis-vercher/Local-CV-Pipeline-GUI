from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from app_models import AppState, ProjectRecord
from app_storage import StateLoadError, StateStore
from project_services import (
    LIBRARY_NAME,
    UnsafePathError,
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


class FailingStore(StateStore):
    def save(self, state: AppState) -> None:
        raise OSError("simulated persistence failure")


class ProjectServicesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app_data = self.root / "app-data"
        self.store = StateStore(self.app_data)
        self.library = create_library(self.root)
        self.state = AppState(library_path=str(self.library))
        self.store.save(self.state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_library_refuses_an_existing_destination(self) -> None:
        with self.assertRaises(ValidationError):
            create_library(self.root)

    def test_recovery_recreates_a_library_deleted_outside_the_app(self) -> None:
        self.library.rmdir()
        recovered = recover_library(self.root)
        self.assertEqual(recovered, self.root / LIBRARY_NAME)
        self.assertTrue(recovered.is_dir())

    def test_recreated_library_can_create_new_projects_and_preserves_stale_records(self) -> None:
        old_project = create_project(self.state, self.store, "Deleted Outside App")
        old_project.folder.rmdir()
        self.library.rmdir()

        recovered = recover_library(self.root)
        self.state.library_path = str(recovered)
        for project in self.state.projects:
            project.path = str(recovered / project.name)
        self.store.save(self.state)

        new_project = create_project(self.state, self.store, "New Project")
        self.assertTrue(new_project.folder.is_dir())
        self.assertFalse(old_project.folder.exists())
        self.assertEqual(len(self.store.load().projects), 2)

    def test_recovery_accepts_an_existing_library(self) -> None:
        self.assertEqual(recover_library(self.root), self.library)

    def test_windows_name_rules_and_case_insensitive_duplicates(self) -> None:
        valid = "Hog Detector"
        self.assertEqual(
            validate_project_name(valid, library=self.library, projects=[]), valid
        )
        for invalid in ("", "A" * 26, "bad/name", "trailing.", " trailing", "CON", "COM1.txt"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_project_name(invalid, library=self.library, projects=[])
        registered = ProjectRecord(name="Hog Detector", path=str(self.library / valid))
        with self.assertRaises(ValidationError):
            validate_project_name("hog detector", library=self.library, projects=[registered])

    def test_unregistered_filesystem_collision_is_rejected(self) -> None:
        (self.library / "Manual Folder").mkdir()
        with self.assertRaises(ValidationError):
            validate_project_name("Manual Folder", library=self.library, projects=[])

    def test_project_creation_is_empty_and_persistent(self) -> None:
        project = create_project(self.state, self.store, "Hog")
        self.assertTrue(project.folder.is_dir())
        self.assertEqual(list(project.folder.iterdir()), [])
        loaded = self.store.load()
        self.assertEqual(loaded.projects[0].name, "Hog")
        self.assertEqual(loaded.projects[0].total_images, 0)

    def test_project_creation_rolls_back_when_registry_save_fails(self) -> None:
        failing = FailingStore(self.app_data)
        with self.assertRaises(OSError):
            create_project(self.state, failing, "Rollback")
        self.assertFalse((self.library / "Rollback").exists())
        self.assertEqual(self.state.projects, [])

    def test_rename_updates_real_folder_and_registry(self) -> None:
        project = create_project(self.state, self.store, "Old Name")
        original_timestamp = project.last_edited
        rename_project(self.state, self.store, project, "New Name")
        self.assertFalse((self.library / "Old Name").exists())
        self.assertTrue((self.library / "New Name").is_dir())
        self.assertEqual(self.store.load().projects[0].name, "New Name")
        self.assertGreaterEqual(project.last_edited, original_timestamp)

    def test_capitalization_only_rename(self) -> None:
        project = create_project(self.state, self.store, "hog")
        rename_project(self.state, self.store, project, "Hog")
        self.assertEqual(project.name, "Hog")
        self.assertTrue((self.library / "Hog").is_dir())

    def test_delete_revalidates_direct_child_target(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        project = ProjectRecord(name="outside", path=str(outside))
        self.state.projects.append(project)
        with self.assertRaises(UnsafePathError):
            delete_project(self.state, self.store, project)
        self.assertTrue(outside.exists())

    def test_delete_removes_folder_and_registry(self) -> None:
        project = create_project(self.state, self.store, "Delete Me")
        (project.folder / "data.txt").write_text("content", encoding="utf-8")
        delete_project(self.state, self.store, project)
        self.assertFalse(project.folder.exists())
        self.assertEqual(self.store.load().projects, [])

    def test_missing_project_can_be_removed_without_deletion(self) -> None:
        project = ProjectRecord(name="Missing", path=str(self.library / "Missing"))
        self.state.projects.append(project)
        self.store.save(self.state)
        remove_stale_project(self.state, self.store, project)
        self.assertEqual(self.state.projects, [])

    def test_scan_counts_jpegs_recursively_and_preserves_label_count(self) -> None:
        project = create_project(self.state, self.store, "Scan")
        project.labeled_images = 7
        nested = project.folder / "nested"
        nested.mkdir()
        (project.folder / "one.JPG").write_bytes(b"1")
        (nested / "two.jpeg").write_bytes(b"2")
        (nested / "ignore.png").write_bytes(b"3")
        result = scan_project(project)
        self.assertEqual(result.total_images, 2)
        failures = apply_scan_results(self.state, self.store, [result])
        self.assertEqual(failures, [])
        self.assertEqual(project.total_images, 2)
        self.assertEqual(project.labeled_images, 7)

    def test_scan_does_not_follow_directory_links(self) -> None:
        project = create_project(self.state, self.store, "Links")
        external = self.root / "external"
        external.mkdir()
        (external / "outside.jpg").write_bytes(b"x")
        link = project.folder / "external-link"
        try:
            link.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are not available to this Windows user.")
        self.assertEqual(scan_project(project).total_images, 0)

    def test_same_volume_library_move_updates_every_path(self) -> None:
        project = create_project(self.state, self.store, "Move Me")
        (project.folder / "image.jpg").write_bytes(b"image")
        new_parent = self.root / "destination-parent"
        new_parent.mkdir()
        destination = move_library(self.state, self.store, new_parent)
        self.assertEqual(destination, new_parent / LIBRARY_NAME)
        self.assertFalse(self.library.exists())
        self.assertTrue((destination / "Move Me" / "image.jpg").is_file())
        loaded = self.store.load()
        self.assertEqual(Path(loaded.projects[0].path), destination / "Move Me")

    def test_library_move_refuses_merge_or_overwrite(self) -> None:
        new_parent = self.root / "occupied-parent"
        new_parent.mkdir()
        (new_parent / LIBRARY_NAME).mkdir()
        with self.assertRaises(ValidationError):
            move_library(self.state, self.store, new_parent)
        self.assertTrue(self.library.is_dir())

    def test_library_move_rolls_back_when_registry_save_fails(self) -> None:
        project = create_project(self.state, self.store, "Stay Put")
        new_parent = self.root / "failing-parent"
        new_parent.mkdir()
        with self.assertRaises(OSError):
            move_library(self.state, FailingStore(self.app_data), new_parent)
        self.assertTrue(self.library.is_dir())
        self.assertTrue(project.folder.is_dir())
        self.assertFalse((new_parent / LIBRARY_NAME).exists())
        self.assertEqual(Path(self.state.library_path), self.library)

    def test_statistics_interface_updates_registry(self) -> None:
        project = create_project(self.state, self.store, "Stats")
        self.store.update_project_statistics(
            project.project_id, total_images=20, labeled_images=8
        )
        loaded = self.store.load().projects[0]
        self.assertEqual((loaded.total_images, loaded.labeled_images), (20, 8))

    def test_inconsistent_statistics_are_preserved_for_reconciliation(self) -> None:
        project = create_project(self.state, self.store, "Inconsistent")
        self.store.update_project_statistics(
            project.project_id, total_images=2, labeled_images=7
        )
        loaded = self.store.load().projects[0]
        self.assertEqual((loaded.total_images, loaded.labeled_images), (2, 7))

    def test_malformed_registry_has_clear_failure(self) -> None:
        self.store.state_path.write_text("{bad json", encoding="utf-8")
        with self.assertRaises(StateLoadError):
            self.store.load()

    def test_legacy_registry_project_id_is_migrated_once_and_stays_stable(self) -> None:
        self.store.state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "library_path": str(self.library),
                    "projects": [{"name": "Legacy", "path": str(self.library / "Legacy")}],
                }
            ),
            encoding="utf-8",
        )
        first_id = self.store.load().projects[0].project_id
        second_id = self.store.load().projects[0].project_id
        self.assertEqual(first_id, second_id)


if __name__ == "__main__":
    unittest.main()
