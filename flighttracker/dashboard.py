"""A self-contained HTML dashboard rendered from the stored price history.

Static output on purpose: it can be opened from disk, or published to GitHub
Pages by the same workflow that collects the prices, with nothing to run and
nothing to fetch.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from sqlite3 import Connection
from statistics import median as statistics_median
from typing import Optional

from .charts import Bar, Point, bar_chart, line_chart, money
from .config import Config
from .fetch import search_url
from .forecast import build_curve, bucket_label
from .holidays import describe as describe_holiday
from .holidays import is_peak
from .run import evaluate_only
from .signals import Verdict
from .store import (
    horizon_samples,
    observations_for_run,
    parse_iso,
    run_history,
    source_counts,
)

# From the reference palette: light and dark steps of the same hues, each
# validated against its own surface rather than flipped.
STYLE = """
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11, 11, 11, 0.10);
  --series-1: #2a78d6;
  --series-2: #eb6834;
  --good: #0ca30c;
  --critical: #d03b3b;
  --chip: rgba(11, 11, 11, 0.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --ink: #ffffff;
    --ink-2: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255, 255, 255, 0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --good: #0ca30c;
    --critical: #d03b3b;
    --chip: rgba(255, 255, 255, 0.06);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d;
  --surface: #1a1a19;
  --ink: #ffffff;
  --ink-2: #c3c2b7;
  --muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --border: rgba(255, 255, 255, 0.10);
  --series-1: #3987e5;
  --series-2: #d95926;
  --good: #0ca30c;
  --critical: #d03b3b;
  --chip: rgba(255, 255, 255, 0.06);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 28px 20px 64px;
  background: var(--page);
  color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 860px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 17px; margin: 0; letter-spacing: -0.01em; }
h3 { font-size: 13px; margin: 22px 0 8px; color: var(--ink-2); font-weight: 600; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 20px 20px;
  margin: 0 0 16px;
}
/* State is carried by the stripe as well as the pill, so a card wanting
   attention reads at a glance without stopping to read the label. */
.card.flagged { border-left: 3px solid var(--good); }
.card.stale { border-left: 3px solid var(--critical); }

.summary {
  display: grid; gap: 10px; margin: 0 0 22px;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
}
.summary .tile { background: var(--surface); }
.summary .v { font-size: 22px; }
.card-head {
  display: flex; flex-wrap: wrap; gap: 8px;
  align-items: baseline; justify-content: space-between;
}
.route { color: var(--muted); font-size: 13px; margin: 2px 0 0; }

.pill {
  font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; padding: 3px 9px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--chip); color: var(--ink-2);
  white-space: nowrap;
}
.pill.buy { background: var(--good); border-color: transparent; color: #fff; }

.tiles {
  display: grid; gap: 10px; margin: 16px 0 0;
  grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
}
.tile {
  border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
}
.tile .k {
  font-size: 11px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.04em;
}
.tile .v { font-size: 20px; font-weight: 650; margin-top: 2px; }
.tile .v.sm { font-size: 15px; font-weight: 600; }
.tile .d { font-size: 12px; color: var(--ink-2); }
.tile .d.down { color: var(--good); }
.tile .d.up { color: var(--critical); }

.reasons { margin: 14px 0 0; padding-left: 18px; }
.reasons li { margin: 2px 0; }

.outlook {
  margin: 14px 0 0; padding: 12px 14px; border-radius: 8px;
  background: var(--chip); border: 1px solid var(--border);
}
.outlook .k {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--muted);
}
.outlook .h { font-weight: 600; margin: 2px 0 0; }
.outlook ul { margin: 8px 0 0; padding-left: 18px; color: var(--ink-2); font-size: 13px; }

