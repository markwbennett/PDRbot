"""Shared data-loading, consolidation, and typography helpers used by
both `generate_slip_opinions.py` and `generate_triage.py`.

Both daily generators answer the same first question — "what opinions
did the fourteen Texas Courts of Appeals release on each day, and which
cause numbers were decided together?" — and only differ in how they
present those answers. The split keeps the SQL, PDF inspection, and
companion-case logic in one place so the two pages can never disagree
about whether four cause numbers were one opinion.

Public surface:
    - constants: COURT_NAMES, TYPE_LABEL, TYPE_ORDER, PDFS_ROOT,
      DEFAULT_DB
    - data:       load_rows(db_path, days) -> list[sqlite3.Row]
                  group_rows(rows) -> dict[date, dict[court, list[entry]]]
    - typography: _roman, _ordinal_suffix, _edition_line,
                  _split_court_label
    - misc:       normalize_style, split_composite, opinion_label
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

DEFAULT_DB = Path.home() / "pdrbot-data" / "pdrbot.db"
# PDRbot's data/ symlink resolves to ~/pdrbot-data, and file_path values
# in the opinions table are stored relative to that ("data/YYYYMMDD/...pdf").
PDFS_ROOT = Path.home() / "pdrbot-data"

COURT_NAMES = {
    "COA01": "First Court of Appeals (Houston)",
    "COA02": "Second Court of Appeals (Fort Worth)",
    "COA03": "Third Court of Appeals (Austin)",
    "COA04": "Fourth Court of Appeals (San Antonio)",
    "COA05": "Fifth Court of Appeals (Dallas)",
    "COA06": "Sixth Court of Appeals (Texarkana)",
    "COA07": "Seventh Court of Appeals (Amarillo)",
    "COA08": "Eighth Court of Appeals (El Paso)",
    "COA09": "Ninth Court of Appeals (Beaumont)",
    "COA10": "Tenth Court of Appeals (Waco)",
    "COA11": "Eleventh Court of Appeals (Eastland)",
    "COA12": "Twelfth Court of Appeals (Tyler)",
    "COA13": "Thirteenth Court of Appeals (Corpus Christi-Edinburg)",
    "COA14": "Fourteenth Court of Appeals (Houston)",
}

TYPE_LABEL = {
    "op":       "Opinion",
    "mem":      "Memorandum opinion",
    "con":      "Concurrence",
    "dis":      "Dissent",
    "combined": "Combined opinion",
}

# Sort order within a case (lower sorts first).
TYPE_ORDER = {"op": 0, "mem": 0, "combined": 0, "con": 1, "dis": 2}


# ─────────────────────────── opinion-row split ─────────────────────────────

def _justice_label(token_value: str | None) -> str:
    if not token_value:
        return ""
    name = re.sub(r"^(?:con|dis|op|mem)_", "", token_value, flags=re.IGNORECASE)
    return name.replace("_", " ").title()


def split_composite(
    opinion_type: str,
    justice_field: str | None,
    pdf_url_field: str | None,
) -> list[dict]:
    """Split a composite opinion row into one entry per token.

    PDRbot stitches separate court-released PDFs into a single local
    file, but the database still records the original txcourts URLs as a
    ';'-joined list in pdf_url, aligned with the '+'-joined tokens in
    opinion_type. Justices appear as a ';'-joined list of '{abbrev}_{name}'
    entries; main ('op'/'mem') tokens have no justice entry.

    Returns: [{abbrev, label, justice, pdf_url}, ...]
    """
    tokens = [t for t in (opinion_type or "").split("+") if t] or [opinion_type or "op"]
    urls = (pdf_url_field or "").split(";") if pdf_url_field else []
    urls = [u.strip() for u in urls]
    while len(urls) < len(tokens):
        urls.append("")

    justice_queue: dict[str, list[str]] = {}
    for part in (justice_field or "").split(";"):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(con|dis|op|mem)_(.+)$", part, flags=re.IGNORECASE)
        if m:
            abbrev = m.group(1).lower()
            justice_queue.setdefault(abbrev, []).append(part)
        else:
            justice_queue.setdefault("", []).append(part)

    out: list[dict] = []
    for token, url in zip(tokens, urls):
        token_l = token.lower()
        raw_just = None
        if token_l in justice_queue and justice_queue[token_l]:
            raw_just = justice_queue[token_l].pop(0)
        label = TYPE_LABEL.get(token_l, token)
        nicename = _justice_label(raw_just)
        if nicename:
            label = f"{label} by Justice {nicename}"
        out.append(
            {
                "abbrev":  token_l,
                "label":   label,
                "justice": nicename or None,
                "pdf_url": url or None,
            }
        )
    return out


# ─────────────────────────── style resolution ──────────────────────────────

# Best-effort scrape of an appellant name from PDRbot's analysis_text.
# JSON-form analyses store {"appellant_name": "..."}; prose-form reports
# occasionally print the name in a labeled line.
_APPELLANT_RE = re.compile(
    r"(?:\"appellant_name\"\s*:\s*\"([^\"]+)\""
    r"|Appellant Name[^:]*:\s*([^\n]+)"
    r"|^Appellant:\s*([^\n]+))",
    flags=re.MULTILINE,
)


def extract_appellant(analysis_text: str | None) -> str | None:
    if not analysis_text:
        return None
    if analysis_text.lstrip().startswith("{"):
        try:
            data = json.loads(analysis_text)
            name = data.get("appellant_name")
            if name:
                return name.strip()
        except json.JSONDecodeError:
            pass
    m = _APPELLANT_RE.search(analysis_text)
    if not m:
        return None
    return (m.group(1) or m.group(2) or m.group(3) or "").strip() or None


def extract_disposition_meta(
    analysis_text: str | None,
    cached_disposition: str | None = None,
    cached_state_is_appellant: int | None = None,
) -> dict:
    """Return {disposition, state_is_appellant}. Prefers the backfill-
    cached columns when present; falls back to parsing analysis_text JSON.
    Returns empty dict when neither source yields a value.
    """
    out: dict = {}
    if cached_disposition:
        out["disposition"] = cached_disposition
    if cached_state_is_appellant is not None:
        out["state_is_appellant"] = bool(cached_state_is_appellant)
    if out.get("disposition") and "state_is_appellant" in out:
        return out
    if not analysis_text or not analysis_text.lstrip().startswith("{"):
        return out
    try:
        data = json.loads(analysis_text)
    except (TypeError, json.JSONDecodeError):
        return out
    if "disposition" not in out and isinstance(data.get("disposition"), str):
        out["disposition"] = data["disposition"]
    if "state_is_appellant" not in out and isinstance(data.get("state_is_appellant"), bool):
        out["state_is_appellant"] = data["state_is_appellant"]
    return out


_REVERSAL_DISPOS = {"reversed", "reversed_in_part", "vacated"}


def is_defense_win(disposition: str | None, state_is_appellant: bool | None) -> bool:
    """Defense wins when the State appealed and lost (affirmance), or
    when the defense appealed and won (reversal / vacatur)."""
    if disposition is None or state_is_appellant is None:
        return False
    if state_is_appellant:
        return disposition == "affirmed"
    return disposition in _REVERSAL_DISPOS


def normalize_style(name: str) -> str:
    """Turn a raw appellant name into a case-style string.

    Most Texas COA criminal cases are styled "[Appellant] v. The State
    of Texas". Original-proceeding (mandamus/habeas) captions are
    already "In Re ..." and are returned unchanged. Handles
    "Last, First" → "First Last".
    """
    name = name.strip().strip(".")
    if not name:
        return name
    lower = name.lower()
    if lower.startswith("in re") or " v. " in lower or " v " in lower:
        return name
    if "," in name:
        last, first = [p.strip() for p in name.split(",", 1)]
        if last and first:
            name = f"{first} {last}"
    return f"{name} v. The State of Texas"


# ───────────────────── PDF inspection (cache to DB) ────────────────────────

def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(opinions)")}
    if "pdf_md5" not in cols:
        conn.execute("ALTER TABLE opinions ADD COLUMN pdf_md5 TEXT")
    if "caption_cases" not in cols:
        conn.execute("ALTER TABLE opinions ADD COLUMN caption_cases TEXT")
    conn.commit()


# Texas COA cause numbers are XX-XX-NNNNN-CR. The court's PDF rendering
# often introduces stray whitespace inside the number, so we extract
# from a whitespace-stripped copy of the first-page text.
_CN_RE = re.compile(r"\b(\d{2}-\d{2}-\d{5}-CR)\b")


def _caption_cases_for(path: Path) -> list[str] | None:
    """Return cause numbers listed in the first-page caption of a PDF,
    or None if the file is unreadable. May return an empty list when
    extraction succeeds but no caption numbers are found.
    """
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
        if not reader.pages:
            return []
        text = reader.pages[0].extract_text() or ""
    except Exception:
        return None
    compact = re.sub(r"\s+", "", text)
    seen: list[str] = []
    for m in _CN_RE.finditer(compact):
        cn = m.group(1)
        if cn not in seen:
            seen.append(cn)
    return seen


def _backfill_pdf_signals(conn: sqlite3.Connection, days: int | None) -> tuple[int, int]:
    """Compute MD5 hashes and caption cause-number lists for any opinion
    PDFs in the rendering window that lack them. Returns (n_hashed,
    n_captioned). Captions are the primary companion-case signal — when
    two cases' opinions list the same set of cause numbers in the
    caption, those cases were decided in a single opinion. Hashes act
    as a fallback for opinions whose first-page text fails extraction.
    """
    where = "AND o.opinion_date >= date('now', ?)" if days is not None else ""
    params: tuple = (f"-{days} days",) if days is not None else ()
    rows = conn.execute(
        f"""
        SELECT o.id, o.file_path, o.pdf_md5, o.caption_cases
        FROM opinions o
        WHERE ((o.pdf_md5 IS NULL OR o.pdf_md5 = '')
                OR o.caption_cases IS NULL)
          AND o.file_path IS NOT NULL
          {where}
        """,
        params,
    ).fetchall()
    n_hash = 0
    n_cap = 0
    for opinion_id, file_path, existing_md5, existing_caption in rows:
        if not file_path:
            continue
        path = PDFS_ROOT / file_path.removeprefix("data/")
        if not path.is_file():
            continue
        new_md5 = existing_md5
        if not existing_md5:
            try:
                new_md5 = hashlib.md5(path.read_bytes()).hexdigest()
                n_hash += 1
            except OSError:
                new_md5 = None
        new_caption_json = existing_caption
        if existing_caption is None:
            caption = _caption_cases_for(path)
            if caption is not None:
                new_caption_json = json.dumps(caption)
                n_cap += 1
        conn.execute(
            "UPDATE opinions SET pdf_md5 = COALESCE(?, pdf_md5), "
            "caption_cases = COALESCE(?, caption_cases) WHERE id = ?",
            (new_md5, new_caption_json, opinion_id),
        )
    if n_hash or n_cap:
        conn.commit()
    return n_hash, n_cap


# ─────────────────────────── data loaders ──────────────────────────────────

def load_rows(db_path: Path, days: int | None) -> list[sqlite3.Row]:
    """Return one row per opinion in the rendering window. Side-effect:
    fills any missing pdf_md5 / caption_cases for opinions in window."""
    conn = sqlite3.connect(db_path)
    _ensure_columns(conn)
    n_hash, n_cap = _backfill_pdf_signals(conn, days)
    if n_hash or n_cap:
        print(f"pdf signals: hashed {n_hash}, captioned {n_cap}")
    conn.row_factory = sqlite3.Row

    has_case_styles = bool(
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='case_styles'"
        ).fetchone()
    )
    style_join = (
        "LEFT JOIN case_styles cs ON cs.case_number = o.case_number"
        if has_case_styles else ""
    )
    style_col = "cs.style AS cached_style," if has_case_styles else "NULL AS cached_style,"

    where = ""
    params: tuple = ()
    if days is not None:
        where = "WHERE o.opinion_date >= date('now', ?)"
        params = (f"-{days} days",)

    sql = f"""
        SELECT
            o.opinion_date,
            o.court,
            o.case_number,
            o.opinion_type,
            o.justice_name,
            o.pdf_url,
            o.case_url,
            a.analysis_text,
            a.disposition AS cached_disposition,
            a.state_is_appellant AS cached_state_is_appellant,
            r.party_name AS rep_party,
            o.pdf_md5,
            o.caption_cases,
            {style_col}
            o.id AS opinion_id
        FROM opinions o
        LEFT JOIN analysis a ON a.opinion_id = o.id
        LEFT JOIN (
            SELECT case_number, party_name
            FROM representatives
            WHERE party_type LIKE '%Appellant%'
            GROUP BY case_number
        ) r ON r.case_number = o.case_number
        {style_join}
        {where}
        ORDER BY o.opinion_date DESC, o.court ASC, o.case_number ASC
    """
    return conn.execute(sql, params).fetchall()


def group_rows(rows):
    """Return nested mapping: date → court → [entries].

    An "entry" is one or more case_numbers that share the exact same set
    of opinion PDFs. The Third Court's habit of issuing a single
    opinion across companion cases collapses into one entry.

    Each entry dict has:
        case_numbers : list[str]   sorted
        case_urls    : list[str]   parallel
        style        : str | None
        opinions     : list[dict]  {abbrev, label, justice, pdf_url,
                                    pdf_md5, caption_cases, opinion_id}
        opinion_ids  : list[int]   row ids backing this entry's opinions
                                   (one per opinion, across all member
                                    cases — useful for joining analysis)
    """
    cases: dict[date, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        try:
            d = datetime.strptime(r["opinion_date"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        court = r["court"]
        case_no = r["case_number"]
        bucket = cases[d][court].setdefault(
            case_no,
            {
                "case_number":         case_no,
                "case_url":            r["case_url"],
                "style":               None,
                "opinions":            [],
                "opinion_ids":         [],
                "disposition":         None,
                "state_is_appellant":  None,
            },
        )
        if not bucket["style"]:
            cached = r["cached_style"] if "cached_style" in r.keys() else None
            if cached:
                bucket["style"] = cached
            else:
                party = r["rep_party"]
                if party:
                    bucket["style"] = normalize_style(party)
                else:
                    ap = extract_appellant(r["analysis_text"])
                    if ap:
                        bucket["style"] = normalize_style(ap)

        primary_token = (r["opinion_type"] or "op").split("+", 1)[0].lower()
        if primary_token in ("op", "mem", "combined"):
            row_keys = r.keys()
            meta = extract_disposition_meta(
                r["analysis_text"],
                r["cached_disposition"] if "cached_disposition" in row_keys else None,
                r["cached_state_is_appellant"] if "cached_state_is_appellant" in row_keys else None,
            )
            if bucket["disposition"] is None and meta.get("disposition"):
                bucket["disposition"] = meta["disposition"]
            if bucket["state_is_appellant"] is None and "state_is_appellant" in meta:
                bucket["state_is_appellant"] = meta["state_is_appellant"]

        try:
            caption_cases = json.loads(r["caption_cases"]) if r["caption_cases"] else []
        except (TypeError, json.JSONDecodeError):
            caption_cases = []
        for entry in split_composite(
            r["opinion_type"] or "op",
            r["justice_name"],
            r["pdf_url"],
        ):
            entry["pdf_md5"] = r["pdf_md5"]
            entry["caption_cases"] = caption_cases
            entry["opinion_id"] = r["opinion_id"]
            bucket["opinions"].append(entry)
        bucket["opinion_ids"].append(r["opinion_id"])

    # Sort opinion entries within each case.
    for date_map in cases.values():
        for court_map in date_map.values():
            for case in court_map.values():
                case["opinions"].sort(
                    key=lambda o: (
                        TYPE_ORDER.get(o["abbrev"], 9),
                        o["justice"] or "",
                    )
                )

    # Consolidate companion cases via union-find. Captions are the
    # primary signal; identical PDF bytes are the fallback when caption
    # parsing returns nothing.
    out: dict[date, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for d, date_map in cases.items():
        for court, court_map in date_map.items():
            local = sorted(court_map.keys())
            parent = {cn: cn for cn in local}

            def find(x: str) -> str:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a: str, b: str) -> None:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

            for cn in local:
                for op in court_map[cn]["opinions"]:
                    listed = op.get("caption_cases") or []
                    for other in listed:
                        if other in parent:
                            union(cn, other)

            hash_buckets: dict[tuple, list[str]] = defaultdict(list)
            for cn in local:
                ops = court_map[cn]["opinions"]
                has_caption = any((op.get("caption_cases") or []) for op in ops)
                if has_caption:
                    continue
                hashes = tuple(op.get("pdf_md5") for op in ops)
                if hashes and all(hashes):
                    hash_buckets[hashes].append(cn)
            for members in hash_buckets.values():
                if len(members) > 1:
                    first = members[0]
                    for other in members[1:]:
                        union(first, other)

            groups: dict[str, list[str]] = defaultdict(list)
            for cn in local:
                groups[find(cn)].append(cn)

            entries: list[dict] = []
            for root, members in groups.items():
                members.sort()
                first = court_map[members[0]]
                style = next((court_map[m]["style"] for m in members if court_map[m]["style"]), None)
                # Union opinion_ids across all member cases (each member
                # has its own opinion row(s); their analyses are
                # equivalent — but we keep all ids for downstream joins).
                opinion_ids: list[int] = []
                for m in members:
                    opinion_ids.extend(court_map[m]["opinion_ids"])
                disposition = next(
                    (court_map[m]["disposition"] for m in members if court_map[m]["disposition"]),
                    None,
                )
                state_is_appellant = next(
                    (court_map[m]["state_is_appellant"] for m in members
                     if court_map[m]["state_is_appellant"] is not None),
                    None,
                )
                entries.append({
                    "case_numbers":       members,
                    "case_urls":          [court_map[m]["case_url"] for m in members],
                    "style":              style,
                    "opinions":           first["opinions"],
                    "opinion_ids":        opinion_ids,
                    "disposition":        disposition,
                    "state_is_appellant": state_is_appellant,
                    "defense_win":        is_defense_win(disposition, state_is_appellant),
                })
            entries.sort(key=lambda e: e["case_numbers"][0])
            out[d][court] = entries
    return out


# ────────────────────────── typography helpers ─────────────────────────────

_ROMAN_DIGITS = (
    ("M", 1000), ("CM", 900), ("D", 500), ("CD", 400),
    ("C", 100),  ("XC", 90),  ("L", 50),  ("XL", 40),
    ("X", 10),   ("IX", 9),   ("V", 5),   ("IV", 4), ("I", 1),
)


def _roman(n: int) -> str:
    out = []
    for sym, val in _ROMAN_DIGITS:
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)


def _ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _edition_line(today: date) -> str:
    """Newspaper-style masthead eyebrow: Vol. (Roman year) · No. (day of year)."""
    return f"Vol. {_roman(today.year)} · No. {today.timetuple().tm_yday:03d}"


def _split_court_label(name: str) -> str:
    # "First Court of Appeals (Houston)" -> "First Court of Appeals · Houston"
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", name)
    if m:
        return f"{m.group(1).strip()} · {m.group(2).strip()}"
    return name


def issue_date_line(today: date) -> str:
    return (
        today.strftime("%A, the %-d")
        + _ordinal_suffix(today.day)
        + " of "
        + today.strftime("%B")
        + " "
        + _roman(today.year)
    )


# Common masthead palette + paper background CSS reused by both renderers.
# Each generator concatenates its own page-specific selectors below this.
BASE_STYLE = r"""
  :root {
    color-scheme: light dark;
    /* Contrast-bumped palette: background ~25% darker, foregrounds ~25%
       toward their high-contrast end. Muted ROUTINE text and rules
       remain visibly distinct from body, just no longer faded. */
    --paper:       #e3d8be;
    --paper-edge:  #d6c9ac;
    --ink:         #0d0a06;
    --ink-soft:    #261e13;
    --rule:        #564938;
    --rule-soft:   #8e8270;
    --accent:      #6e1820;
    --accent-soft: #893338;
    --mute:        #443a2c;

    --serif-display: "Cormorant Garamond", "EB Garamond", "Times New Roman", serif;
    --serif-body:    "EB Garamond", "Cormorant Garamond", "Times New Roman", serif;
    --serif-sc:      "Cormorant SC", "Cormorant Garamond", serif;
    --mono:          "IBM Plex Mono", ui-monospace, Menlo, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper:       #08060a;
      --paper-edge:  #0e0b07;
      --ink:         #faf2db;
      --ink-soft:    #ddd1b3;
      --rule:        #a99c84;
      --rule-soft:   #6a5f4a;
      --accent:      #e88c92;
      --accent-soft: #c6707a;
      --mute:        #bcae93;
    }
  }

  *, *::before, *::after { box-sizing: border-box; }
  /* Root size — every 1rem in this stylesheet resolves to this.
     The font-size slider at top-right overrides this inline at
     runtime and persists the user's choice in localStorage. */
  html { font-size: 32px; }

  /* Font-size slider — fixed at top-right, unobtrusive. */
  .fs-control {
    position: fixed;
    top: .65rem; right: .65rem;
    z-index: 100;
    display: flex; align-items: center; gap: .55rem;
    padding: .35rem .6rem;
    background: var(--paper-edge);
    border: 1px solid var(--rule);
    border-radius: 4px;
    font-family: var(--serif-sc);
    font-size: .55rem;
    color: var(--ink);
    box-shadow: 0 2px 6px rgba(0,0,0,.08);
  }
  .fs-control__label {
    letter-spacing: .15em; text-transform: uppercase;
    white-space: nowrap;
  }
  .fs-control__slider {
    width: 130px; height: 1rem;
    accent-color: var(--accent);
    cursor: pointer;
  }
  .fs-control__value {
    font-family: var(--mono);
    min-width: 3.2ch; text-align: right;
    color: var(--ink);
  }
  @media print { .fs-control { display: none; } }
  @media (max-width: 540px) {
    .fs-control { font-size: .45rem; padding: .3rem .5rem; gap: .35rem; }
    .fs-control__slider { width: 90px; }
  }
  html, body { background: var(--paper); }
  body {
    margin: 0;
    color: var(--ink);
    font-family: var(--serif-body);
    /* Inherited from html: 40px (= 30pt). */
    font-size: 1rem;
    line-height: 1.55;
    font-feature-settings: "onum" 1, "kern" 1, "liga" 1;
    -webkit-font-smoothing: antialiased;
    background:
      radial-gradient(ellipse at 50% -10%, rgba(125,29,36,0.05), transparent 60%),
      linear-gradient(180deg, var(--paper-edge) 0, var(--paper) 220px, var(--paper) calc(100% - 220px), var(--paper-edge) 100%);
    min-height: 100vh;
  }

  .grain {
    position: fixed; inset: 0; pointer-events: none; z-index: 50;
    opacity: .12; mix-blend-mode: multiply;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0.16  0 0 0 0 0.13  0 0 0 0 0.09  0 0 0 0.6 0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>");
  }
  @media (prefers-color-scheme: dark) {
    .grain { opacity: .25; mix-blend-mode: screen; }
  }
  ::selection { background: rgba(125,29,36,0.22); color: inherit; }

  /* max-width pinned in px so line length doesn't grow proportionally
     with body font size. ~70-80 characters at 24pt body. */
  .sheet { max-width: 1200px; margin: 0 auto; padding: 3.5rem 1.5rem 4rem;
           position: relative; z-index: 1; }

  /* Masthead */
  .masthead { text-align: center; margin-bottom: 2.5rem; }
  .masthead__eyebrow {
    display: flex; justify-content: space-between; align-items: baseline;
    font-family: var(--serif-sc); font-size: 1rem;
    letter-spacing: .14em; text-transform: uppercase;
    color: var(--ink); gap: 1rem;
  }
  .masthead__motto { font-style: italic; text-transform: none;
    letter-spacing: .04em; color: var(--ink); font-family: var(--serif-body); }
  .masthead__rules { margin: .4rem 0; }
  .masthead__rules hr { border: 0; margin: 0; background: var(--ink); }
  hr.thick { height: 3px; }
  hr.thin  { height: 1px; background: var(--rule); margin-top: 3px; }
  .masthead__title {
    font-family: var(--serif-display); font-weight: 700;
    font-size: clamp(2.4rem, 7vw, 4.4rem); line-height: 1;
    letter-spacing: -0.005em; margin: 1.1rem 0 .6rem; color: var(--ink);
    font-variant-ligatures: discretionary-ligatures;
  }
  .masthead__subtitle {
    font-family: var(--serif-body); font-style: italic;
    font-size: 1.1rem; color: var(--ink);
    margin: 0 0 .9rem; line-height: 1.4;
  }

  /* Dateline — vertical strip in the left margin, sticky until the
     next day scrolls in. */
  .day {
    display: grid;
    grid-template-columns: 4rem 1fr;
    column-gap: 1.75rem;
    align-items: start;
    margin: 3.2rem 0 2.2rem;
    animation: rise .6s ease-out both;
    animation-delay: calc(var(--stagger) * 40ms);
  }
  @keyframes rise {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: none; }
  }
  @media (prefers-reduced-motion: reduce) { .day { animation: none; } }

  .day__margin {
    position: relative;
    align-self: stretch;        /* fill the row height so the rule runs */
    min-height: 6rem;
  }
  .day__margin::after {
    content: ""; position: absolute;
    top: 0; bottom: 0; right: 0; width: 1px;
    background: var(--rule-soft);
  }
  .dateline__date {
    position: sticky;
    top: 1.5rem;
    display: inline-block;
    margin: 0; padding: .25rem 0 .25rem .25rem;
    font-family: var(--serif-sc); font-weight: 600;
    font-size: 1.1rem; letter-spacing: .28em;
    color: var(--accent); text-transform: uppercase;
    white-space: nowrap;
    /* Vertical, reading bottom-up like a book spine. */
    writing-mode: vertical-rl;
    transform: rotate(180deg);
    transform-origin: center;
  }
  .day__body { min-width: 0; }   /* let grid item shrink past content width */

  /* Court */
  .court { margin: 1.9rem 0; }
  .court__name {
    font-family: var(--serif-display); font-weight: 600; font-style: italic;
    font-size: 1.22rem; color: var(--ink);
    margin: 0 0 .35rem; padding-bottom: .25rem;
    border-bottom: 1px solid var(--rule-soft);
    text-align: center; letter-spacing: .01em;
  }
  .court__cases { padding: .35rem 0 0; }

  /* Empty / colophon */
  .empty { text-align: center; font-style: italic;
           color: var(--ink); padding: 3rem 0; }
  .colophon { margin-top: 4rem; text-align: center; }
  .colophon__rules hr { border: 0; margin: 0; background: var(--ink); }
  .colophon__line { margin: .9rem 0 .25rem; font-style: italic;
                    color: var(--ink); font-size: 1rem; }
  .colophon__line--small {
    font-family: var(--serif-sc); font-style: normal; font-size: 1rem;
    letter-spacing: .18em; text-transform: uppercase; color: var(--ink);
  }
  .colophon a { color: var(--accent); text-decoration: none;
                border-bottom: 1px dotted var(--accent-soft); }
  .colophon a:hover { color: var(--ink); border-bottom-color: var(--ink); }

  /* Small screens — body stays at 24px to keep ≥18pt floor. On narrow
     viewports drop the vertical margin strip and put the date back
     across the top of each day section. */
  @media (max-width: 640px) {
    .sheet { padding: 2rem 1rem 3rem; }
    .masthead__eyebrow { flex-direction: column; gap: .15rem; }
    .masthead__motto { display: none; }
    .day {
      grid-template-columns: 1fr;
      column-gap: 0;
    }
    .day__margin {
      min-height: 0;
      align-self: auto;
      border-bottom: 1px solid var(--rule-soft);
      padding-bottom: .35rem;
      margin-bottom: .5rem;
    }
    .day__margin::after { display: none; }
    .dateline__date {
      position: static; writing-mode: horizontal-tb; transform: none;
      letter-spacing: .18em; display: block; text-align: center;
      padding: 0;
    }
  }

  /* Print */
  @media print {
    :root { --paper: #fff; --paper-edge: #fff; --ink: #000; --ink-soft: #222;
            --rule: #000; --rule-soft: #999; --accent: #000; --accent-soft: #444;
            --mute: #555; }
    .grain { display: none; }
    body { background: #fff; }
    .day, .court { break-inside: avoid; }
    a[href]:after { content: ""; }
    details { display: block; }
    details > summary { list-style: none; }
  }

  /* Defense-win rubber stamp — used by both slip-opinions and
     triage. Inline next to the case-number line, slight tilt. */
  .win-stamp {
    display: inline-block;
    margin-left: .55rem;
    padding: .05rem .4rem;
    border: 2px solid var(--accent);
    color: var(--accent);
    background: transparent;
    font-family: var(--serif-sc);
    font-weight: 700;
    font-size: .55em;
    letter-spacing: .14em;
    text-transform: uppercase;
    line-height: 1;
    transform: rotate(-4deg);
    transform-origin: center;
    vertical-align: middle;
    white-space: nowrap;
    box-shadow: inset 0 0 0 1px var(--paper);
    opacity: .92;
  }
  @media print {
    .win-stamp { color: #000; border-color: #000; }
  }
"""


# Common <head>...<masthead>...</masthead> opener used by both pages.
# Renderers pass title + subtitle + own_style_block; the function returns
# everything up through the closing </header>, ready for them to append
# date sections and the colophon.
def render_masthead(
    title: str,
    subtitle_html: str,
    own_style_block: str,
    page_meta_description: str,
    now: Optional[datetime] = None,
) -> str:
    import html as _html
    now = now or datetime.now()
    today = now.date()
    edition = _edition_line(today)
    issue_date = issue_date_line(today)
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        f"<title>{_html.escape(title)}</title>\n"
        f"<meta name=\"description\" content=\"{_html.escape(page_meta_description)}\">\n"
        # Pre-render application of stored font size — runs before CSS
        # parses, so no flash of stylesheet-default size when the user
        # has previously picked a custom size.
        "<script>(function(){try{var v=parseInt(localStorage.getItem('iacls.rootFontSize'),10);"
        "if(v>=10&&v<=120)document.documentElement.style.fontSize=v+'px';}catch(e){}})();</script>\n"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">\n"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>\n"
        "<link href=\"https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;0,700;1,400;1,500&family=Cormorant+SC:wght@500;600;700&family=EB+Garamond:ital,wght@0,400;0,500;0,600;1,400;1,500&family=IBM+Plex+Mono:wght@400;500&display=swap\" rel=\"stylesheet\">\n"
        f"<style>{BASE_STYLE}{own_style_block}</style>\n"
        "</head>\n<body>\n"
        "<aside class=\"fs-control\" role=\"group\" aria-label=\"Text size\">\n"
        "  <label class=\"fs-control__label\" for=\"fs-slider\">Text</label>\n"
        "  <input type=\"range\" id=\"fs-slider\" class=\"fs-control__slider\""
        " min=\"14\" max=\"72\" step=\"1\" value=\"32\""
        " aria-label=\"Root font size in pixels\">\n"
        "  <span class=\"fs-control__value\" id=\"fs-display\">32px</span>\n"
        "</aside>\n"
        "<div class=\"grain\" aria-hidden=\"true\"></div>\n"
        "<main class=\"sheet\">\n"
        "  <header class=\"masthead\">\n"
        "    <div class=\"masthead__eyebrow\">\n"
        f"      <span class=\"masthead__vol\">{_html.escape(edition)}</span>\n"
        "      <span class=\"masthead__motto\">Published by the Institute for Advanced Criminal Law Studies</span>\n"
        f"      <span class=\"masthead__issue\">{_html.escape(issue_date)}</span>\n"
        "    </div>\n"
        "    <div class=\"masthead__rules\"><hr class=\"thick\"><hr class=\"thin\"></div>\n"
        f"    <h1 class=\"masthead__title\">{_html.escape(title)}</h1>\n"
        f"    <p class=\"masthead__subtitle\">{subtitle_html}</p>\n"
        "    <div class=\"masthead__rules\"><hr class=\"thin\"><hr class=\"thick\"></div>\n"
        "  </header>\n"
    )


def render_colophon(now: Optional[datetime] = None) -> str:
    import html as _html
    now = now or datetime.now()
    today = now.date()
    edition = _edition_line(today)
    generated = now.strftime("%A · %B %-d, %Y · %-I:%M %p")
    return (
        "  <footer class=\"colophon\">\n"
        "    <div class=\"colophon__rules\"><hr class=\"thin\"><hr class=\"thick\"></div>\n"
        "    <p class=\"colophon__line\">Composed and printed by "
        "<a href=\"https://github.com/markwbennett/PDRbot\" target=\"_blank\" rel=\"noopener\">PDRbot</a> "
        "from the public records of "
        "<a href=\"https://search.txcourts.gov/\" target=\"_blank\" rel=\"noopener\">search.txcourts.gov</a>. "
        "Each opinion link opens the court's own PDF in a new tab.</p>\n"
        f"    <p class=\"colophon__line colophon__line--small\">Set this {_html.escape(generated)} · {_html.escape(edition)}</p>\n"
        "  </footer>\n"
        "</main>\n"
        # Font-size slider wiring: read stored value into the slider on
        # load, persist user edits, and live-update <html> font-size as
        # the slider moves.
        "<script>(function(){\n"
        "  var KEY='iacls.rootFontSize';\n"
        "  var slider=document.getElementById('fs-slider');\n"
        "  var display=document.getElementById('fs-display');\n"
        "  if(!slider) return;\n"
        "  var stored=parseInt(localStorage.getItem(KEY),10);\n"
        "  var initial=(stored>=10&&stored<=120)?stored:32;\n"
        "  slider.value=initial;\n"
        "  if(display)display.textContent=initial+'px';\n"
        "  document.documentElement.style.fontSize=initial+'px';\n"
        # Live-update the numeric display while dragging so the user
        # sees the target value, but only apply the size (and persist)
        # when the slider is released, so the heavy reflow does not fire
        # on every pixel of drag.
        "  slider.addEventListener('input',function(e){\n"
        "    if(display)display.textContent=e.target.value+'px';\n"
        "  });\n"
        "  slider.addEventListener('change',function(e){\n"
        "    var v=parseInt(e.target.value,10);\n"
        "    if(isNaN(v))return;\n"
        "    document.documentElement.style.fontSize=v+'px';\n"
        "    if(display)display.textContent=v+'px';\n"
        "    try{localStorage.setItem(KEY,v);}catch(err){}\n"
        "  });\n"
        "})();</script>\n"
        "</body>\n</html>\n"
    )
