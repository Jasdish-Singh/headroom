"""Saved toss plans from duplicates and downloads.

These are suggestions. Nothing in this module moves a file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from headroom.rules import state_dir


@dataclass
class PlanItem:
    path: str
    size: int
    accessed: float
    category: str
    reason: str
    source: str
    is_dir: bool = False


def plans_path(directory: Path | None = None) -> Path:
    return (state_dir() if directory is None else directory) / "plans.json"


def replace_plan(
    source: str,
    roots: list[Path],
    items: list[PlanItem],
    directory: Path | None = None,
) -> None:
    """Replace one source (dupes, downloads) and leave the others in place."""
    path = plans_path(directory)
    data = _read(path)
    sources = data.setdefault("sources", {})
    if not isinstance(sources, dict):
        sources = {}
        data["sources"] = sources
    sources[source] = {
        "roots": [str(root) for root in roots],
        "items": [
            {
                "path": item.path,
                "size": item.size,
                "accessed": item.accessed,
                "category": item.category,
                "reason": item.reason,
                "source": source,
                "is_dir": item.is_dir,
            }
            for item in items
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def load_plans(directory: Path | None = None) -> tuple[list[PlanItem], list[str]]:
    path = plans_path(directory)
    data = _read(path)
    sources = data.get("sources")
    if not isinstance(sources, dict):
        return [], []
    items: list[PlanItem] = []
    roots: list[str] = []
    for source, body in sources.items():
        if not isinstance(body, dict):
            continue
        for root in body.get("roots") or []:
            text = str(root)
            if text not in roots:
                roots.append(text)
        for raw in body.get("items") or []:
            if not isinstance(raw, dict) or "path" not in raw:
                continue
            items.append(
                PlanItem(
                    path=str(raw["path"]),
                    size=int(raw.get("size", 0)),
                    accessed=float(raw.get("accessed", 0)),
                    category=str(raw.get("category", "old")),
                    reason=str(raw.get("reason", "")),
                    source=str(raw.get("source") or source),
                    is_dir=bool(raw.get("is_dir", False)),
                )
            )
    return items, roots


def has_plans(directory: Path | None = None) -> bool:
    items, _roots = load_plans(directory)
    return bool(items)


def _read(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sources": {}}
    if not isinstance(data, dict):
        return {"sources": {}}
    return data
