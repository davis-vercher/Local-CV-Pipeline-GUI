from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from PIL import Image

from intake_services import (
    IntakeManifest,
    IntakeStore,
    IntakeValidationError,
    ManifestError,
    MappingError,
    QueueItem,
    StaleQueueItemError,
    active_queue,
    available_name,
    classify_item,
    copy_discovery,
    create_class,
    discover_classes,
    discover_source,
    ensure_intake_structure,
    repair_manifest,
    require_destination_space,
    recycle_class,
    rename_class,
    review_dataset,
    skip_item,
    undo_classification,
    validate_class_name,
)


# shutil.disk_usage_result is not exposed consistently across supported Python
# versions, so use a minimal tuple with matching attributes for the unit test.
shutil_disk_usage = namedtuple("shutil_disk_usage", "total used free")


def make_jpeg(path: Path, color: tuple[int, int, int] = (20, 40, 60)) -> bytes:
    Image.new("RGB", (12, 8), color).save(path, format="JPEG", quality=90)
    return path.read_bytes()


class FailingManifestStore(IntakeStore):
    def save_manifest(self, manifest: IntakeManifest) -> None:
        raise OSError("simulated manifest failure")


class IntakeServicesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.unsorted, self.sorted_folder = ensure_intake_structure(self.project)
        self.source = self.root / "source"
        self.source.mkdir()
        self.store = IntakeStore("project-id", self.root / "app-data")
        self.manifest = IntakeManifest("project-id")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_structure_creation_is_idempotent_and_preserves_contents(self) -> None:
        marker = self.unsorted / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        second_unsorted, second_sorted = ensure_intake_structure(self.project)
        self.assertEqual((second_unsorted, second_sorted), (self.unsorted, self.sorted_folder))
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_discovery_is_recursive_case_insensitive_and_validates_content(self) -> None:
        nested = self.source / "Nested"
        nested.mkdir()
        original = make_jpeg(nested / "Photo.JPG")
        (self.source / "fake.jpeg").write_text("not an image", encoding="utf-8")
        (self.source / "ignored.png").write_bytes(original)
        result = discover_source(self.source, self.unsorted)
        self.assertEqual([item.source_relative_path for item in result.valid], [str(Path("Nested") / "Photo.JPG")])
        self.assertEqual(result.invalid, ["fake.jpeg"])
        self.assertEqual((nested / "Photo.JPG").read_bytes(), original)
        self.assertEqual(list(self.unsorted.iterdir()), [])

    def test_discovery_skips_hidden_entries_as_one_entry_each(self) -> None:
        hidden = self.source / ".hidden"
        hidden.mkdir()
        make_jpeg(hidden / "inside.jpg")
        make_jpeg(self.source / ".secret.jpg")
        make_jpeg(self.source / "visible.jpg")
        result = discover_source(self.source, self.unsorted)
        self.assertEqual(result.hidden_or_system_skipped, 2)
        self.assertEqual([item.source_relative_path for item in result.valid], ["visible.jpg"])

    def test_discovery_rejects_overlapping_source_and_destination(self) -> None:
        with self.assertRaises(IntakeValidationError):
            discover_source(self.project, self.unsorted)

    def test_preview_reserves_deterministic_collision_names(self) -> None:
        make_jpeg(self.unsorted / "same.jpg")
        make_jpeg(self.source / "same.jpg")
        nested = self.source / "nested"
        nested.mkdir()
        make_jpeg(nested / "same.jpg", (80, 20, 10))
        result = discover_source(self.source, self.unsorted)
        self.assertEqual([item.preview_name for item in result.valid], ["same_1.jpg", "same_2.jpg"])
        self.assertEqual(result.collision_count, 2)

    def test_copy_preserves_bytes_source_and_persists_queue(self) -> None:
        original = make_jpeg(self.source / "photo.jpg")
        discovery = discover_source(self.source, self.unsorted)
        result = copy_discovery(discovery, self.store, self.manifest)
        self.assertEqual((result.valid_discovered, result.copied), (1, 1))
        self.assertEqual((self.unsorted / "photo.jpg").read_bytes(), original)
        self.assertEqual((self.source / "photo.jpg").read_bytes(), original)
        loaded = self.store.load_manifest()
        self.assertEqual(loaded.items[0].source_relative_path, "photo.jpg")

    def test_copy_rechecks_collision_at_action_time(self) -> None:
        make_jpeg(self.source / "photo.jpg")
        discovery = discover_source(self.source, self.unsorted)
        make_jpeg(self.unsorted / "photo.jpg", (1, 2, 3))
        result = copy_discovery(discovery, self.store, self.manifest)
        self.assertTrue((self.unsorted / "photo_1.jpg").is_file())
        self.assertEqual(result.renamed, ["photo.jpg -> photo_1.jpg"])

    def test_free_space_check_reports_required_and_available(self) -> None:
        usage = shutil_disk_usage(total=100, used=95, free=5)
        with mock.patch("intake_services.shutil.disk_usage", return_value=usage):
            with self.assertRaisesRegex(IntakeValidationError, "Required: 10 bytes. Available: 5 bytes"):
                require_destination_space(self.unsorted, 10)
            self.assertEqual(require_destination_space(self.unsorted, 5), 5)

    def test_cancelled_copy_retains_completed_items(self) -> None:
        make_jpeg(self.source / "a.jpg")
        make_jpeg(self.source / "b.jpg")
        discovery = discover_source(self.source, self.unsorted)
        cancel = threading.Event()

        def progress(copied_files: int, *_args: int) -> None:
            if copied_files == 1:
                cancel.set()

        result = copy_discovery(discovery, self.store, self.manifest, cancel, progress)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.copied, 1)
        self.assertEqual(len(self.store.load_manifest().items), 1)

    def test_manifest_failure_discloses_physically_copied_file_as_partial(self) -> None:
        make_jpeg(self.source / "photo.jpg")
        discovery = discover_source(self.source, self.unsorted)
        result = copy_discovery(
            discovery,
            FailingManifestStore("project-id", self.root / "bad-app-data"),
            self.manifest,
        )
        self.assertEqual(result.copied, 1)
        self.assertIsNotNone(result.systemic_error)
        self.assertTrue((self.unsorted / "photo.jpg").is_file())
        self.assertEqual(self.manifest.items, [])

    def test_systemic_copy_failure_stops_after_successful_partial_copy(self) -> None:
        make_jpeg(self.source / "a.jpg")
        make_jpeg(self.source / "b.jpg")
        discovery = discover_source(self.source, self.unsorted)
        from intake_services import _copy_file_exclusive

        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("destination unavailable")
            return _copy_file_exclusive(*args, **kwargs)

        with mock.patch("intake_services._copy_file_exclusive", side_effect=fail_second):
            result = copy_discovery(discovery, self.store, self.manifest)
        self.assertEqual(result.copied, 1)
        self.assertIn("destination unavailable", result.systemic_error or "")
        self.assertEqual(len(self.store.load_manifest().items), 1)

    def test_manifest_and_mappings_are_atomic_and_bound_to_project_id(self) -> None:
        item = QueueItem("a.jpg", "batch", "2026-01-01T00:00:00+00:00", "a.jpg", 0)
        self.manifest.items.append(item)
        self.store.save_manifest(self.manifest)
        self.store.save_mappings({"0": "Hog", "1": "Deer"})
        self.assertEqual(self.store.load_manifest().items[0].current_name, "a.jpg")
        self.assertEqual(self.store.load_mappings(), {"0": "Hog", "1": "Deer"})
        self.assertEqual(list(self.store.folder.glob("*.tmp")), [])
        value = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        value["project_id"] = "another-project"
        self.store.manifest_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ManifestError):
            self.store.load_manifest()

    def test_mapping_exclusivity_is_enforced(self) -> None:
        with self.assertRaises(MappingError):
            self.store.save_mappings({"0": "Hog", "1": "hog"})

    def test_manual_and_missing_files_do_not_join_active_queue(self) -> None:
        make_jpeg(self.unsorted / "registered.jpg")
        make_jpeg(self.unsorted / "manual.jpg")
        self.manifest.items = [
            QueueItem("registered.jpg", "batch", "time", "registered.jpg", 0),
            QueueItem("missing.jpg", "batch", "time", "missing.jpg", 1),
        ]
        self.assertEqual([item.current_name for item in active_queue(self.manifest, self.unsorted)], ["registered.jpg"])

    def test_import_batches_remain_older_first_with_final_name_order(self) -> None:
        for name in ("z.jpg", "A.jpg"):
            make_jpeg(self.source / name)
        first = discover_source(self.source, self.unsorted)
        copy_discovery(first, self.store, self.manifest)
        for path in self.source.iterdir():
            path.unlink()
        make_jpeg(self.source / "middle.jpg")
        second = discover_source(self.source, self.unsorted)
        copy_discovery(second, self.store, self.manifest)
        loaded = self.store.load_manifest()
        self.assertEqual(
            [item.current_name for item in active_queue(loaded, self.unsorted)],
            ["A.jpg", "z.jpg", "middle.jpg"],
        )
        self.assertEqual(len({item.batch_id for item in loaded.items}), 2)

    def test_skip_moves_item_behind_complete_current_queue(self) -> None:
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            make_jpeg(self.unsorted / name)
        self.manifest.items = [QueueItem(name, "batch", "time", name, index) for index, name in enumerate(("a.jpg", "b.jpg", "c.jpg"))]
        self.store.save_manifest(self.manifest)
        skip_item(self.store, self.manifest, self.manifest.items[0])
        self.assertEqual([item.current_name for item in active_queue(self.manifest, self.unsorted)], ["b.jpg", "c.jpg", "a.jpg"])

    def test_classify_and_multi_step_undo_use_safe_collisions(self) -> None:
        create_class(self.sorted_folder, "Hog")
        for name in ("a.jpg", "b.jpg"):
            make_jpeg(self.unsorted / name)
        self.manifest.items = [QueueItem(name, "batch", "time", name, index) for index, name in enumerate(("a.jpg", "b.jpg"))]
        self.store.save_manifest(self.manifest)
        first = classify_item(self.store, self.manifest, self.unsorted, self.sorted_folder, self.manifest.items[0], "Hog")
        second = classify_item(self.store, self.manifest, self.unsorted, self.sorted_folder, self.manifest.items[0], "Hog")
        make_jpeg(self.unsorted / "b.jpg", (100, 100, 100))
        restored_second, renamed = undo_classification(self.store, self.manifest, self.unsorted, second)
        restored_first, _ = undo_classification(self.store, self.manifest, self.unsorted, first)
        self.assertTrue(renamed)
        self.assertEqual(restored_second.current_name, "b_1.jpg")
        self.assertEqual([item.current_name for item in active_queue(self.manifest, self.unsorted)], ["a.jpg", "b_1.jpg"])
        self.assertTrue((self.unsorted / restored_first.current_name).is_file())

    def test_stale_controller_cannot_move_or_overwrite_manifest_twice(self) -> None:
        create_class(self.sorted_folder, "Hog")
        make_jpeg(self.unsorted / "a.jpg")
        self.manifest.items = [QueueItem("a.jpg", "batch", "time", "a.jpg", 0)]
        self.store.save_manifest(self.manifest)
        first_snapshot = self.store.load_manifest()
        stale_snapshot = self.store.load_manifest()

        classify_item(
            self.store,
            first_snapshot,
            self.unsorted,
            self.sorted_folder,
            first_snapshot.items[0],
            "Hog",
        )
        with self.assertRaises(StaleQueueItemError):
            classify_item(
                self.store,
                stale_snapshot,
                self.unsorted,
                self.sorted_folder,
                stale_snapshot.items[0],
                "Hog",
            )

        self.assertEqual(self.store.load_manifest().items, [])
        self.assertTrue((self.sorted_folder / "Hog" / "a.jpg").is_file())

    def test_class_validation_discovery_and_case_only_rename(self) -> None:
        hog = create_class(self.sorted_folder, "hog")
        renamed = rename_class(self.sorted_folder, "hog", "Hog")
        self.assertEqual(hog.name, "hog")
        self.assertTrue(renamed.is_dir())
        self.assertEqual(renamed.name, "Hog")
        self.assertEqual(discover_classes(self.sorted_folder), ["Hog"])
        for invalid in ("", "A" * 26, "bad/name", "trailing.", " CON", "COM1.txt"):
            with self.subTest(invalid=invalid), self.assertRaises(IntakeValidationError):
                validate_class_name(invalid, self.sorted_folder)

    def test_review_orders_mapped_then_unmapped_and_ignores_nested_images(self) -> None:
        for name in ("Zebra", "Hog", "Deer"):
            create_class(self.sorted_folder, name)
        make_jpeg(self.sorted_folder / "Hog" / "one.jpg")
        make_jpeg(self.sorted_folder / "Deer" / "two.jpeg")
        nested = self.sorted_folder / "Hog" / "nested"
        nested.mkdir()
        make_jpeg(nested / "ignored.jpg")
        result = review_dataset(self.sorted_folder, {"0": "Hog", "9": "Zebra"})
        self.assertEqual([item.name for item in result.classes], ["Hog", "Zebra", "Deer"])
        self.assertEqual(result.total_jpegs, 2)
        self.assertEqual(result.nested_folder_count, 1)

    def test_recycle_failure_never_falls_back_to_permanent_deletion(self) -> None:
        folder = create_class(self.sorted_folder, "Keep Me")
        make_jpeg(folder / "image.jpg")
        with mock.patch("send2trash.send2trash", side_effect=OSError("recycle unavailable")):
            with self.assertRaises(OSError):
                recycle_class(self.sorted_folder, "Keep Me")
        self.assertTrue(folder.is_dir())
        self.assertTrue((folder / "image.jpg").is_file())

    def test_repair_validates_direct_jpegs_and_creates_one_batch(self) -> None:
        make_jpeg(self.unsorted / "b.jpg")
        make_jpeg(self.unsorted / "A.jpeg")
        (self.unsorted / "bad.jpg").write_bytes(b"bad")
        nested = self.unsorted / "nested"
        nested.mkdir()
        make_jpeg(nested / "ignored.jpg")
        repaired, omitted = repair_manifest(self.store, self.unsorted)
        self.assertEqual([item.current_name for item in repaired.items], ["A.jpeg", "b.jpg"])
        self.assertEqual(omitted, ["bad.jpg"])
        self.assertEqual(len({item.batch_id for item in repaired.items}), 1)

    def test_available_name_is_case_insensitive(self) -> None:
        (self.unsorted / "Photo.JPG").write_bytes(b"existing")
        self.assertEqual(available_name(self.unsorted, "photo.jpg"), "photo_1.jpg")


if __name__ == "__main__":
    unittest.main()
