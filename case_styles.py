"""Fetch and cache case styles from search.txcourts.gov.

A "style" is the case-caption short form, e.g.
"Don Michael Snider v. The State of Texas". This module:

  * Maintains a `case_styles` table in the PDRbot SQLite database.
  * Exposes get_or_fetch_style(conn, case_number) for use by other code
    (the scraper calls this when it ingests a new case).
  * Provides a backfill_all() entry point to populate every case_number
    currently in the `opinions` table that lacks a style.

Schema:

    CREATE TABLE case_styles (
        case_number  TEXT PRIMARY KEY,
        style        TEXT,         -- "Appellant v. Appellee" or NULL on hard failure
        appellant    TEXT,
        appellee     TEXT,
        source       TEXT,         -- 'txcourts' or 'manual'
        fetched_at   TIMESTAMP DEFAULT (datetime('now','localtime')),
        http_status  INTEGER,
        notes        TEXT
    );
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from html import unescape
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CASE_URL_FMT = "https://search.txcourts.gov/Case.aspx?cn={cn}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Two consecutive form rows: a Style: label followed by an appellant span10,
# then a v.: label followed by an appellee span10. Tolerate whitespace.
_STYLE_RE = re.compile(
    r"Style:\s*</label>.*?<div\s+class=\"span10\">\s*(?P<appellant>.*?)\s*</div>"
    r".*?v\.:\s*</label>.*?<div\s+class=\"span10\">\s*(?P<appellee>.*?)\s*</div>",
    re.S | re.I,
)


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS case_styles (
            case_number  TEXT PRIMARY KEY,
            style        TEXT,
            appellant    TEXT,
            appellee     TEXT,
            source       TEXT,
            fetched_at   TIMESTAMP DEFAULT (datetime('now','localtime')),
            http_status  INTEGER,
            notes        TEXT
        )
        """
    )
    conn.commit()


def _clean(s: str) -> str:
    s = unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip(" .,;: ")
    return s


def parse_style(html: str) -> tuple[Optional[str], Optional[str]]:
    """Return (appellant, appellee) parsed from a Case.aspx HTML body.

    For original proceedings ("In Re" mandamus/habeas filings), the
    appellee field is empty; the caller treats just the appellant as the
    full style.
    """
    m = _STYLE_RE.search(html)
    if not m:
        return None, None
    appellant = _clean(m.group("appellant"))
    appellee = _clean(m.group("appellee"))
    return appellant or None, appellee or None


def fetch_style(
    case_number: str, session: Optional[requests.Session] = None, timeout: int = 20
) -> tuple[Optional[str], Optional[str], int, Optional[str]]:
    """Fetch the Case.aspx page for case_number and parse its style.

    Returns (appellant, appellee, http_status, error_notes).
    """
    sess = session or requests.Session()
    if session is None:
        sess.headers.update(_HEADERS)
    url = CASE_URL_FMT.format(cn=case_number)
    try:
        r = sess.get(url, timeout=timeout)
    except requests.RequestException as e:
        return None, None, 0, f"request error: {e}"
    if r.status_code != 200:
        return None, None, r.status_code, f"non-200 status"
    appellant, appellee = parse_style(r.text)
    if not appellant:
        return None, None, r.status_code, "Style label not found in HTML"
    # Original-proceeding cases ("In Re ...") legitimately have no appellee.
    return appellant, appellee, r.status_code, None


def get_cached_style(conn: sqlite3.Connection, case_number: str) -> Optional[str]:
    row = conn.execute(
        "SELECT style FROM case_styles WHERE case_number = ?", (case_number,)
    ).fetchone()
    return row[0] if row else None


