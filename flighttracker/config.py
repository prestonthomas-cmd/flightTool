"""Reading and validating the watchlist file.

The watchlist is the one file a user edits by hand, so every problem it can
have is reported at once, with the offending watch named, rather than failing
on the first one and making them run the tool again to find the next.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from .dates import DateCombination, coerce_date, combinations, expand_range
from .errors import ConfigError

CABINS = {
    "economy": "economy",
    "premium-economy": "premium-economy",
    "premium economy": "premium-economy",
    "premium_economy": "premium-economy",
    "premium": "premium-economy",
    "business": "business",
    "first": "first",
}

IATA = re.compile(r"^[A-Z]{3}$")
WATCH_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


@dataclass(frozen=True)
class Passengers:
    adults: int = 1
    children: int = 0
    infants_in_seat: int = 0
    infants_on_lap: int = 0

    @property
    def total(self) -> int:
        return (
            self.adults + self.children + self.infants_in_seat + self.infants_on_lap
        )


@dataclass(frozen=True)
class Settings:
    """Run-wide knobs. Every one of these has a workable default."""

    db_path: Path = Path("data/prices.db")
    currency: str = "USD"
    # Google Flights has no SLA and no published rate limit, so searches are
    # spaced out and jittered rather than fired off back to back.
    request_delay_seconds: float = 4.0
    request_jitter_seconds: float = 2.0
    max_retries: int = 3
    retry_backoff_seconds: float = 5.0
    # How many runs a watch needs before its own history is worth comparing
    # against. Below this only a `max_price_alert` ceiling can fire.
    min_observations: int = 20
    percentile: float = 20.0
    max_combinations: int = 60
    alert_cooldown_hours: float = 48.0
    # A price that keeps sitting at its all-time low would otherwise alert every
    # single run. Within the cooldown, only a drop of at least this fraction
    # below the last alerted price is worth another email.
    alert_improvement: float = 0.03

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path


@dataclass(frozen=True)
class Watch:
    id: str
    origin: str
    destination: str
    depart_dates: tuple[date, ...]
    return_dates: Optional[tuple[date, ...]] = None
    trip_length_nights: Optional[tuple[int, int]] = None
    cabin: str = "economy"
    max_price_alert: Optional[float] = None
    passengers: Passengers = field(default_factory=Passengers)
    max_stops: Optional[int] = None
    label: Optional[str] = None
    min_observations: Optional[int] = None
    percentile: Optional[float] = None

    @property
    def one_way(self) -> bool:
        return self.return_dates is None

    @property
    def name(self) -> str:
        return self.label or self.id

    @property
    def route(self) -> str:
        arrow = "->" if self.one_way else "<->"
        return f"{self.origin} {arrow} {self.destination}"

    def searches(self) -> list[DateCombination]:
        return combinations(
            self.depart_dates, self.return_dates, self.trip_length_nights
        )

    def threshold_percentile(self, settings: Settings) -> float:
        return self.percentile if self.percentile is not None else settings.percentile

    def required_observations(self, settings: Settings) -> int:
        if self.min_observations is not None:
            return self.min_observations
        return settings.min_observations


@dataclass(frozen=True)
class Config:
    settings: Settings
    watches: tuple[Watch, ...]
    source: Optional[Path] = None


def load_config(path: os.PathLike | str) -> Config:
    """Parse a watchlist file, or raise `ConfigError` listing everything wrong."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"watchlist file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} should contain a mapping with a `watches:` key")

    problems: list[str] = []
    settings = _parse_settings(raw.get("settings") or {}, problems, path)

    entries = raw.get("watches")
    if entries is None:
        problems.append("no `watches:` key — nothing to track")
        entries = []
    elif not isinstance(entries, list):
        problems.append("`watches:` should be a list")
        entries = []
    elif not entries:
        problems.append("`watches:` is empty — nothing to track")

    watches: list[Watch] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        watch = _parse_watch(entry, index, problems, settings)
        if watch is None:
            continue
        if watch.id in seen:
            problems.append(f"watch id {watch.id!r} is used more than once")
            continue
        seen.add(watch.id)
        watches.append(watch)

    if problems:
        raise ConfigError(problems)

    return Config(settings=settings, watches=tuple(watches), source=path)


