"""One hand-rolled inline SVG chart: a price series and where it is going.

No plotting library and no external assets — the page has to work as a single
file, opened from disk or served from GitHub Pages, with nothing to fetch.
Colours come from CSS custom properties defined once by the page, so both
themes are handled in one place rather than per mark.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from math import floor, log10
from typing import Optional, Sequence


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    tip: str
    highlight: bool = False


def nice_step(span: float, target: int) -> float:
    """A round-numbered gridline step: 1, 2, 2.5 or 5 times a power of ten."""
    if span <= 0:
        return 1.0
    raw = span / max(target, 1)
    magnitude = 10 ** floor(log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= multiple * magnitude:
            return multiple * magnitude
    return 10 * magnitude


def gridlines(low: float, high: float, target: int = 4) -> list[float]:
    """Round values lying inside [low, high].

    Strictly inside, so the plot can keep the data's own range rather than
    padding out to the next round number and wasting a third of the height.
    Callers must cope with an empty list.
    """
    if high <= low:
        return [low]
    step = nice_step(high - low, target)
    values = []
    value = floor(low / step) * step
    while value <= high + step * 1e-9:
        if value >= low - step * 1e-9:
            values.append(round(value, 6))
        value += step
    return values


def money(value: float, currency: str = "") -> str:
    prefix = f"{currency} " if currency else ""
    return f"{prefix}{value:,.0f}"


def _empty(message: str) -> str:
    return f'<p class="empty">{escape(message)}</p>'


@dataclass(frozen=True)
class BandPoint:
    x: float
    low: float
    high: float


def history_and_forecast(
    actual: Sequence[Point],
    predicted: Sequence[Point] = (),
    band: Sequence[BandPoint] = (),
    *,
    currency: str = "",
    reference: Optional[float] = None,
    reference_label: str = "median",
    x_labels: Sequence[tuple[float, str]] = (),
    height: int = 260,
    width: int = 720,
) -> str:
    """One price series, and where it is projected to go.

    The projection shares the actual line's hue and is drawn dashed inside a
    shaded band: it is the same quantity, known less well — not a second thing.
    Identity rests on line style and the band rather than on colour alone.
    """
    if len(actual) < 2:
        return _empty("Not enough runs yet to draw a line.")

    pad_l, pad_r, pad_t, pad_b = 60, 18, 16, 32
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    everything = list(actual) + list(predicted)
    ys = [p.y for p in everything]
    ys += [b.low for b in band] + [b.high for b in band]
    if reference is not None:
        ys.append(reference)
    lo_y, hi_y = min(ys), max(ys)
    if hi_y == lo_y:
        lo_y, hi_y = lo_y * 0.98, hi_y * 1.02
    grid = gridlines(lo_y, hi_y)

    xs = [p.x for p in everything]
    lo_x, hi_x = min(xs), max(xs)
    span_x = (hi_x - lo_x) or 1

    def px(x: float) -> float:
        return pad_l + (x - lo_x) / span_x * plot_w

    def py(y: float) -> float:
        return pad_t + (hi_y - y) / (hi_y - lo_y) * plot_h

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="none">'
    ]

    # The band sits under everything, so gridlines and marks stay readable.
    if band:
        top = " ".join(f"{px(b.x):.1f},{py(b.high):.1f}" for b in band)
        bottom = " ".join(
            f"{px(b.x):.1f},{py(b.low):.1f}" for b in reversed(list(band))
        )
        parts.append(f'<polygon class="band" points="{top} {bottom}"/>')

    for value in grid:
        y = py(value)
        parts.append(
            f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" '
            f'y2="{y:.1f}"/>'
            f'<text class="tick" x="{pad_l - 8}" y="{y + 4:.1f}" '
            f'text-anchor="end">{escape(money(value, currency))}</text>'
        )

    if reference is not None:
        y = py(reference)
        parts.append(
            f'<line class="reference" x1="{pad_l}" y1="{y:.1f}" '
            f'x2="{width - pad_r}" y2="{y:.1f}"/>'
            f'<text class="tick reference-label" x="{width - pad_r}" '
            f'y="{y - 6:.1f}" text-anchor="end">{escape(reference_label)}</text>'
        )

    def path_for(points) -> str:
        return " ".join(
            f"{'M' if i == 0 else 'L'}{px(p.x):.1f} {py(p.y):.1f}"
            for i, p in enumerate(points)
        )

    parts.append(f'<path class="series" d="{path_for(actual)}"/>')

    if predicted:
        # Joined to the last observed point, so the projection reads as a
        # continuation rather than a floating second line.
        joined = [actual[-1], *predicted]
        parts.append(f'<path class="series projected" d="{path_for(joined)}"/>')
        last = predicted[-1]
        parts.append(
            f'<circle class="marker projected-end" cx="{px(last.x):.1f}" '
            f'cy="{py(last.y):.1f}" r="4"/>'
        )

    for point in actual:
        if point.highlight:
            parts.append(
                f'<circle class="marker highlight" cx="{px(point.x):.1f}" '
                f'cy="{py(point.y):.1f}" r="5"/>'
            )

    slot = plot_w / max(len(everything) - 1, 1)
    for point in everything:
        parts.append(
            f'<rect class="hit" x="{px(point.x) - slot / 2:.1f}" y="{pad_t}" '
            f'width="{slot:.1f}" height="{plot_h}" data-tip="{escape(point.tip)}"/>'
        )

    for index, (x, label) in enumerate(x_labels):
        anchor = (
            "start" if index == 0
            else "end" if index == len(x_labels) - 1
            else "middle"
        )
        parts.append(
            f'<text class="tick" x="{px(x):.1f}" y="{height - 10}" '
            f'text-anchor="{anchor}">{escape(label)}</text>'
        )

    parts.append(
        f'<line class="axis" x1="{pad_l}" y1="{pad_t + plot_h}" '
        f'x2="{width - pad_r}" y2="{pad_t + plot_h}"/></svg>'
    )
    return "".join(parts)
