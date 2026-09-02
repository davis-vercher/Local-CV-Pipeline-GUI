from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image

from app_models import AppState, ProjectRecord
from app_storage import StateStore
from intake_services import IntakeManifest, QueueItem, create_class, ensure_intake_structure


@unittest.skipUnless(os.name == "nt", "The production UI is Windows-only.")
class IntakeUISmokeTests(unittest.TestCase):
    def test_project_tools_landing_and_responsive_sorting_view_build(self) -> None:
        try:
            from tkinterdnd2 import TkinterDnD
        except ImportError:
            self.skipTest("tkinterdnd2 is not installed")

        from outdoor_vision_app import OutdoorVisionApp

        with tempfile.TemporaryDirectory() as temporary:
            root_path = Path(temporary)
            library = root_path / "outdoor_vision_CV"
            project_folder = library / "Smoke Project"
            project_folder.mkdir(parents=True)
            project = ProjectRecord(name="Smoke Project", path=str(project_folder))
            store = StateStore(root_path / "app-data")
            store.save(AppState(library_path=str(library), projects=[project]))

            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = OutdoorVisionApp(root, store)
                app.show_project_tools(project)
                root.update_idletasks()
                self.assertEqual(app.app_state, "project_tools")

                app._open_intake(project)
                root.update_idletasks()
                self.assertEqual(app.app_state, "intake_landing")
                self.assertIsNotNone(app.intake_ui)
                intake = app.intake_ui
                unsorted, sorted_folder = ensure_intake_structure(project_folder)
                create_class(sorted_folder, "Hog")
                image_path = unsorted / "sample.jpg"
                Image.new("RGB", (80, 50), (40, 80, 120)).save(image_path, "JPEG")
                intake.manifest = IntakeManifest(
                    project.project_id,
                    [QueueItem("sample.jpg", "batch", "2026-09-02T12:00:00+00:00", "nested\\sample.jpg", 0)],
                )
                intake.store.save_manifest(intake.manifest)
                intake.mappings = {"1": "Hog"}
                intake.store.save_mappings(intake.mappings)
                intake.start_sorting()
                deadline = time.monotonic() + 5
                while intake.worker_active and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)
                root.update_idletasks()
                self.assertEqual(app.app_state, "intake_sorting")
                self.assertIsNotNone(intake.current_image)
                self.assertIn("sample.jpg", intake.sort_filename.get())

                first_controller = intake
                first_controller._leave_sorting()
                root.update_idletasks()
                self.assertTrue(first_controller.disposed)
                self.assertIsNone(first_controller.key_binding)
                self.assertEqual(app.app_state, "project_tools")

                app._open_intake(project)
                root.update_idletasks()
                self.assertIsNot(app.intake_ui, first_controller)
                event = mock.Mock(char="1")
                with mock.patch.object(first_controller, "_classify") as stale_classify:
                    first_controller._handle_sort_key(event)
                stale_classify.assert_not_called()
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
