"""A self-contained HTML page: one chart per watch, and nothing else.

Deliberately small. Each watch gets its price so far and where it is projected
to go, and that is the whole page — no tiles, no bullet lists, no secondary
charts. The reasoning behind a buy signal still exists, in the digest and in
`flighttracker signals`; this page is for the one question you actually look at
a chart to answer.
"""

from __future__ import annotations

import os
import re
import subprocess
import json
from datetime import datetime, timedelta
from html import escape
from sqlite3 import Connection
from typing import Optional
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
  --bad: #c7391f;
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
    --bad: #f0654a;
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
  --bad: #f0654a;
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
.sub { color: var(--muted); font-size: 13px; margin: 0; }

.top { display: flex; flex-wrap: wrap; gap: 10px 16px;
       align-items: baseline; justify-content: space-between; margin: 0 0 22px; }
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

/* --- the add/remove interface ------------------------------------------- */
.manage {
  font: inherit; font-size: 13px; font-weight: 600; white-space: nowrap;
  padding: 7px 13px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--border); background: var(--chip); color: var(--ink);
}
.manage:hover { border-color: var(--series-1); color: var(--series-1); }

.drop {
  font: inherit; font-size: 18px; line-height: 1; cursor: pointer;
  padding: 2px 8px 4px; border-radius: 6px; color: var(--muted);
  border: 1px solid transparent; background: none;
}
.drop:hover { color: var(--bad); border-color: var(--border); }

dialog {
  border: 1px solid var(--border); border-radius: 12px; padding: 0;
  background: var(--surface); color: var(--ink); max-width: 420px; width: 92vw;
}
dialog::backdrop { background: rgba(0, 0, 0, 0.45); }
dialog .inner { padding: 20px; }
dialog h3 { margin: 0 0 4px; font-size: 17px; letter-spacing: -0.01em; }
dialog .why { color: var(--muted); font-size: 13px; margin: 0 0 16px; }

.field { margin: 0 0 12px; }
.field label { display: block; font-size: 12px; color: var(--ink-2);
               margin: 0 0 4px; font-weight: 600; }
.field input, .field select {
  font: inherit; font-size: 15px; width: 100%; padding: 8px 10px;
  border-radius: 7px; border: 1px solid var(--border);
  background: var(--page); color: var(--ink);
}
.pair { display: flex; gap: 10px; }
.pair .field { flex: 1; }
.check { display: flex; align-items: center; gap: 8px; margin: 0 0 14px;
         font-size: 14px; color: var(--ink-2); }
.check input { width: auto; }

