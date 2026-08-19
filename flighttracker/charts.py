"""Hand-rolled inline SVG charts.

No plotting library and no external assets: the dashboard has to work as a
single file, opened from disk or served from GitHub Pages, with nothing to
fetch. Colours come from CSS custom properties defined once by the page, so
both themes are handled in one place rather than per mark.
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


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    tip: str
    series: int = 1
    note: Optional[str] = None


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


def line_chart(
    points: Sequence[Point],
    *,
    currency: str = "",
    reference: Optional[float] = None,
    reference_label: str = "median",
    x_labels: Sequence[tuple[float, str]] = (),
    height: int = 220,
    width: int = 720,
) -> str:
    """A single-series line. One series needs no legend — the heading names it."""
    if len(points) < 2:
        return _empty("Not enough runs yet to draw a line.")

    pad_l, pad_r, pad_t, pad_b = 56, 16, 14, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    xs = [p.x for p in points]
    ys = [p.y for p in points]
    lo_y, hi_y = min(ys), max(ys)
    if reference is not None:
        lo_y, hi_y = min(lo_y, reference), max(hi_y, reference)
    if hi_y == lo_y:
        lo_y, hi_y = lo_y - 1, hi_y + 1
    grid = gridlines(lo_y, hi_y)

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

    path = " ".join(
        f"{'M' if index == 0 else 'L'}{px(p.x):.1f} {py(p.y):.1f}"
        for index, p in enumerate(points)
    )
    parts.append(f'<path class="series" d="{path}"/>')

    for point in points:
        if point.highlight:
            parts.append(
                f'<circle class="marker highlight" cx="{px(point.x):.1f}" '
                f'cy="{py(point.y):.1f}" r="5"/>'
            )

    # Hit targets wider than the marks, so the tooltip is easy to catch.
    slot = plot_w / max(len(points) - 1, 1)
    for point in points:
        parts.append(
            f'<rect class="hit" x="{px(point.x) - slot / 2:.1f}" y="{pad_t}" '
            f'width="{slot:.1f}" height="{plot_h}" data-tip="{escape(point.tip)}"/>'
        )

    # The first and last labels are anchored inwards; centred on the axis ends
    # they would hang off the edge of the viewBox and get clipped.
    for index, (x, label) in enumerate(x_labels):
        if index == 0:
            anchor = "start"
        elif index == len(x_labels) - 1:
            anchor = "end"
        else:
            anchor = "middle"
        parts.append(
            f'<text class="tick" x="{px(x):.1f}" y="{height - 8}" '
            f'text-anchor="{anchor}">{escape(label)}</text>'
        )

    parts.append(
        f'<line class="axis" x1="{pad_l}" y1="{pad_t + plot_h}" '
        f'x2="{width - pad_r}" y2="{pad_t + plot_h}"/></svg>'
    )
    return "".join(parts)


def bar_chart(
    bars: Sequence[Bar],
    *,
    currency: str = "",
    height_per_bar: int = 26,
    width: int = 720,
) -> str:
    """Horizontal bars: magnitude across a handful of named dates."""
    if not bars:
        return _empty("No priced dates in the latest run.")

    pad_l, pad_r, pad_t = 96, 76, 8
    height = pad_t * 2 + height_per_bar * len(bars)
    plot_w = width - pad_l - pad_r
    top = max(b.value for b in bars) or 1

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="none">'
    ]
    for index, bar in enumerate(bars):
        y = pad_t + index * height_per_bar
        bar_h = height_per_bar - 8
        length = max(bar.value / top * plot_w, 2)
        parts.append(
            f'<text class="tick" x="{pad_l - 10}" y="{y + bar_h - 3}" '
            f'text-anchor="end">{escape(bar.label)}</text>'
            f'<rect class="bar series-{bar.series}" x="{pad_l}" y="{y}" '
            f'width="{length:.1f}" height="{bar_h}" rx="4" '
            f'data-tip="{escape(bar.tip)}"/>'
            f'<text class="value" x="{pad_l + length + 8:.1f}" '
            f'y="{y + bar_h - 3}">{escape(money(bar.value, currency))}'
            f'{(" · " + escape(bar.note)) if bar.note else ""}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _empty(message: str) -> str:
    return f'<p class="empty">{escape(message)}</p>'
