from pathlib import Path
import tempfile
import unittest

from file_operations import list_jpegs, move_jpeg, undo_move


class FileOperationsTests(unittest.TestCase):
    def test_lists_jpegs_case_insensitively_and_ignores_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "b.JPG").write_bytes(b"b")
            (folder / "A.jpeg").write_bytes(b"a")
            (folder / "notes.txt").write_text("ignore", encoding="utf-8")
            self.assertEqual(
                [path.name for path in list_jpegs(folder)], ["A.jpeg", "b.JPG"]
            )

    def test_move_and_undo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            image = source / "animal.jpg"
            image.write_bytes(b"jpeg data")

            record = move_jpeg(image, destination)
            self.assertFalse(image.exists())
            self.assertTrue(record.moved_path.exists())

            restored = undo_move(record)
            self.assertEqual(restored, image)
            self.assertTrue(image.exists())
            self.assertFalse(record.moved_path.exists())

    def test_move_does_not_overwrite_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            image = source / "animal.jpg"
            image.write_bytes(b"new")
            (destination / "animal.jpg").write_bytes(b"existing")

            record = move_jpeg(image, destination)
            self.assertEqual(record.moved_path.name, "animal_1.jpg")
            self.assertEqual((destination / "animal.jpg").read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
