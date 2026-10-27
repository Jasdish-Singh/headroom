"""Plain-text keep and toss rules.

A profile and a note you paste use the same lines. Later rules win, and user
rules are always loaded after the selected profiles so they override them.
A line has to name something. A bare ``toss`` is rejected.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from headroom.logsetup import default_log_path

PROFILE_NAMES = ("student", "developer", "gamer")
CATEGORIES = (
    "school",
    "code",
    "games",
    "media",
    "downloads",
    "installers",
    "duplicates",
    "caches",
    "old",
)
_KEYS = (
    "name",
    "path",
    "ext",
    "category",
    "semester",
    "older-than",
    "newer-than",
    "larger-than",
)
_KEY_RE = re.compile(
    r"(?:^|\s)(name|path|ext|category|semester|older-than|newer-than|larger-than):",
    re.IGNORECASE,
)
_SEASON_RE = re.compile(
    r"(spring|summer|fall|autumn|winter)\s*(20\d{2})|(20\d{2})\s*(spring|summer|fall|autumn|winter)",
    re.IGNORECASE,
)
_SEASON_INDEX = {"spring": 1, "summer": 2, "fall": 3, "autumn": 3, "winter": 4}
_SIZE_UNITS = {"tb": 1024**4, "gb": 1024**3, "mb": 1024**2, "kb": 1024, "b": 1}


class RuleError(Exception):
    """A keep/toss line is missing or not usable."""


@dataclass(frozen=True)
class Candidate:
    """One file or folder a rule can match. Paths may use either slash."""

    path: str
    size: int
    accessed: float
    category: str
    is_dir: bool = False

    @property
    def name(self) -> str:
        return PurePosixPath(self.path.replace("\\", "/")).name

    @property
    def ext(self) -> str:
        if self.is_dir:
            return ""
        suffix = PurePosixPath(self.name).suffix
        return suffix.casefold()


@dataclass(frozen=True)
class Rule:
    action: str
    source: str
    line_no: int
    text: str
    name: str | None = None
    path: str | None = None
    ext: str | None = None
    category: str | None = None
    semester: str | None = None
    older_than_days: int | None = None
    newer_than_days: int | None = None
    larger_than_bytes: int | None = None

    def matches(self, item: Candidate, *, now: float, today: date) -> bool:
        if self.name is not None and not _glob(self.name, item.name):
            return False
        if self.path is not None and not _path_glob(self.path, item.path):
            return False
        if self.ext is not None and item.ext != _normalize_ext(self.ext):
            return False
        if self.category is not None and item.category != self.category:
            return False
        if self.semester is not None and not _semester_matches(self.semester, item.path, today):
            return False
        age = (now - item.accessed) / 86400
        if self.older_than_days is not None and age < self.older_than_days:
            return False
        if self.newer_than_days is not None and age >= self.newer_than_days:
            return False
        if self.larger_than_bytes is not None and item.size < self.larger_than_bytes:
            return False
        return True


def state_dir(env=None, home: Path | None = None) -> Path:
    return default_log_path(env, home).parent


def profile_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "profiles" / f"{name}.rules"


def load_profile_text(name: str) -> str:
    key = name.strip().casefold()
    if key not in PROFILE_NAMES:
        known = ", ".join(PROFILE_NAMES)
        raise RuleError(f"unknown profile {name!r}. Known profiles: {known}")
    path = profile_path(key)
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuleError(f"could not read profile {key}: {exc}") from exc


def parse_rules(text: str, *, source: str) -> list[Rule]:
    rules: list[Rule] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rules.append(_parse_line(line, source=source, line_no=line_no))
    return rules


def known_profile_names() -> list[str]:
    names = list(PROFILE_NAMES)
    try:
        from headroom.plugins import profile_plugins
    except Exception:
        return names
    for plugin in profile_plugins():
        key = plugin.name.strip().casefold()
        if key and key not in names:
            names.append(key)
    return names


def load_profile(name: str) -> list[Rule]:
    key = name.strip().casefold()
    if key in PROFILE_NAMES:
        return parse_rules(load_profile_text(key), source=key)
    from headroom.plugins import profile_text

    text = profile_text(key)
    if text is None:
        known = ", ".join(known_profile_names())
        raise RuleError(f"unknown profile {name!r}. Known profiles: {known}")
    return parse_rules(text, source=key)


def merge_rules(groups: list[list[Rule]]) -> list[Rule]:
    """Concatenate groups. The last matching rule wins when they are applied."""
    merged: list[Rule] = []
    for group in groups:
        merged.extend(group)
    return merged


def decide(rules: list[Rule], item: Candidate, *, now: float, today: date) -> str:
    """Return ``keep``, ``toss``, or ``unmatched``. The last match wins."""
    action = "unmatched"
    for rule in rules:
        if rule.matches(item, now=now, today=today):
            action = rule.action
    return action


def winning_rule(rules: list[Rule], item: Candidate, *, now: float, today: date) -> Rule | None:
    found: Rule | None = None
    for rule in rules:
        if rule.matches(item, now=now, today=today):
            found = rule
    return found


def active_profiles_path(directory: Path | None = None) -> Path:
    return (state_dir() if directory is None else directory) / "profiles.active"


def user_rules_path(directory: Path | None = None) -> Path:
    return (state_dir() if directory is None else directory) / "user.rules"


def read_active_profiles(directory: Path | None = None) -> list[str]:
    path = active_profiles_path(directory)
    if not path.is_file():
        return []
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        name = raw.strip().casefold()
        if name and name not in names:
            names.append(name)
    return names


def write_active_profiles(names: list[str], directory: Path | None = None) -> list[str]:
    cleaned: list[str] = []
    for name in names:
        key = name.strip().casefold()
        if key in {"", "none"}:
            continue
        if key not in known_profile_names():
            known = ", ".join(known_profile_names())
            raise RuleError(f"unknown profile {name!r}. Known profiles: {known}")
        if key not in cleaned:
            cleaned.append(key)
    path = active_profiles_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(cleaned)
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")
    return cleaned


def read_user_text(directory: Path | None = None) -> str:
    path = user_rules_path(directory)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig")


def append_user_text(text: str, directory: Path | None = None) -> list[Rule]:
    """Validate ``text`` and append it to the user rule file."""
    addition = parse_rules(text, source="user")
    if not addition:
        raise RuleError("no keep or toss rules were found in that text")
    path = user_rules_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_user_text(directory)
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + separator + text.strip() + "\n", encoding="utf-8")
    return addition


def load_merged_rules(directory: Path | None = None) -> list[Rule]:
    groups: list[list[Rule]] = []
    for name in read_active_profiles(directory):
        try:
            groups.append(load_profile(name))
        except RuleError:
            if name in PROFILE_NAMES:
                raise
            continue
    user = read_user_text(directory)
    if user.strip():
        groups.append(parse_rules(user, source="user"))
    return merge_rules(groups)


def current_semester(today: date) -> tuple[int, int]:
    """Return ``(year, season)`` with spring=1, summer=2, fall=3, winter=4."""
    month, day = today.month, today.day
    if (month, day) <= (5, 15):
        return today.year, 1
    if (month, day) <= (8, 15):
        return today.year, 2
    if (month, day) <= (12, 15):
        return today.year, 3
    return today.year, 4


def semesters_in_path(path: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for match in _SEASON_RE.finditer(path.replace("\\", "/")):
        if match.group(1) and match.group(2):
            season, year = match.group(1), match.group(2)
        else:
            year, season = match.group(3), match.group(4)
        found.append((int(year), _SEASON_INDEX[season.casefold()]))
    return found


def _semester_matches(kind: str, path: str, today: date) -> bool:
    tokens = semesters_in_path(path)
    if not tokens:
        return False
    current = current_semester(today)
    has_current = current in tokens
    has_past = any(token < current for token in tokens)
    if kind == "current":
        return has_current
    if kind == "past":
        return has_past and not has_current
    return False


def _parse_line(line: str, *, source: str, line_no: int) -> Rule:
    parts = line.split(None, 1)
    action = parts[0].casefold()
    if action not in {"keep", "toss"}:
        raise RuleError(
            f"{source}:{line_no}: rules start with keep or toss, not {parts[0]!r}"
        )
    rest = parts[1] if len(parts) > 1 else ""
    fields = _fields(rest, source=source, line_no=line_no)
    if not fields:
        raise RuleError(
            f"{source}:{line_no}: add a name, path, extension, category, semester, age, or size"
        )
    semester = fields.get("semester")
    if semester is not None:
        semester = semester.casefold()
        if semester not in {"past", "current"}:
            raise RuleError(f"{source}:{line_no}: semester must be past or current")
    category = fields.get("category")
    if category is not None:
        category = category.casefold()
        if category not in CATEGORIES:
            known = ", ".join(CATEGORIES)
            raise RuleError(f"{source}:{line_no}: unknown category {category!r}. Known: {known}")
    ext = fields.get("ext")
    if ext is not None:
        ext = _normalize_ext(ext)
    return Rule(
        action=action,
        source=source,
        line_no=line_no,
        text=line,
        name=fields.get("name"),
        path=fields.get("path"),
        ext=ext,
        category=category,
        semester=semester,
        older_than_days=_days(fields.get("older-than"), source, line_no, "older-than"),
        newer_than_days=_days(fields.get("newer-than"), source, line_no, "newer-than"),
        larger_than_bytes=_size(fields.get("larger-than"), source, line_no),
    )


def _fields(rest: str, *, source: str, line_no: int) -> dict[str, str]:
    matches = list(_KEY_RE.finditer(rest))
    if not matches and rest.strip():
        raise RuleError(f"{source}:{line_no}: expected pairs like name: *.pdf")
    fields: dict[str, str] = {}
    for index, match in enumerate(matches):
        key = match.group(1).casefold()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(rest)
        value = rest[start:end].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
        if not value:
            raise RuleError(f"{source}:{line_no}: {key} has no value")
        if key in fields:
            raise RuleError(f"{source}:{line_no}: {key} is listed twice")
        if key not in _KEYS:
            raise RuleError(f"{source}:{line_no}: unknown setting {key}")
        fields[key] = value
    return fields


def _days(value: str | None, source: str, line_no: int, label: str) -> int | None:
    if value is None:
        return None
    raw = value.strip().casefold()
    if raw.endswith("d"):
        raw = raw[:-1].strip()
    try:
        number = int(raw)
    except ValueError as exc:
        raise RuleError(f"{source}:{line_no}: {label} must look like 14d") from exc
    if number < 0:
        raise RuleError(f"{source}:{line_no}: {label} must be zero or more")
    return number


def _size(value: str | None, source: str, line_no: int) -> int | None:
    if value is None:
        return None
    raw = value.strip().casefold().replace(" ", "")
    for unit in ("tb", "gb", "mb", "kb", "b"):
        if raw.endswith(unit):
            number = raw[: -len(unit)]
            scale = _SIZE_UNITS[unit]
            break
    else:
        number = raw
        scale = 1
    try:
        amount = float(number)
    except ValueError as exc:
        raise RuleError(f"{source}:{line_no}: larger-than must look like 100MB") from exc
    if amount < 0:
        raise RuleError(f"{source}:{line_no}: larger-than must be zero or more")
    return int(amount * scale)


def _normalize_ext(value: str) -> str:
    text = value.strip().casefold()
    if not text.startswith("."):
        text = "." + text
    return text


def _glob(pattern: str, text: str) -> bool:
    return fnmatch.fnmatchcase(text.casefold(), pattern.casefold())


def _path_glob(pattern: str, path: str) -> bool:
    text = path.replace("\\", "/")
    wanted = pattern.replace("\\", "/")
    if _glob(wanted, text):
        return True
    if not text.endswith("/") and _glob(wanted, text + "/"):
        return True
    return False
