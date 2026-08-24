# Outdoor Vision CV Desktop App

A local Windows desktop application for creating and managing computer-vision
projects. The project home page is the entry point for the future Outdoor Vision
CV data-preparation pipeline.

The original JPEG Dataset Sorter remains available as a standalone prototype and
has not yet been integrated into a project workspace.

## Setup

1. Double-click `run_outdoor_vision_cv.bat`. It will use a compatible local
   Python installation automatically when one is available.
2. If the launcher reports that Python is missing, install Python 3 for Windows
   from python.org. During installation, enable **Add Python to PATH** and ensure
   Tcl/Tk is selected.

Pillow is not required by the home page. To use the legacy image sorter, install
its image-display dependency:

       python -m pip install -r requirements.txt

Then launch it with `run_jpg_sorter.bat`.

The applications do not connect to the internet.

## Home Page Use

1. On first launch, select a user-accessible parent folder. The app creates and
   remembers an `outdoor_vision_CV` project library there.
2. Select **New Project**, enter a valid Windows folder name of at most 25
   characters, and create the project.
3. Search or sort the project cards. Select **Refresh** to recount JPEG files
   recursively in every project.
4. Click a project card to enter its placeholder project screen, or click its
   path to open the folder in Windows File Explorer.
5. Use the card's ellipsis menu to rename or permanently delete a project.
6. Use **Settings** to move the complete project library to another parent
   folder.

Private registry data is stored in the user's local Windows application-data
folder. User project folders contain only user-visible project content.

## Legacy JPEG Sorter Use

1. Select the source folder containing `.jpg` or `.jpeg` files.
2. Give each class a name and assign its destination folder to a number key.
   Each folder may be assigned to only one key.
3. Select **Start**.
4. Press an assigned number key, or click its on-screen button, to move the
   displayed image into that destination. The number-key legend remains visible
   below the image as a reminder of every assigned class name.
5. Select **Undo last move** to restore only the most recently moved image to
   the source folder. The restored image is displayed again immediately.

The app processes files in alphabetical filename order. It never overwrites an
existing destination file: if a name is already taken, `_1`, `_2`, and so on is
added to the moved filename. Folder choices are saved locally in
`sorter_config.json` after Start is selected.

## Tests

From the repository root, run:

    python -m unittest discover -s data\gui -p "test_*.py" -v
