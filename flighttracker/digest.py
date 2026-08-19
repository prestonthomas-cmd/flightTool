"""Building and sending the one-email-per-run digest."""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from html import escape
from typing import Optional, Sequence

from .errors import FlightTrackerError
from .fetch import Failure, search_url
from .health import Concern
from .signals import Verdict


class EmailNotConfigured(FlightTrackerError):
    """SMTP settings are missing or incomplete."""


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: Optional[str]
    password: Optional[str]
    sender: str
    recipients: tuple[str, ...]
    security: str = "starttls"
    timeout: float = 30.0

    @classmethod
    def from_env(cls, environ=None) -> "SmtpConfig":
        environ = os.environ if environ is None else environ
        missing = [
            name
            for name in ("SMTP_HOST", "EMAIL_FROM", "EMAIL_TO")
            if not (environ.get(name) or "").strip()
        ]
        if missing:
            raise EmailNotConfigured(
                "missing " + ", ".join(missing) + " — see .env.example"
            )

        security = (environ.get("SMTP_SECURITY") or "starttls").strip().lower()
        if security not in {"starttls", "ssl", "none"}:
            raise EmailNotConfigured(
                f"SMTP_SECURITY={security!r} — expected starttls, ssl or none"
            )

        raw_port = (environ.get("SMTP_PORT") or "").strip()
        if raw_port:
            try:
                port = int(raw_port)
            except ValueError as exc:
                raise EmailNotConfigured(f"SMTP_PORT={raw_port!r} is not a number") from exc
        else:
            port = 465 if security == "ssl" else 587

        recipients = tuple(
            address.strip()
            for address in (environ.get("EMAIL_TO") or "").replace(";", ",").split(",")
            if address.strip()
        )
        if not recipients:
            raise EmailNotConfigured("EMAIL_TO has no addresses")

        return cls(
            host=environ["SMTP_HOST"].strip(),
            port=port,
            username=(environ.get("SMTP_USERNAME") or "").strip() or None,
            password=environ.get("SMTP_PASSWORD") or None,
            sender=environ["EMAIL_FROM"].strip(),
            recipients=recipients,
            security=security,
        )


def subject_for(
    flagged: Sequence[Verdict],
    failures: Sequence[Failure],
    concerns: Sequence[Concern] = (),
) -> str:
    # A watch that has stopped being tracked outranks any price news: the
    # prices you are not seeing are the ones that can cost you.
    blocking = [c for c in concerns if c.blocking]
    if blocking and not flagged:
        return f"Flight tracker: {len(blocking)} watch(es) NOT being tracked"
    if blocking and flagged:
        return (
            f"{len(flagged)} buy signal(s) — and {len(blocking)} watch(es) not "
            "being tracked"
        )
    if flagged:
        cheapest = min(flagged, key=lambda v: v.price if v.price is not None else 0)
        if len(flagged) == 1:
            return (
                f"Buy signal: {cheapest.watch.name} at "
                f"{cheapest.currency} {cheapest.price:,.0f}"
            )
        return (
            f"{len(flagged)} buy signals — from {cheapest.currency} "
            f"{cheapest.price:,.0f} ({cheapest.watch.name})"
        )
    if failures:
        return f"Flight tracker: no signals, {len(failures)} lookup failure(s)"
    return "Flight tracker: no buy signals"


def render_text(
    when: datetime,
    verdicts: Sequence[Verdict],
    failures: Sequence[Failure],
    concerns: Sequence[Concern] = (),
) -> str:
    flagged = [v for v in verdicts if v.flagged]
    others = [v for v in verdicts if not v.flagged]

    lines: list[str] = [
        f"Flight price check — {when:%Y-%m-%d %H:%M UTC}",
        "",
    ]

    if concerns:
        lines.append("!! NEEDS ATTENTION")
        lines.append("=" * 40)
        for concern in concerns:
            mark = "!!" if concern.blocking else " -"
            lines.append(f"{mark} {concern.message}")
        lines.append("")

    if flagged:
        lines.append(f"BUY SIGNALS ({len(flagged)})")
        lines.append("=" * 40)
        for verdict in flagged:
            lines.extend(_text_block(verdict))
            lines.append("")
    else:
        lines.append("No buy signals this run.")
        lines.append("")

    if others:
        lines.append("ALSO TRACKED")
        lines.append("-" * 40)
        for verdict in others:
            lines.append(_text_summary(verdict))
        lines.append("")

    if failures:
        lines.append(f"LOOKUPS THAT FAILED ({len(failures)})")
        lines.append("-" * 40)
        for failure in failures:
            dates = failure.depart_date.isoformat() if failure.depart_date else "?"
            if failure.return_date:
                dates += f" / {failure.return_date.isoformat()}"
            lines.append(f"{failure.watch_id} [{dates}]: {failure.message}")
        lines.append("")

    lines.append(
        "Prices are scraped from Google Flights and are a snapshot, not a quote."
    )
    return "\n".join(lines)


