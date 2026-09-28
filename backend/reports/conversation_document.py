"""Shareable conversation document: call details, AI summary and the full
transcript as a PDF or plain-text file for someone outside EchoSphere.

The CSV/XLSX transcript export is an analytics table (one row per turn with
intents, retrieval, latency and, for costs.view, cost). This document is meant
to be forwarded, so it carries only what a reader needs to follow the call —
never costs, traces or internal routing.

The PDF is laid out with PyMuPDF's Story (HTML + CSS). MuPDF shapes complex
scripts with HarfBuzz and falls back to its built-in Noto fonts, so Hindi,
Malayalam, Tamil and the other Indic scripts keep their conjuncts without the
product shipping any font of its own.
"""

from __future__ import annotations

import html
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import fitz

from shared.models import ConversationSession

PDF_CONTENT_TYPE = "application/pdf"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"

_CHANNEL_LABELS = {"voice": "Voice", "whatsapp": "WhatsApp", "web": "Web", "mobile": "Mobile"}
# A layout that never converges must not spin forever; no real call is close.
_MAX_PDF_PAGES = 1000
# Table labels up to this length are kept on one line (see _label).
_NOWRAP_LABEL_CHARS = 28
# The Transcript heading moves to a new page when less than this is left
# under it, instead of sitting alone at the bottom of the page.
_HEADING_KEEP_PT = 72
# Chrome's Intl API reports CLDR ids, which keep old IANA names ("Asia/Calcutta"
# for every viewer in India). Newer system tzdata ships those only in the
# optional tzdata-legacy package, so without this map India fell back to UTC.
_LEGACY_ZONES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Rangoon": "Asia/Yangon",
    "Europe/Kiev": "Europe/Kyiv",
    "America/Godthab": "America/Nuuk",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Indianapolis": "America/Indiana/Indianapolis",
    "Atlantic/Faeroe": "Atlantic/Faroe",
    "Pacific/Enderbury": "Pacific/Kanton",
}


@dataclass(frozen=True)
class DocumentTurn:
    role: str  # "bot" | "user"
    speaker: str
    text: str
    at: str | None = None


@dataclass(frozen=True)
class ConversationDocument:
    conversation_id: str
    bot_name: str
    started: str
    details: list[tuple[str, str]]
    time_zone: str
    generated_at: str
    turns: list[DocumentTurn]
    summary_text: str | None = None
    summary_facts: list[tuple[str, str]] = field(default_factory=list)
    summary_lists: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def has_summary(self) -> bool:
        return bool(self.summary_text or self.summary_facts or self.summary_lists)


# ── building ─────────────────────────────────────────────────────────────────

def resolve_viewer_zone(name: str | None) -> ZoneInfo:
    """The downloading viewer's IANA zone; UTC when absent or unknown (the
    document then says so rather than guessing)."""
    for key in (name, _LEGACY_ZONES.get(name or "")):
        if not key:
            continue
        try:
            return ZoneInfo(key)
        except Exception:  # noqa: BLE001 — client input; malformed keys raise ValueError/OSError too
            continue
    return ZoneInfo("UTC")


def _utc(value: datetime) -> datetime:
    # MySQL rows are naive UTC; runtime turn instants arrive as ISO with Z.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str) and value:
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _date_time(value: datetime) -> str:
    return f"{value.day} {value:%b %Y}, {value:%I:%M:%S %p}"


def _duration(seconds: int | float | None) -> str:
    """Same wording as the Conversation Review list."""
    total = max(0, int(round(seconds or 0)))
    if total < 60:
        return f"{total} sec"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m {total % 60:02d}s"


def _zone_label(zone: ZoneInfo, at: datetime) -> str:
    """A label like 'IST (UTC+05:30)' rather than the IANA key (Chrome reports
    India as the legacy 'Asia/Calcutta'); zones whose abbreviation is only a
    number ('+04') get the bare offset."""
    local = at.astimezone(zone)
    raw = local.strftime("%z")
    if raw == "+0000" and zone.key in {"UTC", "Etc/UTC"}:
        return "UTC"
    offset = f"UTC{raw[:3]}:{raw[3:]}"
    name = local.tzname() or ""
    return f"{name} ({offset})" if name.isalpha() else offset


