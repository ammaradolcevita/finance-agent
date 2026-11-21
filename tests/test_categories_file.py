"""Tests for app/categories_file.py. No database needed."""

from pathlib import Path

import pytest

from app.categories_file import load_categories_file, parse_categories

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "categories.example.yaml"


def test_example_file_loads():
    categories = load_categories_file(EXAMPLE)
    assert len(categories) == 10
    assert {c.kind for c in categories} == {"regular", "structural"}


def test_kind_defaults_to_regular():
    [c] = parse_categories({"categories": [{"name": "Coffee"}]})
    assert c.kind == "regular"


def test_name_is_trimmed():
    [c] = parse_categories({"categories": [{"name": "  Coffee  ", "kind": "regular"}]})
    assert c.name == "Coffee"


@pytest.mark.parametrize(
    "data",
    [
        None,                                                      # empty file
        {"items": []},                                             # wrong top-level key
        {"categories": ["Coffee"]},                                # entry is not a mapping
        {"categories": [{"name": ""}]},                            # empty name
        {"categories": [{"name": "Coffee", "kind": "weekly"}]},    # unknown kind
        {"categories": [{"name": "Coffee"}, {"name": "coffee"}]},  # duplicate (any case)
    ],
)
def test_bad_files_are_rejected(data):
    with pytest.raises(ValueError):
        parse_categories(data)