def _text_block(verdict: Verdict) -> list[str]:
    watch = verdict.watch
    dates = verdict.best_depart or "?"
    if verdict.best_return:
        dates += f" -> {verdict.best_return}"
    block = [
        f"{watch.name}  ({watch.route}, {watch.cabin})",
        f"  {verdict.currency} {verdict.price:,.0f}   {dates}",
    ]
    for reason in verdict.reasons:
        block.append(f"  * {reason.detail}")
    stats = verdict.stats
    if stats.has_history:
        block.append(
            f"  history: {stats.count} runs, low {verdict.currency} "
            f"{stats.minimum:,.0f}, median {verdict.currency} {stats.median:,.0f}, "
            f"high {verdict.currency} {stats.maximum:,.0f}"
        )
    block.extend(_forecast_lines(verdict))
    link = _link(verdict)
    if link:
        block.append(f"  {link}")
    return block


def _forecast_lines(verdict: Verdict, indent: str = "  ") -> list[str]:
    """The outlook block: a headline, then the evidence behind it."""
    forecast = verdict.forecast
    if forecast is None or not getattr(forecast, "known", False):
        return []

    lines = [f"{indent}outlook ({forecast.confidence} confidence): {forecast.headline}"]
    lines.extend(f"{indent}  - {note}" for note in forecast.notes)
    return lines


def _forecast_hint(verdict: Verdict) -> str:
    """A few words for the compact list, or nothing."""
    forecast = verdict.forecast
    if forecast is None or not getattr(forecast, "known", False):
        return ""
    return {
        "falling": " — usually still falling",
        "rising": " — usually rises from here",
        "flat": " — usually flat from here",
    }.get(forecast.direction, "")


def _text_summary(verdict: Verdict) -> str:
    watch = verdict.watch
    if verdict.price is None:
        return f"{watch.name} ({watch.route}): no price — {verdict.suppressed}"
    line = f"{watch.name} ({watch.route}): {verdict.currency} {verdict.price:,.0f}"
    stats = verdict.stats
    if stats.has_history and stats.median:
        delta = verdict.price - stats.median
        direction = "above" if delta > 0 else "below"
        line += (
            f" — {verdict.currency} {abs(delta):,.0f} {direction} its median "
            f"of {stats.count} runs"
        )
    line += _forecast_hint(verdict)
    if verdict.suppressed:
        line += f" [{verdict.suppressed}]"
    return line


def _link(verdict: Verdict) -> Optional[str]:
    from datetime import date as date_type

    if not verdict.best_depart:
        return None
    try:
        depart = date_type.fromisoformat(verdict.best_depart)
        back = (
            date_type.fromisoformat(verdict.best_return)
            if verdict.best_return
            else None
        )
    except ValueError:
        return None
    return search_url(verdict.watch, depart, back)


def render_html(
    when: datetime,
    verdicts: Sequence[Verdict],
    failures: Sequence[Failure],
    concerns: Sequence[Concern] = (),
) -> str:
    flagged = [v for v in verdicts if v.flagged]
    others = [v for v in verdicts if not v.flagged]

    parts = [
        "<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        "font-size:15px;line-height:1.5;color:#111;max-width:640px\">",
        f"<p style=\"color:#666;margin:0 0 18px\">Flight price check — "
        f"{escape(when.strftime('%Y-%m-%d %H:%M UTC'))}</p>",
    ]

    if concerns:
        items = "".join(
            f"<li><strong>{escape(c.message)}</strong></li>" if c.blocking
            else f"<li>{escape(c.message)}</li>"
            for c in concerns
        )
        parts.append(
            "<div style=\"border:1px solid #f0c0c0;border-left:4px solid #c0392b;"
            "border-radius:6px;padding:12px 16px;margin:0 0 18px;background:#fdf5f5\">"
            "<div style=\"font-weight:700;color:#a12b2b\">Needs attention</div>"
            f"<ul style=\"margin:8px 0 0;padding-left:20px\">{items}</ul></div>"
        )

    if flagged:
        parts.append(
            f"<h2 style=\"margin:0 0 12px;font-size:18px\">Buy signals "
            f"({len(flagged)})</h2>"
        )
        for verdict in flagged:
            parts.append(_html_card(verdict))
    else:
        parts.append(
            "<p style=\"margin:0 0 18px\"><strong>No buy signals this run.</strong></p>"
        )

    if others:
        parts.append(
            "<h3 style=\"margin:24px 0 8px;font-size:15px;color:#444\">Also tracked</h3>"
            "<ul style=\"margin:0;padding-left:20px;color:#444\">"
        )
        for verdict in others:
            parts.append(f"<li>{escape(_text_summary(verdict))}</li>")
        parts.append("</ul>")

    if failures:
        parts.append(
            "<h3 style=\"margin:24px 0 8px;font-size:15px;color:#a00\">Lookups that "
            f"failed ({len(failures)})</h3><ul style=\"margin:0;padding-left:20px;"
            "color:#a00\">"
        )
        for failure in failures:
            dates = failure.depart_date.isoformat() if failure.depart_date else "?"
            if failure.return_date:
                dates += f" / {failure.return_date.isoformat()}"
            parts.append(
                f"<li>{escape(failure.watch_id)} [{escape(dates)}]: "
                f"{escape(failure.message)}</li>"
            )
        parts.append("</ul>")

    parts.append(
        "<p style=\"margin:24px 0 0;color:#888;font-size:13px\">Prices are scraped "
        "from Google Flights and are a snapshot, not a quote.</p></div>"
    )
    return "".join(parts)


