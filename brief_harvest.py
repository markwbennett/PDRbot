#!/usr/bin/env python3
"""Harvest winning defense counsel's contact info from their COA briefs.

For each defense win found by defense_wins.py, this module:

  1. Fetches the TAMES case page (Case.aspx) for the win.
  2. Reads the Parties panel to get the defense side's representative
     names (the State's parties are skipped).
  3. Reads the Appellate Briefs panel and downloads the brief PDFs the
     defense side filed (filed-by "Appellant" when the defendant
     appealed, "Appellee" when the State appealed).
  4. Extracts text with pdftotext and pulls (name, bar number, email)
     from the signature block. A contact is kept only when its
     surrounding text names one of the defense representatives from the
     Parties panel -- this keeps certificate-of-service emails for
     opposing counsel out of the table.
  5. Upserts the contacts into the lawyer_contacts table in pdrbot.db
     and refreshes data/lawyer_contacts.csv.

Entry point for pdrbot.py:

    brief_harvest.enrich_wins(wins, db_path)

which adds a "counsel" list ({name, bar_number, email}) to each win
dict in place, saves new contacts, and never raises -- any failure is
logged and the win is passed through unenriched.

CLI (for testing):
    brief_harvest.py --case 02-25-00166-CR --coa 2 [--state-appealed]
    brief_harvest.py --live [--lookback DAYS]        # dry-run, no DB writes
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://search.txcourts.gov/"
UA = {"User-Agent": "Mozilla/5.0 (PDRbot brief harvest; mb@ivi3.com)"}
BRIEFS_DIR = Path(__file__).resolve().parent / "data" / "briefs"
CSV_PATH = Path(__file__).resolve().parent / "data" / "lawyer_contacts.csv"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAR_NO_RE = re.compile(
    r"(?:state\s+bar(?:\s+of\s+texas)?|bar\s+card|sbot|sbn|tbn|tx\s+bar|texas\s+bar)"
    r"\s*(?:no\.?|number|card\s+no\.?|#)?\s*:?\s*(\d{7,8})",
    re.IGNORECASE,
)
# Lines above an email in which we look for the bar number and the name.
SIG_WINDOW = 14
COS_HEADING_RE = re.compile(r"certificate\s+of\s+service", re.IGNORECASE)
# An email this close below a Certificate of Service heading is a
# service address (usually opposing counsel's), never signature block.
COS_ZONE = 12
# Lines marking the State's counsel block on the identity-of-parties
# page or in a certificate of service.
STATE_MARKER_RE = re.compile(
    r"(?:attorney|counsel)s?\s+for\s+the\s+state|district\s+attorney"
    r"|county\s+attorney|attorney\s+pro\s+tem|state\s+prosecuting\s+attorney"
    r"|state\s+of\s+texas",
    re.IGNORECASE,
)
# Pages that are eFileTexas envelope/service sheets (appended to filed
# PDFs) list every service contact on both sides -- never harvest them.
EFILE_SHEET_RE = re.compile(
    r"Automated Certificate of eService|Envelope ID:|Filing Code Description",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


def _is_state_style(style: str) -> bool:
    s = style.lower().lstrip()
    return s.startswith("state") or s.startswith("the state")


def _panel_table(soup: BeautifulSoup, heading_text: str):
    """Return the rgMasterTable under the panel whose heading contains heading_text."""
    for h in soup.find_all("div", class_=["panel-heading", "panel-heading-content"]):
        t = h.get_text(" ", strip=True)
        if t and heading_text in t:
            content = h.find_next_sibling("div", class_="panel-content")
            if content:
                return content.find("table", class_="rgMasterTable")
    return None


def parse_defense_reps(soup: BeautifulSoup) -> list[str]:
    """Representative names for every non-State party on the case page."""
    table = _panel_table(soup, "Parties")
    if table is None:
        return []
    reps: list[str] = []
    tbody = table.find("tbody")
    for row in (tbody.find_all("tr") if tbody else []):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        party_name = cells[0].get_text(strip=True)
        party_type = cells[1].get_text(strip=True)
        if "State of Texas" in party_name or "State of Texas" in party_type:
            continue
        rep_cell = cells[2]
        for br in rep_cell.find_all("br"):
            br.replace_with("\n")
        for name in rep_cell.get_text().split("\n"):
            name = name.strip()
            if name and name not in reps:
                reps.append(name)
    return reps


def parse_defense_brief_links(soup: BeautifulSoup, defense_label: str) -> list[dict]:
    """Brief PDF links filed by the defense side.

    defense_label: "Appellant" or "Appellee". Rows in the Appellate
    Briefs panel carry a filed-by cell; nested media rows (which repeat
    the links without a date) are skipped by requiring a date cell.
    """
    table = _panel_table(soup, "Appellate Briefs")
    if table is None:
        return []
    briefs: list[dict] = []
    tbody = table.find("tbody")
    for row in (tbody.find_all("tr") if tbody else []):
        cells = row.find_all("td")
        texts = [c.get_text(" ", strip=True) for c in cells]
        if not texts or not re.match(r"\d{2}/\d{2}/\d{4}", texts[0]):
            continue  # nested media sub-row, not a filing row
        filed_by = texts[2] if len(texts) > 2 else ""
        if defense_label.lower() not in filed_by.lower():
            continue
        for a in row.find_all("a"):
            href = a.get("href", "")
            if "SearchMedia.aspx" in href and "DT=Brief" in href:
                briefs.append({
                    "date": texts[0],
                    "filed_by": filed_by,
                    "url": urljoin(BASE_URL, href),
                })
                break  # one Brief link per filing row
    return briefs


def download_brief(url: str, case_number: str, date: str,
                   session: requests.Session | None = None) -> Path | None:
    """Download a brief PDF into data/briefs/<case>/; return the path."""
    sess = session or requests
    case_dir = BRIEFS_DIR / case_number
    case_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{case_number}_{date.replace('/', '-')}_brief.pdf"
    path = case_dir / fname
    if path.exists() and path.stat().st_size > 0:
        return path
    try:
        resp = sess.get(url, headers=UA, timeout=120)
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            logger.warning("Brief harvest: %s did not return a PDF", url)
            return None
        path.write_bytes(resp.content)
        return path
    except requests.RequestException as e:
        logger.warning("Brief harvest: download failed for %s (%s)", url, e)
        return None


def pdf_to_text(pdf_path: Path) -> str:
    try:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            txt_path = Path(tf.name)
        subprocess.run(["pdftotext", "-layout", str(pdf_path), str(txt_path)],
                       check=True, capture_output=True, timeout=120)
        text = txt_path.read_text(errors="replace")
        txt_path.unlink(missing_ok=True)
        return text
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("Brief harvest: pdftotext failed on %s (%s)", pdf_path, e)
        return ""


def harvest_contacts(text: str, defense_reps: list[str]) -> list[dict]:
    """Pull (name, bar_number, email) for defense reps from brief text.

    Signature blocks and identity-of-parties entries put the lawyer's
    name (then bar number, address) ABOVE the email, so each email is
    attributed by scanning upward. The scan keeps a contact only when a
    defense representative's name is found closer above the email than
    any State-side marker line ("District Attorney," "Attorney for the
    State," etc.) -- on the identity-of-parties page both sides' blocks
    are stacked, and nearest-marker-wins separates them. The bar number
    is taken only from the lines between the name and the email, so a
    neighboring block's number is never borrowed. Emails just below a
    Certificate of Service heading are skipped outright.
    """
    # Drop eFile envelope/service-sheet pages before line-level parsing.
    kept_pages = [p for p in text.split("\f") if not EFILE_SHEET_RE.search(p)]
    lines = "\n".join(kept_pages).splitlines()
    cos_lines = [i for i, ln in enumerate(lines) if COS_HEADING_RE.search(ln)]
    contacts: dict[str, dict] = {}  # keyed by email lower

    def _norm(s: str) -> str:
        # Curly quotes in PDFs vs straight quotes in TAMES party names.
        return (s.replace("’", "'").replace("‘", "'")
                 .replace("“", '"').replace("”", '"').lower())

    # Precompute matchers per rep: full name, and surname if len > 3.
    rep_matchers = []
    for rep in defense_reps:
        tokens = [t for t in re.split(r"[\s,]+", rep) if len(t) > 1 and "." not in t]
        surname = tokens[-1] if tokens else ""
        rep_matchers.append((rep, _norm(rep), _norm(surname)))

    def _line_matches_rep(line_lower: str):
        for rep, rep_lower, surname in rep_matchers:
            if rep_lower in line_lower or (
                    len(surname) > 3 and re.search(
                        r"\b" + re.escape(surname) + r"\b", line_lower)):
                return rep
        return None

    for i, line in enumerate(lines):
        for email_m in EMAIL_RE.finditer(line):
            email = email_m.group(0).strip(".")
            if any(0 <= i - c <= COS_ZONE for c in cos_lines):
                continue  # inside a Certificate of Service section

            matched_rep = None
            rep_dist = None
            state_dist = None
            for d in range(0, SIG_WINDOW + 1):
                j = i - d
                if j < 0:
                    break
                ln_lower = _norm(lines[j])
                # An email more than two lines up means we crossed into
                # a different counsel block; stop the scan there.
                if d > 2 and EMAIL_RE.search(lines[j]):
                    break
                if rep_dist is None:
                    rep = _line_matches_rep(ln_lower)
                    if rep:
                        matched_rep, rep_dist = rep, d
                if state_dist is None and d > 0 and STATE_MARKER_RE.search(ln_lower):
                    state_dist = d
                if rep_dist is not None and state_dist is not None:
                    break
            if matched_rep is None:
                continue
            if state_dist is not None and state_dist <= rep_dist:
                continue  # the State's block is closer: opposing counsel

            # Two extra lines above the name catch a bar number that
            # sits above the line the name matched on (e.g. the firm
            # name "O'NEAL LAW" matching below "State Bar No. ...").
            block = "\n".join(lines[max(0, i - rep_dist - 2):i + 1])
            bar_m = BAR_NO_RE.search(block)
            bar_number = bar_m.group(1) if bar_m else None
            key = email.lower()
            if key in contacts:
                if bar_number and not contacts[key]["bar_number"]:
                    contacts[key]["bar_number"] = bar_number
            else:
                contacts[key] = {
                    "name": matched_rep,
                    "bar_number": bar_number,
                    "email": email,
                }
    return list(contacts.values())


def ensure_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lawyer_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                bar_number TEXT,
                email TEXT NOT NULL,
                source TEXT,
                first_seen TEXT DEFAULT (date('now','localtime')),
                last_seen TEXT DEFAULT (date('now','localtime')),
                UNIQUE(name COLLATE NOCASE, email COLLATE NOCASE)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def upsert_contacts(db_path: str, contacts: list[dict], source: str) -> int:
    """Insert or refresh contacts; returns how many were new."""
    if not contacts:
        return 0
    ensure_table(db_path)
    conn = sqlite3.connect(db_path)
    new = 0
    try:
        cur = conn.cursor()
        for c in contacts:
            cur.execute(
                "SELECT id, bar_number FROM lawyer_contacts "
                "WHERE name = ? COLLATE NOCASE AND email = ? COLLATE NOCASE",
                (c["name"], c["email"]))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE lawyer_contacts SET last_seen = date('now','localtime'), "
                    "bar_number = COALESCE(bar_number, ?) WHERE id = ?",
                    (c["bar_number"], row[0]))
            else:
                cur.execute(
                    "INSERT INTO lawyer_contacts (name, bar_number, email, source) "
                    "VALUES (?, ?, ?, ?)",
                    (c["name"], c["bar_number"], c["email"], source))
                new += 1
        conn.commit()
    finally:
        conn.close()
    return new


def export_csv(db_path: str, csv_path: Path = CSV_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name, bar_number, email, source, first_seen, last_seen "
            "FROM lawyer_contacts ORDER BY name COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "bar_number", "email", "source", "first_seen", "last_seen"])
        w.writerows(rows)


def enrich_win(win: dict, session: requests.Session) -> list[dict]:
    """Fetch case page, download defense briefs, harvest contacts.

    Adds win["counsel"] (possibly empty) in place; returns the contacts.
    """
    win.setdefault("counsel", [])
    case_url = win.get("case_url", "")
    if not case_url:
        return []
    resp = session.get(case_url, headers=UA, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    defense_reps = parse_defense_reps(soup)
    defense_label = "Appellee" if _is_state_style(win.get("style", "")) else "Appellant"
    briefs = parse_defense_brief_links(soup, defense_label)
    if not briefs:
        logger.info("Brief harvest: no %s brief on %s", defense_label, win["case_number"])
        # Still surface the names even without a brief to mine.
        win["counsel"] = [{"name": r, "bar_number": None, "email": None}
                          for r in defense_reps]
        return []

    contacts: dict[str, dict] = {}
    for brief in briefs:
        pdf_path = download_brief(brief["url"], win["case_number"], brief["date"], session)
        if pdf_path is None:
            continue
        text = pdf_to_text(pdf_path)
        if not text:
            continue
        for c in harvest_contacts(text, defense_reps):
            key = c["email"].lower()
            if key not in contacts or (c["bar_number"] and not contacts[key]["bar_number"]):
                contacts[key] = c
        time.sleep(0.5)

    found = list(contacts.values())
    # Counsel list for the report: harvested contacts first, then any
    # remaining Parties-panel reps we found no email for.
    harvested_names = {c["name"].lower() for c in found}
    win["counsel"] = found + [
        {"name": r, "bar_number": None, "email": None}
        for r in defense_reps if r.lower() not in harvested_names
    ]
    return found


def enrich_wins(wins: list[dict], db_path: str) -> None:
    """Enrich every win in place and persist contacts. Never raises."""
    if not wins:
        return
    session = requests.Session()
    total_new = 0
    for win in wins:
        try:
            found = enrich_win(win, session)
            if found:
                source = f"brief:{win['case_number']} {win.get('court', '')}".strip()
                total_new += upsert_contacts(db_path, found, source)
        except Exception as e:  # never let harvesting break the daily email
            logger.warning("Brief harvest: failed on %s (%s)",
                           win.get("case_number", "?"), e)
        time.sleep(0.5)
    if total_new:
        try:
            export_csv(db_path)
        except Exception as e:
            logger.warning("Brief harvest: CSV export failed (%s)", e)
    logger.info("Brief harvest: %d new contact(s) saved", total_new)


def _print_win(win: dict) -> None:
    print(f"{win['date']}  {win['court']}  {win['case_number']}  {win['disposition']}")
    print(f"  {win['style'][:70]}")
    for c in win.get("counsel", []):
        bar = c["bar_number"] or "bar# ?"
        email = c["email"] or "email ?"
        print(f"    {c['name']}  |  {bar}  |  {email}")
    print()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="case number, e.g. 02-25-00166-CR")
    parser.add_argument("--coa", type=int, help="COA number 1-14")
    parser.add_argument("--state-appealed", action="store_true",
                        help="the State was the appellant (defense side = Appellee)")
    parser.add_argument("--live", action="store_true",
                        help="run over current defense wins (dry run, no DB writes)")
    parser.add_argument("--lookback", type=int, default=5)
    parser.add_argument("--save", metavar="DB_PATH",
                        help="also upsert contacts into this SQLite DB")
    args = parser.parse_args()

    session = requests.Session()
    if args.live:
        import defense_wins
        wins, _state = defense_wins.collect_defense_wins(args.lookback)
        for win in wins:
            try:
                found = enrich_win(win, session)
            except Exception as e:
                print(f"FAILED {win['case_number']}: {e}")
                continue
            _print_win(win)
            if args.save and found:
                source = f"brief:{win['case_number']} {win.get('court', '')}".strip()
                n = upsert_contacts(args.save, found, source)
                print(f"    -> {n} new contact(s) saved")
        if args.save:
            export_csv(args.save)
        return 0

    if not args.case or not args.coa:
        parser.error("--case and --coa are required unless --live")
    win = {
        "date": "?", "court": f"COA{args.coa:02d}",
        "case_number": args.case,
        "style": "State v. X" if args.state_appealed else "X v. State",
        "disposition": "(manual test)",
        "case_url": f"{BASE_URL}Case.aspx?cn={args.case}&coa=coa{args.coa:02d}",
    }
    found = enrich_win(win, session)
    _print_win(win)
    if args.save and found:
        n = upsert_contacts(args.save, found, f"brief:{args.case} COA{args.coa:02d}")
        export_csv(args.save)
        print(f"-> {n} new contact(s) saved to {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