def _parse_settings(raw: Any, problems: list[str], source: Path) -> Settings:
    if not isinstance(raw, dict):
        problems.append("`settings:` should be a mapping")
        return Settings()

    known = {f for f in Settings.__dataclass_fields__}
    values: dict[str, Any] = {}

    for key, value in raw.items():
        if key not in known:
            problems.append(f"settings: unknown key {key!r}")
            continue
        try:
            values[key] = _coerce_setting(key, value, source)
        except ValueError as exc:
            problems.append(f"settings.{key}: {exc}")

    percentile = values.get("percentile")
    if percentile is not None and not 0 < percentile < 100:
        problems.append("settings.percentile: should be between 0 and 100 exclusive")
        values.pop("percentile")

    for key in ("min_observations", "max_retries", "max_combinations"):
        if key in values and values[key] < 1:
            problems.append(f"settings.{key}: should be at least 1")
            values.pop(key)

    return Settings(**values)


def _coerce_setting(key: str, value: Any, source: Path):
    if key == "db_path":
        candidate = Path(str(value)).expanduser()
        # A relative path is relative to the watchlist file, so the tool behaves
        # the same wherever it is run from — cron included.
        return candidate if candidate.is_absolute() else (source.parent / candidate)
    if key == "currency":
        text = str(value).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", text):
            raise ValueError(f"expected a 3-letter currency code, got {value!r}")
        return text
    if key in {"min_observations", "max_retries", "max_combinations"}:
        return _as_int(value)
    return _as_float(value)


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a whole number, got {value!r}")
    number = float(value)
    if number != int(number):
        raise ValueError(f"expected a whole number, got {value!r}")
    return int(number)


def _as_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a number, got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected a number, got {value!r}") from exc


def _parse_watch(
    raw: Any, index: int, problems: list[str], settings: Settings
) -> Optional[Watch]:
    where = f"watches[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: should be a mapping")
        return None

    watch_id = raw.get("id")
    if not isinstance(watch_id, str) or not WATCH_ID.match(watch_id):
        problems.append(
            f"{where}: needs an `id` of letters, digits, dashes or underscores"
        )
        return None
    where = f"watch {watch_id!r}"

    known = {
        "id",
        "origin",
        "destination",
        "depart_date_range",
        "return_date_range",
        "trip_length_nights",
        "cabin",
        "max_price_alert",
        "passengers",
        "max_stops",
        "label",
        "min_observations",
        "percentile",
    }
    for key in raw:
        if key not in known:
            problems.append(f"{where}: unknown key {key!r}")

    before = len(problems)

    origin = _airport(raw.get("origin"), f"{where}.origin", problems)
    destination = _airport(raw.get("destination"), f"{where}.destination", problems)
    if origin and destination and origin == destination:
        problems.append(f"{where}: origin and destination are both {origin}")

    depart = _date_range(raw.get("depart_date_range"), f"{where}.depart_date_range", problems)
    if depart is None:
        problems.append(f"{where}: `depart_date_range` is required")

    returns = None
    if raw.get("return_date_range") is not None:
        returns = _date_range(
            raw["return_date_range"], f"{where}.return_date_range", problems
        )

    nights = _trip_length(raw.get("trip_length_nights"), where, problems)

    cabin = "economy"
    if raw.get("cabin") is not None:
        key = str(raw["cabin"]).strip().lower()
        if key not in CABINS:
            problems.append(
                f"{where}.cabin: {raw['cabin']!r} is not one of "
                + ", ".join(sorted(set(CABINS.values())))
            )
        else:
            cabin = CABINS[key]

    ceiling = None
    if raw.get("max_price_alert") is not None:
        try:
            ceiling = _as_float(raw["max_price_alert"])
            if ceiling <= 0:
                problems.append(f"{where}.max_price_alert: should be above zero")
                ceiling = None
        except ValueError as exc:
            problems.append(f"{where}.max_price_alert: {exc}")

    passengers = _passengers(raw.get("passengers"), where, problems)

    max_stops = None
    if raw.get("max_stops") is not None:
        try:
            max_stops = _as_int(raw["max_stops"])
            if max_stops < 0:
                problems.append(f"{where}.max_stops: cannot be negative")
                max_stops = None
        except ValueError as exc:
            problems.append(f"{where}.max_stops: {exc}")

    min_obs = None
    if raw.get("min_observations") is not None:
        try:
            min_obs = _as_int(raw["min_observations"])
            if min_obs < 1:
                problems.append(f"{where}.min_observations: should be at least 1")
                min_obs = None
        except ValueError as exc:
            problems.append(f"{where}.min_observations: {exc}")

    percentile = None
    if raw.get("percentile") is not None:
        try:
            percentile = _as_float(raw["percentile"])
            if not 0 < percentile < 100:
                problems.append(f"{where}.percentile: should be between 0 and 100")
                percentile = None
        except ValueError as exc:
            problems.append(f"{where}.percentile: {exc}")

    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        problems.append(f"{where}.label: should be text")
        label = None

    if len(problems) > before or depart is None or not origin or not destination:
        return None

    watch = Watch(
        id=watch_id,
        origin=origin,
        destination=destination,
        depart_dates=tuple(depart),
        return_dates=tuple(returns) if returns is not None else None,
        trip_length_nights=nights,
        cabin=cabin,
        max_price_alert=ceiling,
        passengers=passengers,
        max_stops=max_stops,
        label=label,
        min_observations=min_obs,
        percentile=percentile,
    )

    searches = watch.searches()
    if not searches:
        problems.append(
            f"{where}: the date ranges produce no searches — check that the return "
            "range falls after the departure range and that `trip_length_nights` "
            "is reachable"
        )
        return None
    if len(searches) > settings.max_combinations:
        problems.append(
            f"{where}: {len(searches)} date combinations exceeds "
            f"max_combinations ({settings.max_combinations}). Narrow a range, set "
            "`trip_length_nights`, or raise the limit — every combination is a "
            "separate scrape on every run."
        )
        return None

    return watch


