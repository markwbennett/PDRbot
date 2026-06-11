#!/usr/bin/env python3
"""Backfill disposition + state_is_appellant onto existing analyses.

Iterates over main opinions (op / mem / combined) in the rendering
window whose cached disposition column is NULL. Sends opinion text to
Haiku with a tight JSON-schema prompt and stores the result in the
analysis.disposition / analysis.state_is_appellant columns.

Idempotent: skips rows that already have a cached disposition.

Run on iacls as `ubuntu`, with the virtualenv active so mwb_claude is
importable.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

# Wire up project + mwb_common imports the same way pdrbot.py does.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.path.expanduser("~/github/mwb_common"))

from mwb_claude import call_claude_with_retry  # noqa: E402
from PyPDF2 import PdfReader  # noqa: E402

PDFS_ROOT = Path.home() / "pdrbot-data"
DEFAULT_DB = PDFS_ROOT / "pdrbot.db"

DISPOSITION_ENUM = [
    "affirmed",
    "reversed",
    "reversed_in_part",
    "modified_and_affirmed",
    "vacated",
    "remanded",
    "dismissed",
    "petition_granted",
    "petition_denied",
    "abated",
    "other",
]

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "state_is_appellant": {
            "type": "boolean",
            "description": (
                "True when the State of Texas is the appellant — i.e., the "
                "State, not the defendant, brought the appeal (article 44.01 "
                "interlocutory). False for ordinary defense appeals and for "
                "original proceedings styled 'In re ...'."
            ),
        },
        "disposition": {
            "type": "string",
            "enum": DISPOSITION_ENUM,
            "description": (
                "Court's order on the appellant's appeal. 'reversed' covers "
                "reversed-and-remanded, reversed-and-rendered, and reversed-"
                "and-acquitted. 'reversed_in_part' only when the court "
                "reversed some counts/issues and affirmed others. "
                "'modified_and_affirmed' when the court modified the "
                "judgment but the conviction stands. 'dismissed' for "
                "jurisdictional dismissals. 'petition_granted'/"
                "'petition_denied' for mandamus / habeas original "
                "proceedings. 'other' only when nothing else fits."
            ),
        },
    },
    "required": ["state_is_appellant", "disposition"],
}

PROMPT = (
    "You are extracting two facts from a Texas Court of Appeals criminal "
    "opinion. Return JSON only — no preamble, no commentary.\n\n"
    "Fact 1 — state_is_appellant: was the State of Texas the appellant "
    "(true) or was the appellant the defendant or a private relator (false)? "
    "Check page 1 of the opinion. The appellant is named first. For "
    "mandamus / habeas styled 'In re X', state_is_appellant is false.\n\n"
    "Fact 2 — disposition: what did the court of appeals order? Pick exactly "
    "one of: affirmed, reversed, reversed_in_part, modified_and_affirmed, "
    "vacated, remanded, dismissed, petition_granted, petition_denied, "
    "abated, other.\n\n"
    "  - reversed: judgment reversed-and-remanded, reversed-and-rendered, or "
    "reversed-and-acquitted (any full reversal).\n"
    "  - reversed_in_part: court reversed some counts or issues and affirmed "
    "others.\n"
    "  - modified_and_affirmed: judgment modified (e.g., struck a fee, court "
    "cost, or finding) but the conviction stands.\n"
    "  - dismissed: jurisdictional dismissal (untimely notice, plea-bargain "
    "waiver, Anders dismissal).\n"
    "  - petition_granted / petition_denied: mandamus or habeas original "
    "proceedings only.\n"
    "  - other: only when no listed value fits.\n\n"
    "Look at the court's final order — typically the last paragraph of the "
    "opinion and the boxed disposition line at the top of the opinion. "
    "Examples of court language: 'We affirm.' -> affirmed. 'Reversed and "
    "Remanded.' -> reversed. 'We modify the judgment to delete the court-"
    "cost finding and as modified, affirm.' -> modified_and_affirmed. "
    "'Dismissed for want of jurisdiction.' -> dismissed."
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("backfill_disposition")


def extract_pdf_text(file_path: str) -> str | None:
    pdf_path = PDFS_ROOT / file_path.removeprefix("data/")
    if not pdf_path.is_file():
        return None
    try:
        reader = PdfReader(str(pdf_path))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:
        logger.warning(f"PDF read failed for {pdf_path}: {e}")
        return None


def trim_text(text: str, head: int = 6000, tail: int = 6000) -> str:
    """Send page-1 plus the final-paragraph region — enough to capture both
    the caption (who is the appellant) and the mandate (disposition). Most
    COA opinions are 5–30k chars, so this almost always sends the full thing."""
    if len(text) <= head + tail + 100:
        return text
    return text[:head] + "\n\n[…opinion middle elided…]\n\n" + text[-tail:]


def haiku_extract(text: str, case_number: str) -> dict | None:
    body = trim_text(text)
    full_prompt = f"{PROMPT}\n\n--- OPINION TEXT ---\n{body}"
    try:
        raw = call_claude_with_retry(
            prompt=full_prompt,
            timeout=90,
            max_retries=3,
            base_delay=3,
            model="claude-haiku-4-5",
            json_schema=EXTRACT_SCHEMA,
        )
    except Exception as e:
        logger.warning(f"{case_number}: Haiku call failed: {e}")
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning(f"{case_number}: invalid JSON from Haiku: {e}; body={raw[:200]!r}")
        return None
    if not isinstance(data.get("state_is_appellant"), bool):
        return None
    if data.get("disposition") not in DISPOSITION_ENUM:
        return None
    return data


def select_candidates(conn: sqlite3.Connection, days: int | None, limit: int | None) -> list[tuple]:
    where = ["a.disposition IS NULL"]
    params: list = []
    if days is not None:
        where.append("o.opinion_date >= date('now', ?)")
        params.append(f"-{days} days")
    # primary opinion-type token must be op / mem / combined.
    where.append(
        "(o.opinion_type IN ('op','mem','combined') "
        "OR o.opinion_type LIKE 'op+%' "
        "OR o.opinion_type LIKE 'mem+%' "
        "OR o.opinion_type LIKE 'combined+%')"
    )
    sql = f"""
        SELECT a.id, o.case_number, o.opinion_date, o.file_path
        FROM analysis a
        JOIN opinions o ON o.id = a.opinion_id
        WHERE {' AND '.join(where)}
        ORDER BY o.opinion_date DESC, a.id ASC
    """
    if limit is not None:
        sql += f" LIMIT {limit}"
    return list(conn.execute(sql, params))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--days", type=int, default=60,
                   help="Backfill only opinions released within the last N days. 0 = all.")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after N rows (for smoke testing).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be backfilled, but make no LLM calls or DB writes.")
    args = p.parse_args()

    if not args.db.exists():
        logger.error(f"DB not found: {args.db}")
        return 1

    days = None if args.days == 0 else args.days
    conn = sqlite3.connect(args.db)
    rows = select_candidates(conn, days, args.limit)
    logger.info(f"{len(rows)} analyses to backfill (days={days}, limit={args.limit})")

    if args.dry_run:
        for analysis_id, case_no, date_, fp in rows[:20]:
            logger.info(f"  would backfill analysis {analysis_id}  {date_}  {case_no}  {fp}")
        if len(rows) > 20:
            logger.info(f"  … and {len(rows) - 20} more")
        return 0

    done = 0
    skipped_no_text = 0
    failed = 0
    t0 = time.time()
    for analysis_id, case_no, date_, fp in rows:
        text = extract_pdf_text(fp)
        if not text:
            skipped_no_text += 1
            logger.info(f"  [{done+failed+skipped_no_text}/{len(rows)}] {case_no} skip — no PDF text")
            continue
        result = haiku_extract(text, case_no)
        if not result:
            failed += 1
            logger.info(f"  [{done+failed+skipped_no_text}/{len(rows)}] {case_no} FAILED")
            continue
        conn.execute(
            "UPDATE analysis SET disposition = ?, state_is_appellant = ? WHERE id = ?",
            (result["disposition"], 1 if result["state_is_appellant"] else 0, analysis_id),
        )
        conn.commit()
        done += 1
        dw_marker = ""
        if result["state_is_appellant"] and result["disposition"] == "affirmed":
            dw_marker = "  ← DEFENSE WIN (State appeal affirmed)"
        elif (not result["state_is_appellant"]
              and result["disposition"] in ("reversed", "reversed_in_part", "vacated")):
            dw_marker = "  ← DEFENSE WIN"
        logger.info(
            f"  [{done+failed+skipped_no_text}/{len(rows)}] {case_no} {date_} "
            f"state={result['state_is_appellant']} disp={result['disposition']}{dw_marker}"
        )

    dt = time.time() - t0
    logger.info(
        f"backfill done — {done} updated, {failed} failed, {skipped_no_text} no-text "
        f"in {dt:.1f}s ({dt/max(done,1):.1f}s/row)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
