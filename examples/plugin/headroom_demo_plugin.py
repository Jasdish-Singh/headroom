"""Example Headroom plugin. Install this folder to register it.

Scanners return hits. Profiles return keep/toss text. Report sections return text.
None of these functions delete or move files.
"""

from __future__ import annotations

import os
from pathlib import Path


def scan(root):
    """Flag ``*.scratch`` files. Links are not followed."""
    root = Path(root)
    hits = []
    if not root.exists() or root.is_symlink():
        return hits
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not (Path(dirpath) / name).is_symlink()]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or path.suffix.casefold() != ".scratch":
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            hits.append({"path": str(path), "size": size, "note": "scratch file"})
    return hits


def profile() -> str:
    return "toss name: *.scratch older-than: 30d\n"


def report_section(context) -> str:
    del context
    return "Demo plugin: scratch files are toss suggestions once they are 30 days old.\n"
