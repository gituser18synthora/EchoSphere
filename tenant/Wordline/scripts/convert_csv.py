"""Wordline merchant CSV → ``data/merchants.json``.

The source file (``Wordline.csv``, tab-separated despite the name) is the
field-team export of merchant terminals with an open paper-roll or battery
ticket.  Columns map one-to-one onto the JSON records the tenant API serves:

    ANCHORAPPTID   → terminal_id      (alphanumeric, e.g. ``1231410A``)
    MERCHANT       → outlet_name
    Bank Name      → bank_name
    ASSETMODEL     → asset_type       (terminal model)
    Contact No     → contact_number   (10-digit Indian mobile, digits only)
    Address        → address          (optional; multi-line, kept line by line)
    Contact Person → contact_person   (optional)
    Type           → issue_type       (+ ``issue_code``, a slug of it)

No value is reshaped beyond whitespace trimming and the contact-number
digit normalisation, so every record can be traced back to its CSV row.
The TTS-friendly casing of names, models and addresses is applied by the
API when it answers (``api/speech.py``), not here.  Header names are
matched after trimming (the 2026-11-10 export has ``"Contact Person "``).
``Address`` and ``Contact Person`` are optional: an older export without
them, or an empty cell, converts with an empty value.
The converter refuses files whose terminal ids, digit keys (what a caller
dictates — see the API) or contact numbers collide, because the voice
workflow resolves callers by exactly those keys.

Usage (from the repo root)::

    env/bin/python tenant/Wordline/scripts/convert_csv.py            # rewrite JSON
    env/bin/python tenant/Wordline/scripts/convert_csv.py --check    # verify only

``--check`` exits 1 when the JSON on disk no longer matches the CSV.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TENANT_DIR = Path(__file__).resolve().parent.parent
CSV_PATH = TENANT_DIR / "Wordline.csv"
JSON_PATH = TENANT_DIR / "data" / "merchants.json"

SCHEMA_VERSION = 2
COLUMNS = {
    "ANCHORAPPTID": "terminal_id",
    "MERCHANT": "outlet_name",
    "Bank Name": "bank_name",
    "ASSETMODEL": "asset_type",
    "Contact No": "contact_number",
    "Type": "issue_type",
}
# Added by the 2026-11-10 export; optional so older exports still convert.
OPTIONAL_COLUMNS = {
    "Address": "address",
    "Contact Person": "contact_person",
}
FIELDS = [*COLUMNS.values(), "issue_code", *OPTIONAL_COLUMNS.values()]
LOOKUP_KEYS = ["terminal_id (digit key)", "contact_number (last 10 digits)"]

_TERMINAL_ID = re.compile(r"^[A-Z0-9]{4,12}$")


class ConversionError(ValueError):
    """The CSV cannot be turned into a trustworthy lookup table."""


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def normalise_contact(raw: str) -> str:
    """Digits only; a ``91``-prefixed 12-digit number is reduced to 10."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits


def _sniff_dialect(sample: str) -> str:
    return "\t" if "\t" in sample.split("\n", 1)[0] else ","


def read_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    delimiter = _sniff_dialect(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    # "Contact Person " (trailing space in the export) is "Contact Person".
    reader.fieldnames = [" ".join((name or "").split()) for name in (reader.fieldnames or [])]
    missing = [c for c in COLUMNS if c not in reader.fieldnames]
    if missing:
        raise ConversionError(
            f"{path.name}: missing column(s) {missing}; found {reader.fieldnames}")
    rows = []
    for row in reader:
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank line
        rows.append({
            # Quoted multi-line cells span several physical lines; report the
            # line the record ends on (what an editor shows for the row).
            "_line": reader.line_num,
            **{c: (row.get(c) or "").strip() for c in COLUMNS},
            **{c: clean_multiline(row.get(c)) for c in OPTIONAL_COLUMNS},
        })
    return rows


def clean_multiline(raw: str | None) -> str:
    """Trim every line, drop empty ones, keep the line structure (addresses)."""
    lines = [" ".join(line.split()) for line in (raw or "").splitlines()]
    return "\n".join(line for line in lines if line)


def convert_rows(rows: list[dict[str, str]]) -> list[dict]:
    records: list[dict] = []
    seen_terminal: dict[str, int] = {}
    seen_digits: dict[str, int] = {}
    seen_contact: dict[str, int] = {}
    problems: list[str] = []
    for row in rows:
        line = row["_line"]
        terminal_id = row["ANCHORAPPTID"].upper()
        if not _TERMINAL_ID.match(terminal_id):
            problems.append(f"line {line}: terminal id {terminal_id!r} is not 4-12 letters/digits")
            continue
        digits = re.sub(r"\D", "", terminal_id)
        contact = normalise_contact(row["Contact No"])
        if len(contact) != 10:
            problems.append(f"line {line}: contact number must be 10 digits (got {len(contact)})")
            continue
        for key, seen, label in (
            (terminal_id, seen_terminal, "terminal id"),
            (digits, seen_digits, "terminal digit key"),
            (contact, seen_contact, "contact number"),
        ):
            if key in seen:
                problems.append(f"line {line}: duplicate {label} (first on line {seen[key]})")
            else:
                seen[key] = line
        record = {
            "terminal_id": terminal_id,
            "outlet_name": row["MERCHANT"],
            "bank_name": row["Bank Name"],
            "asset_type": row["ASSETMODEL"],
            "contact_number": contact,
            "issue_type": row["Type"],
            "issue_code": slug(row["Type"]),
        }
        empty = [k for k, v in record.items() if not v]
        record["address"] = row.get("Address", "")
        record["contact_person"] = " ".join(row.get("Contact Person", "").split())
        if empty:
            problems.append(f"line {line}: empty value(s) for {empty}")
            continue
        records.append(record)
    if problems:
        raise ConversionError("; ".join(problems))
    return records


def build_document(records: list[dict], source: str) -> dict:
    return {
        "_meta": {
            "source": source,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "record_count": len(records),
            "fields": FIELDS,
            "lookup_keys": LOOKUP_KEYS,
        },
        "records": records,
    }


def convert(csv_path: Path = CSV_PATH) -> dict:
    return build_document(convert_rows(read_rows(csv_path)), csv_path.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--csv", type=Path, default=CSV_PATH)
    parser.add_argument("--out", type=Path, default=JSON_PATH)
    parser.add_argument("--check", action="store_true",
                        help="verify the JSON matches the CSV; write nothing")
    args = parser.parse_args(argv)
    try:
        document = convert(args.csv)
    except ConversionError as exc:
        print(f"conversion refused: {exc}", file=sys.stderr)
        return 2
    if args.check:
        try:
            current = json.loads(args.out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{args.out}: unreadable ({exc})", file=sys.stderr)
            return 1
        if current.get("records") != document["records"]:
            print(f"{args.out.name} is out of date with {args.csv.name} "
                  f"({len(current.get('records') or [])} vs {len(document['records'])} records)",
                  file=sys.stderr)
            return 1
        print(f"ok   {args.out.name} matches {args.csv.name} ({len(document['records'])} records)")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"wrote {args.out} ({len(document['records'])} records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