.actions { display: flex; gap: 8px; justify-content: flex-end; margin: 18px 0 0; }
.actions button {
  font: inherit; font-size: 14px; font-weight: 600; cursor: pointer;
  padding: 8px 15px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--chip); color: var(--ink);
}
.actions .go { background: var(--series-1); border-color: transparent; color: #fff; }
.actions .go:disabled { opacity: 0.55; cursor: progress; }
.actions .danger { background: var(--bad); border-color: transparent; color: #fff; }

.said { font-size: 13px; margin: 14px 0 0; padding: 10px 12px; border-radius: 8px;
        background: var(--chip); color: var(--ink-2); }
.said.bad { color: var(--bad); }
.said:empty { display: none; }
dialog code { font-size: 12px; background: var(--chip); padding: 1px 5px;
              border-radius: 4px; }

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


# The page is a static file: it cannot edit the watchlist itself. What it can
# do is ask GitHub to run the workflow that does, which is a plain HTTPS call
# the browser makes on the reader's behalf. That needs a token, and a public
# page cannot hold a secret — so the reader supplies one and it is kept in
# their own browser, never in the repository.
DIALOGS = """
<dialog id="add-dialog"><form method="dialog" class="inner" id="add-form">
<h3>Add a flight</h3>
<p class="why">It starts collecting prices on the next run.</p>
<div class="pair">
  <div class="field"><label for="f-from">From</label>
    <input id="f-from" name="from" placeholder="JFK" maxlength="3" required
           autocapitalize="characters" autocomplete="off"></div>
  <div class="field"><label for="f-to">To</label>
    <input id="f-to" name="to" placeholder="HND" maxlength="3" required
           autocapitalize="characters" autocomplete="off"></div>
</div>
<div class="pair">
  <div class="field"><label for="f-out">Depart</label>
    <input id="f-out" name="out" type="date" required></div>
  <div class="field"><label for="f-back">Return <span class="why">optional</span></label>
    <input id="f-back" name="back" type="date"></div>
</div>
<div class="pair">
  <div class="field"><label for="f-cabin">Cabin</label>
    <select id="f-cabin" name="cabin">
      <option value="economy">Economy</option>
      <option value="premium-economy">Premium economy</option>
      <option value="business">Business</option>
      <option value="first">First</option>
    </select></div>
  <div class="field"><label for="f-adults">Adults</label>
    <input id="f-adults" name="adults" type="number" min="1" max="9" value="1"></div>
</div>
<div class="field"><label for="f-max">Always alert below <span class="why">optional</span></label>
  <input id="f-max" name="max" type="number" min="1" placeholder="900"></div>
<label class="check"><input type="checkbox" id="f-nonstop" name="nonstop">
  Nonstop only</label>
<p class="said" id="add-said"></p>
<div class="actions">
  <button type="button" data-close>Cancel</button>
  <button type="submit" class="go">Add flight</button>
</div>
</form></dialog>

<dialog id="drop-dialog"><form method="dialog" class="inner" id="drop-form">
<h3>Stop tracking this flight?</h3>
<p class="why" id="drop-what"></p>
<label class="check"><input type="checkbox" id="drop-purge">
  Also delete its recorded prices</label>
<p class="why">Leave that unticked and the history is kept, so adding the
flight back later picks up where it left off.</p>
<p class="said" id="drop-said"></p>
<div class="actions">
  <button type="button" data-close>Cancel</button>
  <button type="submit" class="go danger">Stop tracking</button>
</div>
</form></dialog>

<dialog id="key-dialog"><form method="dialog" class="inner" id="key-form">
<h3>One-time setup</h3>
<p class="why">Changing the watchlist means asking GitHub to run a workflow,
which needs a token. It is stored in this browser only — never in the
repository, and never sent anywhere except GitHub.</p>
<p class="why">Create a <b>fine-grained personal access token</b> scoped to
this one repository, with <code>Actions: Read and write</code>. Give it a
short expiry.</p>
<div class="field"><label for="f-key">Token</label>
  <input id="f-key" type="password" placeholder="github_pat_..." required
         autocomplete="off" spellcheck="false"></div>
<p class="said" id="key-said"></p>
<div class="actions">
  <button type="button" data-close>Cancel</button>
  <button type="submit" class="go">Save</button>
</div>
</form></dialog>
"""

MANAGE_SCRIPT = """
(function () {
  var config = window.__watchlist;
  if (!config) { return; }

  var STORE = 'flighttracker.token';
  var byId = function (id) { return document.getElementById(id); };

  // The token lives in this browser. Reading it can throw in private mode or
  // with site data blocked, so every access is guarded and the page still
  // works without it — it just asks again.
  function token(value) {
    try {
      if (value === undefined) { return window.localStorage.getItem(STORE); }
      if (value === null) { window.localStorage.removeItem(STORE); }
      else { window.localStorage.setItem(STORE, value); }
    } catch (e) { return null; }
    return value;
  }

  function say(node, text, bad) {
    node.textContent = text || '';
    node.className = bad ? 'said bad' : 'said';
  }

  function dispatch(inputs) {
    var key = token();
    if (!key) { return Promise.reject(new Error('no-token')); }
    var url = 'https://api.github.com/repos/' + config.repo +
              '/actions/workflows/' + config.workflow + '/dispatches';
    // Every input must be a string: the workflow_dispatch API rejects
    // anything else, including real booleans.
    var body = {};
    Object.keys(inputs).forEach(function (name) {
      body[name] = String(inputs[name]);
    });
    return fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + key,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
      },
      body: JSON.stringify({ ref: config.ref, inputs: body })
    }).then(function (response) {
      if (response.status === 204) { return; }
      if (response.status === 401 || response.status === 403) {
        token(null);
        throw new Error('The token was rejected. It may be expired, or ' +
                        'missing the Actions: Read and write permission.');
      }
      return response.json().catch(function () { return {}; })
        .then(function (data) {
          throw new Error(data.message || ('GitHub said ' + response.status));
        });
    });
  }

  var pending = null;

  function withToken(run) {
    if (token()) { run(); return; }
    pending = run;
    byId('key-dialog').showModal();
  }

  function submitter(form) {
    return form.querySelector('button[type="submit"]');
  }

  function run(form, said, inputs, done) {
    var button = submitter(form);
    button.disabled = true;
    say(said, 'Asking GitHub to run it...');
    dispatch(inputs).then(function () {
      say(said, 'Queued. It takes a minute or two, then reload this page.');
      button.disabled = false;
      if (done) { done(); }
    }).catch(function (error) {
      button.disabled = false;
      if (error.message === 'no-token') {
        say(said, '');
        withToken(function () { run(form, said, inputs, done); });
        return;
      }
      say(said, error.message, true);
    });
  }

  // --- the token prompt ----------------------------------------------------
  byId('key-form').addEventListener('submit', function (event) {
    event.preventDefault();
    var value = byId('f-key').value.trim();
    if (!value) { return; }
    token(value);
    byId('f-key').value = '';
    say(byId('key-said'), '');
    byId('key-dialog').close();
    var next = pending; pending = null;
    if (next) { next(); }
  });

  // --- adding --------------------------------------------------------------
  var addDialog = byId('add-dialog');
  Array.prototype.forEach.call(
    document.querySelectorAll('[data-add]'),
    function (button) {
      button.addEventListener('click', function () {
        say(byId('add-said'), '');
        addDialog.showModal();
      });
    }
  );

  byId('add-form').addEventListener('submit', function (event) {
    event.preventDefault();
    var form = event.target;
    var said = byId('add-said');
    var from = form.elements.from.value.trim().toUpperCase();
    var to = form.elements.to.value.trim().toUpperCase();
    var out = form.elements.out.value;
    var back = form.elements.back.value;

    if (!/^[A-Z]{3}$/.test(from) || !/^[A-Z]{3}$/.test(to)) {
      say(said, 'Airports are three-letter codes, like JFK.', true); return;
    }
    if (!out) { say(said, 'Pick a departure date.', true); return; }
    if (back && back < out) {
      say(said, 'The return date is before the departure date.', true); return;
    }

    run(form, said, {
      action: 'add',
      route: from + '-' + to + ':' + out,
      returning: back,
      watch_id: '',
      cabin: form.elements.cabin.value,
      adults: form.elements.adults.value || '1',
      nonstop: byId('f-nonstop').checked,
      max_price: form.elements.max.value,
      purge: false
    }, function () { form.reset(); });
  });

  // --- removing ------------------------------------------------------------
  var dropDialog = byId('drop-dialog');
  var dropping = '';

  Array.prototype.forEach.call(
    document.querySelectorAll('[data-drop]'),
    function (button) {
      button.addEventListener('click', function () {
        dropping = button.getAttribute('data-drop');
        byId('drop-what').textContent = button.getAttribute('data-label');
        byId('drop-purge').checked = false;
        say(byId('drop-said'), '');
        dropDialog.showModal();
      });
    }
  );

  byId('drop-form').addEventListener('submit', function (event) {
    event.preventDefault();
    if (!dropping) { return; }
    run(event.target, byId('drop-said'), {
      action: 'remove',
      watch_id: dropping,
      purge: byId('drop-purge').checked,
      route: '', returning: '', cabin: 'economy',
      adults: '1', nonstop: false, max_price: ''
    });
  });

  Array.prototype.forEach.call(
    document.querySelectorAll('dialog [data-close]'),
    function (button) {
      button.addEventListener('click', function () {
        button.closest('dialog').close();
      });
    }
  );
})();
"""


# The page is a static file on GitHub Pages, so it cannot add a flight itself.
# What it can do is link to the workflow that can — which is the difference
# between "there is no way to do this" and "the way is one tap away".
WORKFLOW = "watchlist.yml"
SLUG = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/*$")


def repo_slug() -> Optional[str]:
    """`owner/repo` for this checkout, or None if it cannot be worked out.

    Actions sets GITHUB_REPOSITORY, and that is where the published page is
    built, so it is both the usual case and the one with the owner's exact
    capitalisation. The git remote is the fallback for building the page by
    hand.
    """
    from_env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if from_env:
        return from_env

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    found = SLUG.search(result.stdout.strip())
    return found.group(1) if found else None


# GitHub allows letters, digits, dot, dash and underscore in both halves. The
# page keeps a token for this repo in the reader's browser, so anything that
# could break out of the script tag below would be worth real money to steal —
# the value is checked against this before it is written anywhere.
VALID_SLUG = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")


def _usable_slug(slug: Optional[str]) -> Optional[str]:
    return slug if slug and VALID_SLUG.match(slug) else None


def _script_json(value: object) -> str:
    """JSON safe to inline in a <script> block.

    `json.dumps` escapes quotes but leaves `<` alone, so a string containing
    `</script>` would close the tag early and anything after it would run as
    markup. Escaping the angle brackets and the two line separators JavaScript
    treats as newlines closes that off.
    """
    encoded = json.dumps(value)
    for raw, safe in (
        ("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
        ("\u2028", "\\u2028"), ("\u2029", "\\u2029"),
    ):
        encoded = encoded.replace(raw, safe)
    return encoded


def _manage_button(slug: Optional[str]) -> str:
    """The button that opens the add form. Absent when there is no repo."""
    if not _usable_slug(slug):
        return ""
    return '<button type="button" class="manage" data-add>+ Add a flight</button>'


def _manage_tail(slug: Optional[str], branch: str = "main") -> str:
    """The dialogs and the script behind them, for the end of the page."""
    slug = _usable_slug(slug)
    if not slug:
        return ""
    config = {"repo": slug, "workflow": WORKFLOW, "ref": branch}
    return (
        DIALOGS
        + f"<script>window.__watchlist = {_script_json(config)};</script>"
        + f"<script>{MANAGE_SCRIPT}</script>"
    )


def render_body(
    conn: Connection,
    config: Config,
    now: datetime,
    repo: Optional[str] = None,
) -> str:
    verdicts = list(evaluate_only(config, conn, now))
    samples = horizon_samples(conn)

    slug = repo if repo is not None else repo_slug()
    checked = escape(now.strftime("%d %b %Y, %H:%M UTC"))
    parts = [
        f"<style>{STYLE}</style>",
        '<div class="wrap">',
        '<div class="top"><div>',
        "<h1>Flight Price Watch</h1>",
        f'<p class="sub">Last checked {checked}</p>',
        "</div>",
        _manage_button(slug),
        "</div>",
    ]
    model = fit_model(samples)
    for verdict in verdicts:
        parts.append(
            _card(conn, config, verdict, model, now, bool(_usable_slug(slug)))
        )
    parts.append('</div><div id="tip"></div>')
    parts.append(f"<script>{SCRIPT}</script>")
    parts.append(_manage_tail(slug))
    return "\n".join(parts)


def render_document(
    conn: Connection,
    config: Config,
    now: datetime,
    repo: Optional[str] = None,
) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Flight Price Watch</title>\n</head>\n<body>\n"
        + render_body(conn, config, now, repo)
        + "\n</body>\n</html>\n"
    )


def _card(
    conn, config, verdict: Verdict, model, now: datetime, manageable: bool = False
) -> str:
    watch = verdict.watch
    currency = verdict.currency
    history = run_history(conn, watch.id)

    pill = '<span class="pill buy">Buy signal</span>' if verdict.flagged else ""
    tone = " flagged" if verdict.flagged else ""
    drop = (
        f'<button type="button" class="drop" data-drop="{escape(watch.id, quote=True)}"'
        f' data-label="{escape(watch.name, quote=True)} · '
        f'{escape(watch.route, quote=True)}"'
        f' title="Stop tracking" aria-label="Stop tracking {escape(watch.name, quote=True)}"'
        ">&times;</button>"
        if manageable
        else ""
    )

    parts = [
        f'<section class="card{tone}">',
        '<div class="head"><div>',
        f"<h2>{escape(watch.name)}</h2>",
        f'<p class="route">{escape(watch.route)} · {escape(watch.cabin)}</p>',
        f"</div><div>{pill}{drop}</div></div>",
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
        '<span><i class="key band"></i>Range</span></div>'
        if projection.usable
        else ""
    )
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
