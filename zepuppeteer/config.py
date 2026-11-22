"""Small configuration helpers; TOML is built in starting with Python 3.11."""

import json
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def read_toml(path: str | Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read TOML configuration {path}: {error}") from error


def json_text(value: Any) -> str:
    """Reject nonportable outputs (dates, Paths, NaN, non-string object keys)."""
    def check(item):
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings.")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError(f"Value must be JSON-compatible, got {type(item).__name__}.")

    check(value)
    return json.dumps(value, allow_nan=False, ensure_ascii=False)