.chart { width: 100%; height: auto; display: block; margin: 6px 0 0; overflow: visible; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.reference { stroke: var(--axis); stroke-width: 1; stroke-dasharray: 4 4; }
.reference-label { fill: var(--muted); }
.tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.value { fill: var(--ink-2); font-size: 11px; font-variant-numeric: tabular-nums; }
.series { fill: none; stroke: var(--series-1); stroke-width: 2;
          stroke-linejoin: round; stroke-linecap: round; }
.marker { fill: var(--series-1); stroke: var(--surface); stroke-width: 2; }
.marker.highlight { fill: var(--good); }
.bar { fill: var(--series-1); stroke: var(--surface); stroke-width: 2; }
.bar.series-2 { fill: var(--series-2); }
.hit { fill: transparent; }
.hit:hover { fill: var(--chip); }
.bar:hover { opacity: 0.82; }

.legend { display: flex; gap: 14px; flex-wrap: wrap; margin: 10px 0 0;
          font-size: 12px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.swatch.s1 { background: var(--series-1); }
.swatch.s2 { background: var(--series-2); }

.empty { color: var(--muted); font-size: 13px; margin: 10px 0 0; }
details { margin: 12px 0 0; }
summary { cursor: pointer; color: var(--ink-2); font-size: 13px; }
table { border-collapse: collapse; width: 100%; margin: 10px 0 0; font-size: 13px; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border);
         font-variant-numeric: tabular-nums; }
th { color: var(--muted); font-weight: 600; }
.scroll { overflow-x: auto; }
a { color: var(--series-1); }
:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { #tip { transition: none; } }
footer { color: var(--muted); font-size: 12px; margin: 28px 0 0; }

#tip {
  position: fixed; pointer-events: none; opacity: 0; transition: opacity .08s;
  background: var(--ink); color: var(--page); padding: 6px 9px;
  border-radius: 6px; font-size: 12px; max-width: 280px; z-index: 9;
}
"""

SCRIPT = """
(function () {
  var tip = document.getElementById('tip');
  document.addEventListener('mousemove', function (event) {
    var target = event.target.closest('[data-tip]');
    if (!target) { tip.style.opacity = 0; return; }
    tip.textContent = target.getAttribute('data-tip');
    tip.style.opacity = 1;
    var box = tip.getBoundingClientRect();
    var x = Math.min(event.clientX + 14, window.innerWidth - box.width - 8);
    var y = Math.max(event.clientY - box.height - 10, 8);
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  });
})();
"""


def render_body(conn: Connection, config: Config, now: datetime) -> str:
    """The dashboard, as a fragment: a <style>, the content, and a <script>."""
    verdicts = list(evaluate_only(config, conn, now))
    flagged = [v for v in verdicts if v.flagged]

    parts = [
        f"<style>{STYLE}</style>",
        '<div class="wrap">',
        "<h1>Flight Price Watch</h1>",
        f'<p class="sub">{escape(_subtitle(verdicts, now))}</p>',
        _summary_strip(verdicts, flagged),
    ]

    for verdict in verdicts:
        parts.append(_watch_card(conn, verdict, now))

    parts.append(_horizon_section(conn, config))
    parts.append(
        "<footer>Prices are scraped from Google Flights and are a snapshot, not "
        "a quote. Forecasts are descriptive statistics over the history in this "
        "database — they annotate a price, they never decide it.</footer>"
    )
    parts.append("</div><div id=\"tip\"></div>")
    parts.append(f"<script>{SCRIPT}</script>")
    return "\n".join(parts)


def render_document(conn: Connection, config: Config, now: datetime) -> str:
    """The same dashboard as a complete page, for GitHub Pages or a local file."""
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Flight Price Watch</title>\n</head>\n<body>\n"
        + render_body(conn, config, now)
        + "\n</body>\n</html>\n"
    )


def _subtitle(verdicts, now: datetime) -> str:
    return f"Last checked {now.strftime('%d %b %Y, %H:%M UTC')}"


def _summary_strip(verdicts, flagged) -> str:
    # What a dashboard is for: the answer before the detail.
    tiles = [("Watches", str(len(verdicts)), "")]

    if flagged:
        cheapest = min(flagged, key=lambda v: v.price)
        tiles.append(
            (
                "Buy signals",
                str(len(flagged)),
                f'<div class="d down">cheapest {escape(cheapest.watch.name)}</div>',
            )
        )
        tiles.append(
            (
                "Best price now",
                money(cheapest.price, cheapest.currency),
                f'<div class="d">{escape(cheapest.best_depart or "")}</div>',
            )
        )
    else:
        tiles.append(("Buy signals", "None", '<div class="d">nothing flagged</div>'))

    upcoming = [v for v in verdicts if v.best_depart and v.price is not None]
    if upcoming:
        soonest = min(upcoming, key=lambda v: v.best_depart)
        tiles.append(
            (
                "Next departure",
                escape(soonest.best_depart),
                f'<div class="d">{escape(soonest.watch.name)}</div>',
            )
        )

    cells = "".join(
        f'<div class="tile"><div class="k">{escape(key)}</div>'
        f'<div class="v">{value}</div>{extra}</div>'
        for key, value, extra in tiles
    )
    return f'<div class="summary">{cells}</div>'


def _watch_card(conn: Connection, verdict: Verdict, now: datetime) -> str:
    watch = verdict.watch
    stats = verdict.stats
    currency = verdict.currency

    if verdict.flagged:
        pill = '<span class="pill buy">Buy signal</span>'
        tone = " flagged"
    elif verdict.price is None:
        pill = '<span class="pill">No price</span>'
        tone = " stale"
    elif verdict.suppressed and "building history" in verdict.suppressed:
        pill = f'<span class="pill">{escape(str(stats.count))} runs so far</span>'
        tone = ""
    else:
        pill = '<span class="pill">Watching</span>'
        tone = ""

    parts = [
        f'<section class="card{tone}">',
        '<div class="card-head"><div>',
        f"<h2>{escape(watch.name)}</h2>",
        f'<p class="route">{escape(watch.route)} · {escape(watch.cabin)} · '
        f"{watch.passengers.total} passenger(s)</p>",
        f"</div>{pill}</div>",
    ]

    # The verdict's stats deliberately exclude the run being judged, so the
    # tile is built from the full history instead — otherwise the card would
    # say "110 runs" directly above a table listing 111.
    history = run_history(conn, watch.id)
    sources = source_counts(conn, watch.id)
    imported = sum(runs for name, runs in sources.items() if name != "observed")
    parts.append(_tiles(verdict, history, now, imported))

    if verdict.reasons:
        parts.append('<ul class="reasons">')
        parts.extend(f"<li>{escape(r.detail)}</li>" for r in verdict.reasons)
        parts.append("</ul>")

    if verdict.suppressed:
        parts.append(f'<p class="empty">{escape(verdict.suppressed)}</p>')

    parts.append(_outlook(verdict))

    parts.append("<h3>Price history</h3>")
    parts.append(_history_chart(history, currency))
    parts.append(_history_table(history, currency))

    parts.append("<h3>Dates in the latest run</h3>")
    parts.append(_date_grid(conn, verdict))

    link = _link(verdict)
    if link:
        parts.append(f'<p style="margin:14px 0 0"><a href="{escape(link)}">Open in Google Flights</a></p>')

    parts.append("</section>")
    return "".join(parts)


def _tiles(verdict: Verdict, history, now: datetime, imported: int = 0) -> str:
    currency = verdict.currency
    stats = verdict.stats
    tiles = []

    if verdict.price is None:
        tiles.append(("Current", "—", ""))
    else:
        delta = ""
        if stats.median:
            gap = verdict.price - stats.median
            way = "down" if gap < 0 else "up"
            arrow = "↓" if gap < 0 else "↑"
            delta = (
                f'<div class="d {way}">{arrow} {money(abs(gap), currency)} '
                f"vs median</div>"
            )
        tiles.append(("Current", money(verdict.price, currency), delta))

    dates = verdict.best_depart or "—"
    if verdict.best_return:
        dates += f" → {verdict.best_return}"
    tiles.append(("Best dates", f'<span class="sm">{escape(dates)}</span>', ""))

    if verdict.best_depart:
        try:
            days = (date.fromisoformat(verdict.best_depart) - now.date()).days
            tiles.append(
                (
                    "Days out",
                    str(days),
                    f'<div class="d">{escape(describe_holiday(date.fromisoformat(verdict.best_depart)))}</div>',
                )
            )
        except ValueError:
            pass

    if history:
        prices = [p.price for p in history]
        detail = (
            f'<div class="d">low {money(min(prices), currency)} · '
            f"high {money(max(prices), currency)}</div>"
        )
        if imported:
            detail += (
                f'<div class="d">{imported} imported from price history</div>'
            )
        tiles.append(("Seen", f"{len(history)} runs", detail))

    cells = "".join(
        f'<div class="tile"><div class="k">{escape(key)}</div>'
        f'<div class="v">{value}</div>{extra}</div>'
        for key, value, extra in tiles
    )
    return f'<div class="tiles">{cells}</div>'


def _outlook(verdict: Verdict) -> str:
    forecast = verdict.forecast
    if forecast is None or not getattr(forecast, "known", False):
        return ""
    notes = "".join(f"<li>{escape(note)}</li>" for note in forecast.notes)
    baseline = ""
    if verdict.horizon_adjusted:
        baseline = (
            ' · <span title="Past prices re-based onto today\'s point in the '
            'booking window before judging">horizon-adjusted baseline</span>'
        )
    return (
        f'<div class="outlook"><div class="k">Outlook · '
        f"{escape(forecast.confidence)} confidence{baseline}</div>"
        f'<div class="h">{escape(forecast.headline)}</div>'
        f"<ul>{notes}</ul></div>"
    )


def _history_chart(history, currency: str) -> str:
    """The raw series, with a reference line drawn from that same raw series.

    Deliberately not the verdict's median: that one may have been re-based onto
    today's booking horizon, and a reference line on a different footing from
    the points around it is worse than no line at all.
    """
    if len(history) < 2:
        return '<p class="empty">Not enough runs yet to draw a line.</p>'

    prices = [p.price for p in history]
    lowest = min(prices)
    middle = float(statistics_median(prices))
    origin = parse_iso(history[0].timestamp)
    points = []
    for point in history:
        when = parse_iso(point.timestamp)
        points.append(
            Point(
                x=(when - origin).total_seconds() / 86400.0,
                y=point.price,
                tip=f"{when:%d %b %H:%M} · {money(point.price, currency)}",
                highlight=point.price <= lowest,
            )
        )

    labels = []
    for index in (0, len(points) // 2, len(points) - 1):
        when = parse_iso(history[index].timestamp)
        labels.append((points[index].x, when.strftime("%d %b")))

    return line_chart(
        points,
        currency=currency,
        reference=middle,
        reference_label="median",
        x_labels=labels,
    )


def _history_table(history, currency: str) -> str:
    if not history:
        return ""
    rows = "".join(
        f"<tr><td>{escape(parse_iso(p.timestamp).strftime('%Y-%m-%d %H:%M'))}</td>"
        f"<td>{escape(money(p.price, currency))}</td></tr>"
        for p in reversed(history[-40:])
    )
    return (
        f"<details><summary>Show the numbers ({len(history)} runs)</summary>"
        f'<div class="scroll"><table><thead><tr><th>Run (UTC)</th>'
        f"<th>Cheapest</th></tr></thead><tbody>{rows}</tbody></table></div></details>"
    )


def _date_grid(conn: Connection, verdict: Verdict) -> str:
    from .store import latest_run

    stamp = latest_run(conn, verdict.watch.id)
    if stamp is None:
        return '<p class="empty">No priced dates stored yet.</p>'

    cheapest: dict[str, tuple[float, Optional[str]]] = {}
    for row in observations_for_run(conn, verdict.watch.id, stamp):
        depart = row["depart_date"]
        if not depart:
            continue
        if depart not in cheapest or row["price"] < cheapest[depart][0]:
            cheapest[depart] = (row["price"], row["return_date"])

    if not cheapest:
        return '<p class="empty">No priced dates stored yet.</p>'

    bars = []
    any_peak = False
    for depart in sorted(cheapest):
        price, back = cheapest[depart]
        try:
            day = date.fromisoformat(depart)
        except ValueError:
            continue
        peak = is_peak(day)
        any_peak = any_peak or peak
        label = day.strftime("%a %d %b")
        tip = f"{depart}"
        if back:
            tip += f" → {back}"
        tip += f" · {money(price, verdict.currency)} · {describe_holiday(day)}"
        bars.append(
            Bar(
                label=label,
                value=price,
                tip=tip,
                series=2 if peak else 1,
                note="peak" if peak else None,
            )
        )

    chart = bar_chart(bars, currency=verdict.currency)
    if not any_peak:
        return chart
    # Two colours means identity must not rest on colour alone: the peak bars
    # carry a written label as well as the legend.
    return chart + (
        '<div class="legend">'
        '<span><i class="swatch s1"></i>Ordinary dates</span>'
        '<span><i class="swatch s2"></i>Holiday peak travel</span></div>'
    )


def _horizon_section(conn: Connection, config: Config) -> str:
    curve = build_curve(horizon_samples(conn), config.settings)
    if not curve.usable:
        return (
            '<section class="card"><h2>Booking-horizon curve</h2>'
            '<p class="empty">Not enough data yet. This curve pools every watch '
            "to show what prices typically do as departure approaches, and a "
            "bucket only counts once it holds observations from at least "
            f"{config.settings.horizon_min_watches} different watches — one "
            "watch alone cannot tell a horizon effect from the calendar."
            "</p></section>"
        )

    ordered = sorted(curve.buckets, key=lambda b: -b.low)
    points = [
        Point(
            x=float(index),
            y=bucket.index * 100,
            tip=(
                f"{bucket.label} before departure · index "
                f"{bucket.index * 100:.0f} · {bucket.samples} observations from "
                f"{bucket.watches} watches"
            ),
            highlight=bucket.index == min(b.index for b in ordered),
        )
        for index, bucket in enumerate(ordered)
    ]
    labels = [(float(i), bucket_label(b.low, b.high)) for i, b in enumerate(ordered)]

    return (
        '<section class="card"><h2>Booking-horizon curve</h2>'
        f'<p class="route">Pooled across {curve.scope} · {curve.samples} '
        "observations. Each price is divided by its own watch's median first, so "
        "100 means \"what this trip normally costs\". Departure is to the right."
        "</p>"
        + line_chart(points, reference=100.0, reference_label="typical", x_labels=labels)
        + "</section>"
    )


def _link(verdict: Verdict) -> Optional[str]:
    if not verdict.best_depart:
        return None
    try:
        depart = date.fromisoformat(verdict.best_depart)
        back = (
            date.fromisoformat(verdict.best_return) if verdict.best_return else None
        )
    except ValueError:
        return None
    return search_url(verdict.watch, depart, back)