def save_style(
    conn: sqlite3.Connection,
    case_number: str,
    appellant: Optional[str],
    appellee: Optional[str],
    http_status: int,
    source: str = "txcourts",
    notes: Optional[str] = None,
) -> None:
    style: Optional[str] = None
    if appellant and appellee:
        style = f"{appellant} v. {appellee}"
    elif appellant:
        # "In Re" original-proceeding caption — appellee is blank by design.
        style = appellant
    conn.execute(
        """
        INSERT INTO case_styles
            (case_number, style, appellant, appellee, source, fetched_at,
             http_status, notes)
        VALUES (?, ?, ?, ?, ?, datetime('now','localtime'), ?, ?)
        ON CONFLICT(case_number) DO UPDATE SET
            style        = excluded.style,
            appellant    = excluded.appellant,
            appellee     = excluded.appellee,
            source       = excluded.source,
            fetched_at   = excluded.fetched_at,
            http_status  = excluded.http_status,
            notes        = excluded.notes
        """,
        (case_number, style, appellant, appellee, source, http_status, notes),
    )
    conn.commit()


def get_or_fetch_style(
    conn: sqlite3.Connection,
    case_number: str,
    session: Optional[requests.Session] = None,
    force: bool = False,
) -> Optional[str]:
    """Return cached style, fetching from txcourts if not yet cached.

    `force=True` re-fetches even when a cached row exists.
    """
    if not force:
        cached = get_cached_style(conn, case_number)
        if cached:
            return cached
    appellant, appellee, status, notes = fetch_style(case_number, session=session)
    save_style(conn, case_number, appellant, appellee, status, notes=notes)
    if appellant and appellee:
        return f"{appellant} v. {appellee}"
    if appellant:
        return appellant
    return None


def backfill_all(
    db_path: str,
    delay: float = 0.5,
    only_missing: bool = True,
    limit: Optional[int] = None,
) -> dict:
    """Populate case_styles for every case_number in the opinions table.

    With `only_missing=True` (the default), cases that already have a
    style cached are skipped. With `only_missing=False`, every case is
    re-fetched.

    `delay` is the inter-request sleep in seconds.
    """
    conn = sqlite3.connect(db_path)
    ensure_table(conn)

    where = ""
    if only_missing:
        where = (
            "WHERE o.case_number NOT IN ("
            "  SELECT case_number FROM case_styles WHERE style IS NOT NULL"
            ")"
        )
    sql = f"""
        SELECT DISTINCT o.case_number
        FROM opinions o
        {where}
        ORDER BY o.opinion_date DESC, o.case_number
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    targets = [r[0] for r in conn.execute(sql).fetchall()]

    session = requests.Session()
    session.headers.update(_HEADERS)

    stats = {"attempted": 0, "saved": 0, "failed": 0}
    for i, cn in enumerate(targets, 1):
        appellant, appellee, status, notes = fetch_style(cn, session=session)
        save_style(conn, cn, appellant, appellee, status, notes=notes)
        stats["attempted"] += 1
        if appellant:
            stats["saved"] += 1
        else:
            stats["failed"] += 1
        if i % 25 == 0 or i == len(targets):
            logger.info("style backfill %d/%d (saved=%d failed=%d)",
                        i, len(targets), stats["saved"], stats["failed"])
        time.sleep(delay)
    conn.close()
    return stats


def _cli() -> int:
    import argparse
    import os
    from pathlib import Path

    p = argparse.ArgumentParser(description="Backfill case styles from txcourts.")
    p.add_argument(
        "--db", default=str(Path.home() / "pdrbot-data" / "pdrbot.db"),
        help="Path to pdrbot.db"
    )
    p.add_argument("--delay", type=float, default=0.5,
                   help="Seconds between requests (default 0.5)")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after N cases (default: all)")
    p.add_argument("--all", action="store_true",
                   help="Refetch every case, including ones already cached")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    if not os.path.exists(args.db):
        print(f"ERROR: db not found: {args.db}")
        return 1
    stats = backfill_all(
        db_path=args.db,
        delay=args.delay,
        only_missing=not args.all,
        limit=args.limit,
    )
    print(f"Backfill done: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
