"""Read-only merchant lookup table for the Wordline tenant API.

Loads ``data/merchants.json`` (produced by ``scripts/convert_csv.py``) and
indexes it by the keys a voice caller can actually give:

* the exact terminal id (``1231410A``),
* its *digit key* — the digits only (``1231410``).  The workflow's Terminal
  ID question captures digits (the STT rarely hears the trailing letter) and
  may keep the spaces or dashes a caller dictates with (``1 2 3 1 4 1 0``);
* the registered contact number, matched on its last ten digits.

The file is re-read when its modification time changes, so a regenerated
JSON is picked up without a restart.  Nothing here writes.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "merchants.json"

_PLACEHOLDER = re.compile(r"^\s*(\{\{.*\}\}|\{.*\}|\[.*\])\s*$")
_ALNUM = re.compile(r"[^A-Z0-9]")
_DIGITS = re.compile(r"\D")

MIN_TERMINAL_DIGITS = 4
MAX_TERMINAL_CHARS = 12
CONTACT_DIGITS = 10


def is_placeholder(raw: object) -> bool:
    """An unresolved workflow template (``{{spoken_terminal_id_retry}}``) is
    not a value: the executor sends unknown placeholders literally."""
    return bool(_PLACEHOLDER.match(str(raw)))


def clean_value(raw: object) -> str:
    if raw is None or isinstance(raw, (dict, list)) or is_placeholder(raw):
        return ""
    return str(raw).strip()


def terminal_keys(raw: object) -> tuple[str, str]:
    """(exact alphanumeric key, digit key) for a dictated terminal id."""
    value = clean_value(raw).upper()
    exact = _ALNUM.sub("", value)
    digits = _DIGITS.sub("", value)
    if len(exact) > MAX_TERMINAL_CHARS:
        return "", ""
    return exact, digits


def contact_key(raw: object) -> str:
    """Last ten digits of a phone number, or "" when it is too short."""
    digits = _DIGITS.sub("", clean_value(raw))
    return digits[-CONTACT_DIGITS:] if len(digits) >= CONTACT_DIGITS else ""


def mask_contact(number: str, keep: int = 4) -> str:
    digits = _DIGITS.sub("", number or "")
    if not digits:
        return ""
    return "X" * max(0, len(digits) - keep) + digits[-keep:]


@dataclass(frozen=True)
class Match:
    record: dict
    matched_by: str  # terminal_id | terminal_digits | contact_number


class MerchantStore:
    """Thread-safe, mtime-refreshing view over the merchants JSON file."""

    def __init__(self, path: Path = DATA_FILE):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._records: list[dict] = []
        self._by_terminal: dict[str, dict] = {}
        self._by_digits: dict[str, dict] = {}
        self._by_contact: dict[str, dict] = {}
        self.loaded_at: str | None = None
        self.meta: dict = {}

    # ── loading ───────────────────────────────────────────────────────────

    def _load_locked(self) -> None:
        document = json.loads(self.path.read_text(encoding="utf-8"))
        records = [r for r in document.get("records", []) if isinstance(r, dict)]
        by_terminal: dict[str, dict] = {}
        by_digits: dict[str, dict] = {}
        by_contact: dict[str, dict] = {}
        for record in records:
            exact, digits = terminal_keys(record.get("terminal_id"))
            if exact:
                by_terminal.setdefault(exact, record)
            if len(digits) >= MIN_TERMINAL_DIGITS:
                by_digits.setdefault(digits, record)
            contact = contact_key(record.get("contact_number"))
            if contact:
                by_contact.setdefault(contact, record)
        self._records = records
        self._by_terminal, self._by_digits, self._by_contact = by_terminal, by_digits, by_contact
        self.meta = document.get("_meta") or {}
        self.loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def refresh(self, force: bool = False) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = None
        with self._lock:
            if force or mtime != self._mtime:
                self._load_locked()
                self._mtime = mtime

    # ── queries ───────────────────────────────────────────────────────────

    def records(self) -> list[dict]:
        self.refresh()
        return list(self._records)

    def __len__(self) -> int:
        self.refresh()
        return len(self._records)

    def find_terminal(self, raw: object) -> Match | None:
        self.refresh()
        exact, digits = terminal_keys(raw)
        if exact and exact in self._by_terminal:
            return Match(self._by_terminal[exact], "terminal_id")
        if len(digits) >= MIN_TERMINAL_DIGITS and digits in self._by_digits:
            return Match(self._by_digits[digits], "terminal_digits")
        return None

    def find_contact(self, raw: object) -> Match | None:
        self.refresh()
        key = contact_key(raw)
        record = self._by_contact.get(key) if key else None
        return Match(record, "contact_number") if record else None

    def search(self, *, issue_code: str | None = None, bank: str | None = None) -> list[dict]:
        self.refresh()
        wanted_bank = (bank or "").strip().lower()
        return [
            r for r in self._records
            if (not issue_code or r.get("issue_code") == issue_code)
            and (not wanted_bank or wanted_bank in str(r.get("bank_name", "")).lower())
        ]
