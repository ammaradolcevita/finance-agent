"""Reads and checks a categories YAML file (see examples/categories.example.yaml).

Pure logic, no database, so it is easy to test.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

VALID_KINDS = {"regular", "structural"}


@dataclass(frozen=True)
class CategorySpec:
    name: str
    kind: str


def parse_categories(data: object) -> list[CategorySpec]:
    """Takes the parsed YAML → checks every entry → returns a clean list, or raises ValueError."""
    if not isinstance(data, dict) or not isinstance(data.get("categories"), list):
        raise ValueError("the file must have a top-level 'categories:' list")

    result: list[CategorySpec] = []
    seen: set[str] = set()
    for number, entry in enumerate(data["categories"], start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {number}: must look like {{name: ..., kind: ...}}")
        name = str(entry.get("name") or "").strip()
        kind = str(entry.get("kind") or "regular").strip()
        if not name:
            raise ValueError(f"entry {number}: name is empty")
        if kind not in VALID_KINDS:
            raise ValueError(f"entry {number}: kind must be 'regular' or 'structural'")
        if name.lower() in seen:
            raise ValueError(f"entry {number}: duplicate category name")
        seen.add(name.lower())
        result.append(CategorySpec(name=name, kind=kind))
    return result


def load_categories_file(path: str | Path) -> list[CategorySpec]:
    """Takes a file path → reads the YAML → returns the checked categories."""
    with open(path, encoding="utf-8") as f:
        return parse_categories(yaml.safe_load(f))