def _airport(value: Any, where: str, problems: list[str]) -> Optional[str]:
    if value is None:
        problems.append(f"{where}: required")
        return None
    code = str(value).strip().upper()
    if not IATA.match(code):
        problems.append(f"{where}: {value!r} is not a 3-letter IATA airport code")
        return None
    return code


def _date_range(value: Any, where: str, problems: list[str]) -> Optional[list[date]]:
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    if not 1 <= len(items) <= 2:
        problems.append(f"{where}: expected one date or a [start, end] pair")
        return None
    try:
        bounds = [coerce_date(item) for item in items]
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
        return None
    try:
        return expand_range(bounds)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
        return None


def _trip_length(
    value: Any, where: str, problems: list[str]
) -> Optional[tuple[int, int]]:
    if value is None:
        return None
    items = value if isinstance(value, list) else [value, value]
    if len(items) != 2:
        problems.append(
            f"{where}.trip_length_nights: expected a number of nights or a "
            "[min, max] pair"
        )
        return None
    try:
        low, high = (_as_int(item) for item in items)
    except ValueError as exc:
        problems.append(f"{where}.trip_length_nights: {exc}")
        return None
    if low < 1:
        problems.append(f"{where}.trip_length_nights: a trip is at least 1 night")
        return None
    if high < low:
        problems.append(f"{where}.trip_length_nights: max is below min")
        return None
    return (low, high)


def _passengers(value: Any, where: str, problems: list[str]) -> Passengers:
    if value is None:
        return Passengers()
    if isinstance(value, int) and not isinstance(value, bool):
        return Passengers(adults=value) if value >= 1 else Passengers()
    if not isinstance(value, dict):
        problems.append(f"{where}.passengers: expected a number of adults or a mapping")
        return Passengers()

    fields = {f for f in Passengers.__dataclass_fields__}
    counts: dict[str, int] = {}
    for key, raw_count in value.items():
        if key not in fields:
            problems.append(f"{where}.passengers: unknown key {key!r}")
            continue
        try:
            count = _as_int(raw_count)
        except ValueError as exc:
            problems.append(f"{where}.passengers.{key}: {exc}")
            continue
        if count < 0:
            problems.append(f"{where}.passengers.{key}: cannot be negative")
            continue
        counts[key] = count

    passengers = Passengers(**counts)
    if passengers.total < 1:
        problems.append(f"{where}.passengers: at least one passenger is required")
        return Passengers()
    return passengers


def format_problems(error: ConfigError, source: Optional[Path] = None) -> str:
    heading = f"{source}: " if source else ""
    return f"{heading}watchlist has {len(error.problems)} problem(s):\n{error}"
