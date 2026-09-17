"""Adding and removing watches in the watchlist file.

Editing the file as *text* rather than round-tripping it through a YAML parser
is the whole point: the watchlist is heavily commented, and those comments are
the only documentation of what each setting does. `yaml.safe_load` followed by
`yaml.safe_dump` would silently delete every one of them, so a watch is added
by appending a block and removed by deleting its lines.

Every edit is validated by re-parsing the result before it is written. A file
that would not load is never saved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from .config import load_config
from .errors import ConfigError

WATCHES_KEY = "watches:"
ITEM = "  - "
CABINS = ("economy", "premium-economy", "business", "first")


@dataclass(frozen=True)
class NewWatch:
    """What a caller has to supply to start tracking a trip."""

    id: str
    origin: str
    destination: str
    depart: tuple[date, ...]
    returns: Optional[tuple[date, ...]] = None
    cabin: str = "economy"
    label: Optional[str] = None
    max_price: Optional[float] = None
    max_stops: Optional[int] = None
    adults: int = 1
    trip_length_nights: Optional[tuple[int, int]] = None

    def to_yaml(self) -> str:
        """The watch as a YAML list item, at the indentation the file uses."""
        lines = [f"{ITEM}id: {self.id}"]

        def field(name: str, value: object) -> None:
            lines.append(f"    {name}: {value}")

        if self.label:
            field("label", _quote(self.label))
        field("origin", self.origin)
        field("destination", self.destination)
        field("depart_date_range", _dates(self.depart))
        if self.returns:
            field("return_date_range", _dates(self.returns))
        if self.trip_length_nights:
            low, high = self.trip_length_nights
            field("trip_length_nights", f"[{low}, {high}]")
        if self.cabin != "economy":
            field("cabin", self.cabin)
        if self.max_stops is not None:
            field("max_stops", self.max_stops)
        if self.adults != 1:
            lines.append("    passengers:")
            lines.append(f"      adults: {self.adults}")
        if self.max_price is not None:
            field("max_price_alert", _number(self.max_price))
        return "\n".join(lines)


def _dates(days: Sequence[date]) -> str:
    if len(days) == 1:
        return days[0].isoformat()
    return f"[{days[0].isoformat()}, {days[-1].isoformat()}]"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _quote(text: str) -> str:
    """Quote a label only when YAML would otherwise misread it."""
    if text != text.strip() or any(ch in text for ch in ":#[]{},&*!|>'\"%@`"):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _section(lines: list[str]) -> tuple[int, int]:
    """Where the `watches:` list starts and ends, as a half-open line range."""
    start = next(
        (i for i, line in enumerate(lines) if line.rstrip() == WATCHES_KEY), None
    )
    if start is None:
        raise ConfigError(f"No `{WATCHES_KEY}` key in the watchlist.")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line[0].isspace() and not line.startswith("#"):
            end = index
            break
    return start + 1, end


def _blocks(lines: list[str], start: int, end: int) -> list[tuple[int, int, str]]:
    """Each watch in the section, as (first line, last line + 1, its id).

    The first line is the earliest comment line attached to the watch, so
    removing a watch takes its comments with it rather than orphaning them
    above the next one.
    """
    heads = [i for i in range(start, end) if lines[i].startswith(ITEM)]
    out = []
    for position, head in enumerate(heads):
        stop = heads[position + 1] if position + 1 < len(heads) else end
        # Trailing blank lines belong to the gap, not to this watch.
        while stop > head + 1 and not lines[stop - 1].strip():
            stop -= 1

        first = head
        while first > start and lines[first - 1].lstrip().startswith("#"):
            first -= 1

        watch_id = _id_of(lines[head:stop])
        out.append((first, stop, watch_id))
    return out


def _id_of(block: list[str]) -> str:
    for line in block:
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith("id:"):
            return stripped[3:].strip().strip("'\"")
    return ""


def watch_ids(path: Path | str) -> list[str]:
    lines = Path(path).read_text().splitlines()
    start, end = _section(lines)
    return [found for _, _, found in _blocks(lines, start, end) if found]


def add(path: Path | str, watch: NewWatch) -> None:
    """Append a watch to the file, leaving everything else byte for byte."""
    path = Path(path)
    lines = path.read_text().splitlines()
    start, end = _section(lines)

    existing = [found for _, _, found in _blocks(lines, start, end)]
    if watch.id in existing:
        raise ConfigError(
            f"A watch called {watch.id!r} is already in the watchlist. "
            "Give this one a different id, or remove that one first."
        )

    block = watch.to_yaml().splitlines()
    # Sit the new watch after the last one, before anything that follows the
    # list — a trailing comment, or another top-level key.
    blocks = _blocks(lines, start, end)
    insert = blocks[-1][1] if blocks else start
    updated = lines[:insert] + ([""] if blocks else []) + block + lines[insert:]
    _write(path, updated, watch.id)


def remove(path: Path | str, watch_id: str) -> None:
    """Delete a watch and its comments. Its recorded prices are untouched."""
    path = Path(path)
    lines = path.read_text().splitlines()
    start, end = _section(lines)

    blocks = _blocks(lines, start, end)
    match = next((b for b in blocks if b[2] == watch_id), None)
    if match is None:
        known = ", ".join(sorted(b[2] for b in blocks if b[2])) or "none"
        raise ConfigError(
            f"No watch called {watch_id!r} in the watchlist. Currently: {known}."
        )

    first, stop, _ = match
    # Take the blank line that separated it from its neighbour, so removing
    # watches one at a time does not leave a growing stack of gaps.
    while first > start and not lines[first - 1].strip():
        first -= 1
    _write(path, lines[:first] + lines[stop:], None)


def _write(path: Path, lines: list[str], expect: Optional[str]) -> None:
    """Save only if the result still parses — and still says what we meant."""
    text = "\n".join(lines).rstrip("\n") + "\n"
    original = path.read_text()
    path.write_text(text)
    try:
        config = load_config(path)
    except Exception:
        path.write_text(original)
        raise
    if expect is not None and not any(w.id == expect for w in config.watches):
        path.write_text(original)
        raise ConfigError(
            f"Adding {expect!r} did not take effect — the watchlist was left "
            "unchanged. Its `watches:` list may be laid out unusually; edit "
            "the file by hand."
        )