def _words(value: object) -> str:
    """A stored code such as ``promise_to_pay`` as readable text."""
    text = " ".join(str(value).replace("_", " ").split())
    return text[:1].upper() + text[1:]


def _amount(value: object) -> str:
    """Grouped the way the review drawer shows it (en-IN: 1,00,000)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    whole, _, fraction = f"{abs(number):.2f}".partition(".")
    head, grouped = whole[:-3], whole[-3:]
    while head:
        grouped = f"{head[-2:]},{grouped}"
        head = head[:-2]
    fraction = fraction.rstrip("0")
    return ("-" if number < 0 else "") + grouped + (f".{fraction}" if fraction else "")


def _summary_parts(
    summary: dict | None,
) -> tuple[str | None, list[tuple[str, str]], list[tuple[str, list[str]]]]:
    """The drawer's AI summary as document sections (``summary`` is the
    serialize_conversation_memory payload, which is already PII-free)."""
    if not summary:
        return None, [], []
    facts: list[tuple[str, str]] = []
    if summary.get("callOutcome"):
        facts.append(("Call outcome", _words(summary["callOutcome"])))
    labels = summary.get("structuredFieldLabels") or {}
    for key, value in (summary.get("structuredFields") or {}).items():
        facts.append((
            labels.get(key) or _words(key),
            "Not determined" if value is None else _words(value),
        ))
    nba = summary.get("nextBestAction") or {}
    if nba.get("action"):
        action = _words(nba["action"])
        if nba.get("priority"):
            action += f" ({nba['priority']} priority)"
        if nba.get("reason"):
            action += f" — {nba['reason']}"
        facts.append(("Next best action", action))

    lists: list[tuple[str, list[str]]] = []
    commitments = []
    for c in summary.get("customerCommitments") or []:
        line = str(c.get("description") or c.get("type") or "Commitment")
        if c.get("amount"):
            line += " — " + " ".join(p for p in (c.get("currency"), _amount(c["amount"])) if p)
        if c.get("dueDate"):
            line += f" (due {c['dueDate']})"
        if c.get("status"):
            line += f" · {c['status']}"
        commitments.append(line)
    if commitments:
        lists.append(("Customer commitments", commitments))
    pending = [
        _words(item)
        for item in [*(summary.get("unresolvedItems") or []), *(summary.get("missingSlots") or [])]
        if item
    ]
    if pending:
        lists.append(("Pending", pending))
    important = [str(f) for f in summary.get("importantFacts") or [] if f]
    if important:
        lists.append(("Important facts", important))
    text = (summary.get("summary") or "").strip() or None
    return text, facts, lists


def build_conversation_document(
    conversation: ConversationSession,
    *,
    bot_name: str,
    turns: list[dict],
    summary: dict | None,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> ConversationDocument:
    """``turns`` are ui_turns() output; times render in ``zone`` — the viewer's
    own timezone, so the file matches the page it was downloaded from."""
    started_at = _instant(conversation.started_at)
    generated = _utc(now or datetime.now(timezone.utc))
    started = _date_time(started_at.astimezone(zone)) if started_at else "—"
    other_party = "Caller" if conversation.channel == "voice" else "Customer"

    details = [
        ("Conversation ID", conversation.id),
        ("Bot", bot_name),
        ("Channel", _CHANNEL_LABELS.get(conversation.channel, _words(conversation.channel or "—"))),
        (other_party, conversation.caller_masked or "•••"),
        ("Language", conversation.language or "—"),
        ("Started", started),
        ("Duration", _duration(conversation.duration_sec)),
        ("Outcome", "Contained" if conversation.contained else "Escalated"),
    ]
    if not conversation.contained and conversation.escalation_reason:
        details.append(("Escalation reason", conversation.escalation_reason))
    if conversation.disposition:
        details.append(("Disposition", _words(conversation.disposition)))
    if conversation.sentiment:
        details.append(("Sentiment", _words(conversation.sentiment)))

    document_turns = []
    for turn in turns:
        text = str(turn.get("text") or "").replace("\r\n", "\n").strip()
        if not text:
            continue
        is_bot = turn.get("speaker") == "bot"
        at = _instant(turn.get("at"))
        document_turns.append(DocumentTurn(
            role="bot" if is_bot else "user",
            speaker="Bot" if is_bot else other_party,
            text=text,
            at=f"{at.astimezone(zone):%I:%M:%S %p}" if at else None,
        ))

    summary_text, summary_facts, summary_lists = _summary_parts(summary)
    return ConversationDocument(
        conversation_id=conversation.id,
        bot_name=bot_name,
        started=started,
        details=details,
        time_zone=_zone_label(zone, started_at or generated),
        generated_at=_date_time(generated.astimezone(zone)),
        turns=document_turns,
        summary_text=summary_text,
        summary_facts=summary_facts,
        summary_lists=summary_lists,
    )


# ── plain text ───────────────────────────────────────────────────────────────

def render_conversation_text(document: ConversationDocument) -> bytes:
    """UTF-8 with a BOM, so Windows editors and browsers opening the file
    locally do not guess a legacy encoding and mangle Indic text."""
    lines = ["Conversation transcript", f"{document.bot_name} · {document.conversation_id}", ""]
    width = max(len(label) for label, _ in document.details)
    lines += [f"{label.ljust(width)} : {value}" for label, value in document.details]
    lines += ["", f"Times are shown in {document.time_zone}."]

    if document.has_summary:
        lines += ["", "AI SUMMARY", "----------"]
        if document.summary_text:
            lines.append(document.summary_text)
        lines += [f"{label}: {value}" for label, value in document.summary_facts]
        for label, items in document.summary_lists:
            lines.append(f"{label}:")
            lines += [f"  - {item}" for item in items]

    lines += ["", "TRANSCRIPT", "----------"]
    if not document.turns:
        lines.append("No turns were captured for this call.")
    for turn in document.turns:
        prefix = f"[{turn.at}] " if turn.at else ""
        body = turn.text.replace("\n", "\n    ")
        lines += [f"{prefix}{turn.speaker}: {body}", ""]

    lines += ["", f"Generated by EchoSphere on {document.generated_at}."]
    return ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")


# ── PDF ──────────────────────────────────────────────────────────────────────

_PDF_CSS = """
* { font-family: sans-serif; }
body { font-size: 10pt; line-height: 1.4; color: #1f2430; }
h1 { font-size: 17pt; margin: 0 0 2pt 0; }
p.sub { font-size: 9.5pt; color: #5b6272; margin: 0 0 12pt 0; }
h2 { font-size: 11.5pt; margin: 14pt 0 6pt 0; padding-bottom: 3pt; border-bottom: 0.75pt solid #c9ced8; }
h2.new-page { page-break-before: always; }
table { border-collapse: collapse; width: 100%; }
td { padding: 3pt 10pt 3pt 0; vertical-align: top; border-bottom: 0.5pt solid #e3e6ec; }
td.k { color: #5b6272; }
p { margin: 0 0 5pt 0; }
p.note { font-size: 8.5pt; color: #6b7280; margin: 5pt 0 0 0; }
p.label { font-weight: bold; margin: 8pt 0 2pt 0; }
ul { margin: 0 0 4pt 14pt; padding: 0; }
.turn { margin: 0 0 6pt 0; padding: 5pt 8pt; }
.turn.bot { background-color: #f1f3f7; }
.turn.user { background-color: #edf0fd; }
.who { font-weight: bold; font-size: 9pt; }
.at { color: #6b7280; font-size: 8.5pt; }
.text { margin-top: 1pt; }
"""


def _esc(value: str) -> str:
    return html.escape(value, quote=True).replace("\n", "<br>")


def _label(value: str) -> str:
    # MuPDF sizes table columns by their longest word and ignores
    # white-space: nowrap (the label then overlaps its value), so "Conversation
    # ID" would break after one word; non-breaking spaces are honoured.
    escaped = _esc(value)
    return escaped.replace(" ", "&nbsp;") if len(value) <= _NOWRAP_LABEL_CHARS else escaped


def _rows(pairs: list[tuple[str, str]]) -> str:
    return "<table>" + "".join(
        f'<tr><td class="k">{_label(label)}</td><td>{_esc(value)}</td></tr>'
        for label, value in pairs
    ) + "</table>"


def _pdf_html(document: ConversationDocument, *, transcript_on_new_page: bool = False) -> str:
    parts = [
        "<h1>Conversation transcript</h1>",
        f'<p class="sub">{_esc(document.bot_name)} · {_esc(document.started)}</p>',
        _rows(document.details),
        f'<p class="note">Times are shown in {_esc(document.time_zone)}.</p>',
    ]
    if document.has_summary:
        parts.append("<h2>AI summary</h2>")
        if document.summary_text:
            parts.append(f"<p>{_esc(document.summary_text)}</p>")
        if document.summary_facts:
            parts.append(_rows(document.summary_facts))
        for label, items in document.summary_lists:
            parts.append(f'<p class="label">{_esc(label)}</p><ul>')
            parts += [f"<li>{_esc(item)}</li>" for item in items]
            parts.append("</ul>")
    new_page = ' class="new-page"' if transcript_on_new_page else ""
    parts.append(f'<h2 id="transcript"{new_page}>Transcript</h2>')
    if not document.turns:
        parts.append('<p class="note">No turns were captured for this call.</p>')
    for turn in document.turns:
        at = f' <span class="at">{_esc(turn.at)}</span>' if turn.at else ""
        parts.append(
            f'<div class="turn {turn.role}"><span class="who">{_esc(turn.speaker)}</span>{at}'
            f'<div class="text">{_esc(turn.text)}</div></div>'
        )
    return "".join(parts)


def _stamp_footers(pdf: fitz.Document, document: ConversationDocument) -> None:
    # Footer strings are ASCII by construction (ids, dates), so the built-in
    # Helvetica is enough here; the bot name stays in the Story-laid body.
    left = f"EchoSphere · Conversation {document.conversation_id}"
    left = left.encode("latin-1", "replace").decode("latin-1")
    for index, page in enumerate(pdf):
        right = f"Page {index + 1} of {pdf.page_count}"
        y = page.rect.height - 26
        page.insert_text((42, y), left, fontsize=7.5, color=(0.42, 0.45, 0.5))
        x = page.rect.width - 42 - fitz.get_text_length(right, fontname="helv", fontsize=7.5)
        page.insert_text((x, y), right, fontsize=7.5, color=(0.42, 0.45, 0.5))


def _lay_out(markup: str) -> tuple[bytes, bool]:
    """Paginate the Story onto A4; also report whether the Transcript heading
    landed too close to a page bottom (MuPDF ignores page-break-after)."""
    story = fitz.Story(html=markup, user_css=_PDF_CSS)
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (42, 42, -42, -54)
    stranded = False

    def track(position) -> None:
        nonlocal stranded
        if position.id == "transcript" and position.rect[3] > where.y1 - _HEADING_KEEP_PT:
            stranded = True

    for _page in range(_MAX_PDF_PAGES):
        device = writer.begin_page(mediabox)
        more, _filled = story.place(where)
        story.element_positions(track)
        story.draw(device)
        writer.end_page()
        if not more:
            break
    writer.close()
    return buffer.getvalue(), stranded


def render_conversation_pdf(document: ConversationDocument) -> bytes:
    laid_out, stranded = _lay_out(_pdf_html(document))
    if stranded:
        laid_out, _ = _lay_out(_pdf_html(document, transcript_on_new_page=True))

    pdf = fitz.open("pdf", laid_out)
    try:
        _stamp_footers(pdf, document)
        pdf.set_metadata({
            "title": f"Conversation {document.conversation_id}",
            "subject": f"{document.bot_name} · {document.started}",
            "author": "EchoSphere",
            "creator": "EchoSphere",
        })
        try:
            # Keeps only the glyphs used: a Hindi call is ~60 KB, not ~1 MB of
            # embedded Noto fonts.
            pdf.subset_fonts()
        except Exception:  # noqa: BLE001 — a larger file is still a valid file
            pass
        return pdf.tobytes(garbage=4, deflate=True)
    finally:
        pdf.close()