def _html_card(verdict: Verdict) -> str:
    watch = verdict.watch
    dates = escape(verdict.best_depart or "?")
    if verdict.best_return:
        dates += " &rarr; " + escape(verdict.best_return)

    reasons = "".join(
        f"<li>{escape(reason.detail)}</li>" for reason in verdict.reasons
    )
    stats = verdict.stats
    forecast = _html_forecast(verdict)
    history = ""
    if stats.has_history:
        history = (
            f"<p style=\"margin:8px 0 0;color:#666;font-size:13px\">"
            f"{stats.count} runs — low {escape(verdict.currency)} "
            f"{stats.minimum:,.0f}, median {escape(verdict.currency)} "
            f"{stats.median:,.0f}, high {escape(verdict.currency)} "
            f"{stats.maximum:,.0f}</p>"
        )

    link = _link(verdict)
    link_html = (
        f"<p style=\"margin:10px 0 0\"><a href=\"{escape(link)}\" "
        "style=\"color:#0b5fff\">Open in Google Flights</a></p>"
        if link
        else ""
    )

    return (
        "<div style=\"border:1px solid #e3e3e3;border-left:4px solid #0b8a3e;"
        "border-radius:6px;padding:14px 16px;margin:0 0 14px\">"
        f"<div style=\"font-weight:600\">{escape(watch.name)}</div>"
        f"<div style=\"color:#666;font-size:13px\">{escape(watch.route)} &middot; "
        f"{escape(watch.cabin)}</div>"
        f"<div style=\"font-size:22px;font-weight:700;margin:8px 0 2px\">"
        f"{escape(verdict.currency)} {verdict.price:,.0f}</div>"
        f"<div style=\"color:#444\">{dates}</div>"
        f"<ul style=\"margin:10px 0 0;padding-left:20px\">{reasons}</ul>"
        f"{history}{forecast}{link_html}</div>"
    )


def _html_forecast(verdict: Verdict) -> str:
    forecast = verdict.forecast
    if forecast is None or not getattr(forecast, "known", False):
        return ""

    tint = {"falling": "#0b6fbf", "rising": "#b34700", "flat": "#555"}.get(
        forecast.direction, "#555"
    )
    notes = "".join(
        f"<li>{escape(note)}</li>" for note in forecast.notes
    )
    return (
        f"<div style=\"margin:12px 0 0;padding:10px 12px;background:#f6f7f9;"
        f"border-radius:4px\">"
        f"<div style=\"font-weight:600;color:{tint};font-size:13px\">"
        f"Outlook &middot; {escape(forecast.confidence)} confidence</div>"
        f"<div style=\"margin:2px 0 0;font-size:13px\">"
        f"{escape(forecast.headline)}</div>"
        f"<ul style=\"margin:6px 0 0;padding-left:18px;color:#555;font-size:12px\">"
        f"{notes}</ul></div>"
    )


def build_message(
    config: SmtpConfig,
    when: datetime,
    verdicts: Sequence[Verdict],
    failures: Sequence[Failure],
    concerns: Sequence[Concern] = (),
) -> EmailMessage:
    flagged = [v for v in verdicts if v.flagged]
    message = EmailMessage()
    message["Subject"] = subject_for(flagged, failures, concerns)
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message["Date"] = formatdate(localtime=True)
    message.set_content(render_text(when, verdicts, failures, concerns))
    message.add_alternative(
        render_html(when, verdicts, failures, concerns), subtype="html"
    )
    return message


def send(config: SmtpConfig, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    if config.security == "ssl":
        server = smtplib.SMTP_SSL(
            config.host, config.port, timeout=config.timeout, context=context
        )
    else:
        server = smtplib.SMTP(config.host, config.port, timeout=config.timeout)

    with server:
        server.ehlo()
        if config.security == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if config.username and config.password:
            server.login(config.username, config.password)
        server.send_message(message)
