"""Filesystem-safe output names, shared by the pipeline and the Drive flow.

Its own module so drive_flow (a long-running watcher) can sanitise a name
without importing pipeline, which would drag torch and docling into it.
"""
from pathlib import Path


def safe_name(img_path) -> str:
    """Folder/file-safe stem for an image.

    Windows silently TRIMS trailing spaces and dots from a path component, so a
    Drive file called "Chemical equation upper right .png" produced a folder on
    disk named "...right" while every path we built still carried the space ->
    WinError 3 on the first subfolder. Strip those here, and the characters
    Windows forbids outright, so the name we compute and the name that lands on
    disk are the same thing.
    """
    stem = Path(img_path).stem
    for ch in r'<>:"/\|?*':
        stem = stem.replace(ch, "_")
    stem = "".join(c for c in stem if c >= " ").strip().rstrip(". ")
    return stem or "image"
