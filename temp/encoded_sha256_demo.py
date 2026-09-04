"""Compare the encoded SHA-256 values of two selected image files.

Run without arguments to select both files using Windows file dialogs:

    python encoded_sha256_demo.py

Or provide both paths directly:

    python encoded_sha256_demo.py "C:\\path\\first.jpg" "C:\\path\\second.jpg"
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys


READ_SIZE = 1024 * 1024


def encoded_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of the file's exact encoded bytes."""

    digest = hashlib.sha256()
    with path.open("rb") as image_file:
        while chunk := image_file.read(READ_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def select_image(title: str) -> Path | None:
    """Ask the user to select an image, returning None if cancelled."""

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askopenfilename(
            parent=root,
            title=title,
            filetypes=[
                ("JPEG images", "*.jpg *.jpeg"),
                ("All files", "*.*"),
            ],
        )
    finally:
        root.destroy()
    return Path(selected) if selected else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print encoded SHA-256 values for two image files."
    )
    parser.add_argument("first", nargs="?", type=Path, help="first image path")
    parser.add_argument("second", nargs="?", type=Path, help="second image path")
    args = parser.parse_args()
    if (args.first is None) != (args.second is None):
        parser.error("provide both image paths or omit both to use file dialogs")
    return args


def main() -> int:
    args = parse_args()
    try:
        first = args.first or select_image("Select the first image")
        if first is None:
            print("No first image selected.")
            return 1
        second = args.second or select_image("Select the second image")
        if second is None:
            print("No second image selected.")
            return 1

        for path in (first, second):
            if not path.is_file():
                print(f"File not found: {path}", file=sys.stderr)
                return 1

        first_hash = encoded_sha256(first)
        second_hash = encoded_sha256(second)
    except OSError as error:
        print(f"Could not read an image: {error}", file=sys.stderr)
        return 1

    print(f"Image 1: {first.resolve()}")
    print(f"encoded_sha256: {first_hash}")
    print()
    print(f"Image 2: {second.resolve()}")
    print(f"encoded_sha256: {second_hash}")
    print()
    if first_hash == second_hash:
        print("MATCH: the files are byte-for-byte identical.")
    else:
        print("NO MATCH: the encoded file contents differ.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
