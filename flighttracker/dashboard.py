"""A self-contained HTML page: one chart per watch, and nothing else.

Deliberately small. Each watch gets its price so far and where it is projected
to go, and that is the whole page — no tiles, no bullet lists, no secondary
charts. The reasoning behind a buy signal still exists, in the digest and in
`flighttracker signals`; this page is for the one question you actually look at
a chart to answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from sqlite3 import Connection
from statistics import median as statistics_median

from .charts import BandPoint, Point, history_and_forecast, money
from .config import Config
from .forecast import project
from .model import fit as fit_model
from .run import evaluate_only
from .signals import Verdict
from .store import horizon_samples, parse_iso, run_history, source_counts

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
  --good: #0ca30c;
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
    --good: #0ca30c;
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
  --good: #0ca30c;
  --chip: rgba(255, 255, 255, 0.06);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 28px 20px 56px;
  background: var(--page);
  color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 780px; margin: 0 auto; }
h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 16px; margin: 0; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 22px; }

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 18px 18px;
  margin: 0 0 14px;
}
.card.flagged { border-left: 3px solid var(--good); }

.head {
  display: flex; flex-wrap: wrap; gap: 8px;
  align-items: baseline; justify-content: space-between;
}
.route { color: var(--muted); font-size: 13px; margin: 2px 0 0; }
.now { font-size: 24px; font-weight: 650; margin: 10px 0 0; }
.now .when { font-size: 13px; font-weight: 400; color: var(--muted); }

.pill {
  font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; padding: 3px 9px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--chip); color: var(--ink-2);
  white-space: nowrap;
}
.pill.buy { background: var(--good); border-color: transparent; color: #fff; }

.chart { width: 100%; height: auto; display: block; margin: 8px 0 0;
         overflow: visible; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.reference { stroke: var(--axis); stroke-width: 1; stroke-dasharray: 4 4; }
.reference-label { fill: var(--muted); }
.tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.series { fill: none; stroke: var(--series-1); stroke-width: 2;
          stroke-linejoin: round; stroke-linecap: round; }
.series.projected { stroke-dasharray: 6 5; opacity: 0.9; }
.band { fill: var(--series-1); opacity: 0.11; stroke: none; }
.marker { fill: var(--series-1); stroke: var(--surface); stroke-width: 2; }
.marker.highlight { fill: var(--good); }
.marker.projected-end { fill: var(--surface); stroke: var(--series-1); }
.hit { fill: transparent; }
.hit:hover { fill: var(--chip); }

.legend { display: flex; gap: 16px; flex-wrap: wrap; margin: 10px 0 0;
          font-size: 12px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.key { width: 18px; height: 0; border-top: 2px solid var(--series-1);
       display: inline-block; }
.key.dashed { border-top-style: dashed; }
.key.band { height: 10px; border: none; background: var(--series-1);
            opacity: 0.22; border-radius: 2px; }

.note { color: var(--muted); font-size: 12px; margin: 10px 0 0; }
.empty { color: var(--muted); font-size: 13px; margin: 10px 0 0; }
details { margin: 10px 0 0; }
summary { cursor: pointer; color: var(--ink-2); font-size: 12px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 0; font-size: 13px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border);
         font-variant-numeric: tabular-nums; }
th { color: var(--muted); font-weight: 600; }
.scroll { overflow-x: auto; }
a { color: var(--series-1); }
:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { #tip { transition: none; } }
footer { color: var(--muted); font-size: 12px; margin: 24px 0 0; }

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
    verdicts = list(evaluate_only(config, conn, now))
    samples = horizon_samples(conn)

    parts = [
        f"<style>{STYLE}</style>",
        '<div class="wrap">',
        "<h1>Flight Price Watch</h1>",
        f'<p class="sub">Last checked '
        f"{escape(now.strftime('%d %b %Y, %H:%M UTC'))}</p>",
    ]
    model = fit_model(samples)
    for verdict in verdicts:
        parts.append(_card(conn, config, verdict, model, now))
    parts.append(
        "<footer>Prices are scraped from Google Flights and are a snapshot, not "
        "a quote. The projection is a description of the data so far, not a "
        "promise — the shaded band is how much it could be wrong by.</footer>"
    )
    parts.append('</div><div id="tip"></div>')
    parts.append(f"<script>{SCRIPT}</script>")
    return "\n".join(parts)


def render_document(conn: Connection, config: Config, now: datetime) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Flight Price Watch</title>\n</head>\n<body>\n"
        + render_body(conn, config, now)
        + "\n</body>\n</html>\n"
    )


def _card(conn, config, verdict: Verdict, model, now: datetime) -> str:
    watch = verdict.watch
    currency = verdict.currency
    history = run_history(conn, watch.id)

    pill = '<span class="pill buy">Buy signal</span>' if verdict.flagged else ""
    tone = " flagged" if verdict.flagged else ""

    parts = [
        f'<section class="card{tone}">',
        '<div class="head"><div>',
        f"<h2>{escape(watch.name)}</h2>",
        f'<p class="route">{escape(watch.route)} · {escape(watch.cabin)}</p>',
        f"</div>{pill}</div>",
    ]

    if verdict.price is not None:
        when = (
            f' <span class="when">· {escape(verdict.best_depart)}'
            + (f" → {escape(verdict.best_return)}" if verdict.best_return else "")
            + "</span>"
        )
        parts.append(f'<p class="now">{money(verdict.price, currency)}{when}</p>')
    else:
        parts.append('<p class="empty">No price returned in the latest run.</p>')

    projection = project(
        history, watch, model, config.settings, now,
        price=verdict.price, currency=currency,
    )
    parts.append(_chart(history, projection, currency))
    parts.append(
        '<div class="legend">'
        '<span><i class="key"></i>Recorded</span>'
        '<span><i class="key dashed"></i>Projected</span>'
        '<span><i class="key band"></i>Could be anywhere in here</span></div>'
        if projection.usable
        else ""
    )
    if projection.note:
        parts.append(f'<p class="note">{escape(projection.note)}</p>')

    parts.append(_table(history, conn, watch.id, currency))
    parts.append("</section>")
    return "".join(parts)


def _chart(history, projection, currency: str) -> str:
    if len(history) < 2:
        return '<p class="empty">Not enough runs yet to draw a line.</p>'

    origin = parse_iso(history[0].timestamp)
    lowest = min(p.price for p in history)
    # Only one dot. On a fare that has not moved, every point ties the minimum,
    # and marking them all turns the whole line green and says nothing.
    best_at = max(
        index for index, p in enumerate(history) if p.price <= lowest
    )

    def offset(moment: datetime) -> float:
        return (moment - origin).total_seconds() / 86400.0

    actual = [
        Point(
            x=offset(parse_iso(point.timestamp)),
            y=point.price,
            tip=(
                f"{parse_iso(point.timestamp):%d %b %H:%M} · "
                f"{money(point.price, currency)}"
                + (" · lowest so far" if index == best_at else "")
            ),
            highlight=index == best_at,
        )
        for index, point in enumerate(history)
    ]

    predicted, band = [], []
    if projection.points:
        # Anchor the band to the last reading with zero width, so uncertainty
        # visibly grows out of what is known rather than appearing detached.
        band.append(
            BandPoint(x=actual[-1].x, low=actual[-1].y, high=actual[-1].y)
        )
    for step in projection.points:
        moment = datetime.combine(step.day, datetime.min.time(), tzinfo=origin.tzinfo)
        x = offset(moment)
        predicted.append(
            Point(
                x=x,
                y=step.price,
                tip=(
                    f"{step.day:%d %b} · projected {money(step.price, currency)} "
                    f"({money(step.low, currency)} to {money(step.high, currency)})"
                ),
            )
        )
        band.append(BandPoint(x=x, low=step.low, high=step.high))

    # Spaced across the whole x range rather than pinned to the readings. A
    # few days of history beside months of projection puts the first and last
    # observations within pixels of each other, and the labels collide.
    span_end = (predicted[-1].x if predicted else actual[-1].x)
    labels = [
        (
            position,
            (origin + timedelta(days=position)).strftime("%d %b"),
        )
        for position in (
            actual[0].x,
            actual[0].x + (span_end - actual[0].x) / 2,
            span_end,
        )
    ]

    return history_and_forecast(
        actual,
        predicted,
        band,
        currency=currency,
        reference=float(statistics_median([p.price for p in history])),
        x_labels=labels,
    )


def _table(history, conn, watch_id: str, currency: str) -> str:
    """Kept, collapsed: a chart that cannot be read as numbers is not accessible."""
    if not history:
        return ""
    sources = source_counts(conn, watch_id)
    imported = sum(runs for name, runs in sources.items() if name != "observed")
    label = f"{len(history)} runs"
    if imported:
        label += f", {imported} imported"

    rows = "".join(
        f"<tr><td>{escape(parse_iso(p.timestamp).strftime('%Y-%m-%d %H:%M'))}</td>"
        f"<td>{escape(money(p.price, currency))}</td></tr>"
        for p in reversed(history[-40:])
    )
    return (
        f"<details><summary>Show the numbers ({label})</summary>"
        f'<div class="scroll"><table><thead><tr><th>Run (UTC)</th>'
        f"<th>Cheapest</th></tr></thead><tbody>{rows}</tbody></table></div></details>"
    )
