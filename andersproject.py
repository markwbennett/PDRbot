#!/usr/bin/env python3
"""
Anders Project — daily audit of intermediate COA opinions.

Reads opinions already downloaded by pdrbot, identifies those resolving
an Anders/Gainous brief in a trial (non-plea) case, and checks whether:
  1. The COA opinion identifies the elements of the charged offense AND
     describes the evidence supporting each element.
  2. If not, whether the Anders brief itself did so.

Cases where neither the opinion nor the brief satisfied that standard
are emailed to mb@ivi3.com from andersproject@iacls.org.

Usage:
    python andersproject.py              # analyze yesterday's opinions
    python andersproject.py --date 2026-04-22
    python andersproject.py --date 2026-04-22 --dry-run
    python andersproject.py --reanalyze  # force re-analysis of all
    python andersproject.py --report-only  # email pending items without re-analyzing
"""

import argparse
import json
import logging
import os
import re
import smtplib
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from email import encoders
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# pdrbot lives one directory up from this script's perspective — we're in PDRbot/
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.path.expanduser('~/github/mwb_common'))

from mwb_claude import call_claude

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
LOG = logging.getLogger('andersproject')

DB_PATH = ROOT / 'data' / 'pdrbot.db'

FROM_ADDR = 'andersproject@iacls.org'
TO_ADDR = 'mb@ivi3.com'

# ── Claude prompts ────────────────────────────────────────────────────────────

OPINION_PROMPT = """\
You are a Texas appellate practice expert reviewing a Court of Appeals opinion.

Answer ONLY with a JSON object — no other text.

{
  "is_anders": true/false,
  "is_trial": true/false/null,
  "opinion_lists_elements": true/false/null,
  "offense_name": "string or null",
  "notes": "one sentence"
}

Definitions:
- "is_anders": true if the opinion states that appointed counsel filed an Anders
  brief (sometimes called an Anders/Gainous brief) representing there are no
  non-frivolous appellate issues.
- "is_trial": true if the underlying conviction followed a jury or bench trial;
  false if it followed a guilty or no-contest plea; null if is_anders is false
  or cannot be determined.
- "opinion_lists_elements": true if the opinion (a) names the specific statutory
  elements of the charged offense AND (b) describes the trial evidence that
  supports each element. false if it does not do both. null if is_anders is false
  or is_trial is false.
- "offense_name": the name of the charged offense if identifiable, else null.

OPINION TEXT:
"""

BRIEF_PROMPT = """\
You are a Texas appellate practice expert reviewing an Anders brief.

Answer ONLY with a JSON object — no other text.

{
  "brief_lists_elements": true/false,
  "notes": "one sentence"
}

Definitions:
- "brief_lists_elements": true if the brief (a) identifies the specific statutory
  elements of the charged offense AND (b) describes the trial evidence that
  supports each element in the sufficiency-of-evidence review. false if it does
  not do both.

BRIEF TEXT:
"""


# ── Database ──────────────────────────────────────────────────────────────────

SCHEMA = """\
CREATE TABLE IF NOT EXISTS anders_analyses (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    opinion_id             INTEGER NOT NULL UNIQUE,
    case_number            TEXT NOT NULL,
    court                  TEXT,
    opinion_date           DATE,
    is_anders              INTEGER,
    is_trial               INTEGER,
    opinion_lists_elements INTEGER,
    brief_lists_elements   INTEGER,
    brief_url              TEXT,
    brief_pdf_path         TEXT,
    offense_name           TEXT,
    notes                  TEXT,
    analyzed_at            TIMESTAMP DEFAULT (datetime('now','localtime')),
    model                  TEXT,
    FOREIGN KEY (opinion_id) REFERENCES opinions(id)
);

CREATE TABLE IF NOT EXISTS anders_report_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number     TEXT NOT NULL,
    court           TEXT,
    opinion_date    DATE,
    case_url        TEXT,
    brief_url       TEXT,
    offense_name    TEXT,
    failure_reason  TEXT,
    reported_at     TIMESTAMP DEFAULT (datetime('now','localtime')),
    emailed_at      TIMESTAMP
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_text(pdf_path: Path, max_chars: int = 50_000) -> str:
    import subprocess
    try:
        r = subprocess.run(
            ['pdftotext', '-layout', str(pdf_path), '-'],
            capture_output=True, text=True, timeout=60,
        )
        text = r.stdout.strip()
        if len(text) < 100:
            r = subprocess.run(
                ['pdftotext', str(pdf_path), '-'],
                capture_output=True, text=True, timeout=60,
            )
            text = r.stdout.strip()
        return text[:max_chars]
    except Exception as e:
        LOG.warning('pdftotext failed for %s: %s', pdf_path, e)
        return ''


# ── Anders brief fetching from search.txcourts.gov ───────────────────────────

def _court_coa_code(court_str: str) -> str:
    """Convert 'COA14' → 'cos14', 'COA01' → 'cos01', etc."""
    m = re.search(r'(\d+)', court_str)
    if m:
        return f"cos{int(m.group(1)):02d}"
    return 'cos01'


def fetch_anders_brief(case_number: str, court: str) -> tuple[str | None, Path | None]:
    """
    Fetch the Anders brief PDF for a case from search.txcourts.gov.
    Returns (brief_url, local_pdf_path) or (None, None).
    """
    import requests
    from bs4 import BeautifulSoup

    coa = _court_coa_code(court)
    base = 'https://search.txcourts.gov'
    case_url = f'{base}/Case.aspx?cn={case_number}&coa={coa}'

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (X11; Linux x86_64; rv:109.0) '
            'Gecko/20100101 Firefox/115.0'
        )
    }
    try:
        r = requests.get(case_url, headers=headers, timeout=30)
        if r.status_code != 200:
            return None, None
    except Exception as e:
        LOG.warning('fetch_anders_brief: %s', e)
        return None, None

    soup = BeautifulSoup(r.text, 'lxml')
    brief_grid = soup.find('div', {'id': 'ctl00_ContentPlaceHolder1_grdBriefs'})
    if not brief_grid:
        return None, None

    link_re = re.compile(r'\[(https?://[^\]]+)\]')

    for row in brief_grid.find_all('tr', class_=lambda c: c and ('rgRow' in c or 'rgAltRow' in c)):
        row_text = row.get_text(' ', strip=True)
        if not re.search(r'anders', row_text, re.IGNORECASE):
            continue
        # Found an Anders row — grab first PDF link
        for a in row.find_all('a'):
            href = a.get('href', '')
            if not href.startswith('http'):
                href = base + '/' + href.lstrip('/')
            if 'SearchMedia' in href or href.lower().endswith('.pdf'):
                # Download it
                brief_url = href
                try:
                    pr = requests.get(brief_url, headers=headers, timeout=60)
                    if pr.status_code == 200 and pr.content[:5] == b'%PDF-':
                        out_dir = ROOT / 'data' / 'anders_briefs'
                        out_dir.mkdir(parents=True, exist_ok=True)
                        safe = re.sub(r'[^A-Za-z0-9._-]', '_', case_number)
                        pdf_path = out_dir / f'{safe}_anders_brief.pdf'
                        pdf_path.write_bytes(pr.content)
                        LOG.info('  Downloaded Anders brief: %s', pdf_path.name)
                        return brief_url, pdf_path
                except Exception as e:
                    LOG.warning('  Brief download failed: %s', e)
                return brief_url, None

    return None, None


# ── Claude calls ──────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    LOG.warning('No JSON in Claude response: %r', text[:200])
    return {}


def analyze_opinion(pdf_path: Path) -> dict:
    text = extract_text(pdf_path)
    if not text:
        return {}
    try:
        raw = call_claude(OPINION_PROMPT + text, timeout=120)
        return _parse_json(raw)
    except Exception as e:
        LOG.warning('Claude opinion analysis failed: %s', e)
        return {}


def analyze_brief(pdf_path: Path) -> dict:
    text = extract_text(pdf_path)
    if not text:
        return {}
    try:
        raw = call_claude(BRIEF_PROMPT + text, timeout=120)
        return _parse_json(raw)
    except Exception as e:
        LOG.warning('Claude brief analysis failed: %s', e)
        return {}


# ── Core analysis loop ────────────────────────────────────────────────────────

def process_opinion(conn: sqlite3.Connection, row: tuple, reanalyze: bool = False) -> None:
    op_id, case_number, court, op_date, file_path, case_url = row

    # Skip if already analyzed (unless --reanalyze)
    existing = conn.execute(
        'SELECT id FROM anders_analyses WHERE opinion_id=?', (op_id,)
    ).fetchone()
    if existing and not reanalyze:
        return

    pdf_path = ROOT / file_path
    if not pdf_path.exists():
        LOG.warning('PDF not found: %s', pdf_path)
        return

    LOG.info('Analyzing %s (%s)', case_number, court)
    result = analyze_opinion(pdf_path)
    if not result:
        LOG.warning('  No result for %s', case_number)
        return

    is_anders = result.get('is_anders')
    is_trial = result.get('is_trial')
    opinion_lists = result.get('opinion_lists_elements')
    offense_name = result.get('offense_name')
    notes = result.get('notes', '')

    LOG.info('  is_anders=%s is_trial=%s opinion_lists_elements=%s',
             is_anders, is_trial, opinion_lists)

    brief_lists = None
    brief_url = None
    brief_pdf_path = None

    # If Anders + trial + opinion did NOT list elements, check the brief
    if is_anders and is_trial and opinion_lists is False:
        LOG.info('  Fetching Anders brief from search.txcourts.gov...')
        brief_url, brief_pdf = fetch_anders_brief(case_number, court)
        if brief_pdf:
            brief_pdf_path = str(brief_pdf)
            br = analyze_brief(brief_pdf)
            brief_lists = br.get('brief_lists_elements')
            LOG.info('  brief_lists_elements=%s', brief_lists)
        else:
            LOG.info('  Anders brief not found or not downloadable')
        time.sleep(1.0)

    # Upsert analysis record
    if existing and reanalyze:
        conn.execute(
            '''UPDATE anders_analyses SET
               is_anders=?, is_trial=?, opinion_lists_elements=?,
               brief_lists_elements=?, brief_url=?, brief_pdf_path=?,
               offense_name=?, notes=?, analyzed_at=datetime('now','localtime'),
               model='claude-opus-4-7'
               WHERE opinion_id=?''',
            (
                int(is_anders) if is_anders is not None else None,
                int(is_trial) if is_trial is not None else None,
                int(opinion_lists) if opinion_lists is not None else None,
                int(brief_lists) if brief_lists is not None else None,
                brief_url, brief_pdf_path, offense_name, notes, op_id,
            ),
        )
    else:
        conn.execute(
            '''INSERT OR REPLACE INTO anders_analyses
               (opinion_id, case_number, court, opinion_date,
                is_anders, is_trial, opinion_lists_elements,
                brief_lists_elements, brief_url, brief_pdf_path,
                offense_name, notes, model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                op_id, case_number, court, op_date,
                int(is_anders) if is_anders is not None else None,
                int(is_trial) if is_trial is not None else None,
                int(opinion_lists) if opinion_lists is not None else None,
                int(brief_lists) if brief_lists is not None else None,
                brief_url, brief_pdf_path, offense_name, notes,
                'claude-opus-4-7',
            ),
        )
    conn.commit()

    # Flag for report if Anders + trial + neither opinion nor brief listed elements
    if is_anders and is_trial:
        failure_reason = None
        if opinion_lists is False and brief_lists is False:
            failure_reason = 'Neither opinion nor Anders brief lists elements and supporting evidence'
        elif opinion_lists is False and brief_lists is None:
            failure_reason = 'Opinion does not list elements and supporting evidence; brief not available for check'

        if failure_reason:
            conn.execute(
                '''INSERT OR IGNORE INTO anders_report_items
                   (case_number, court, opinion_date, case_url,
                    brief_url, offense_name, failure_reason)
                   VALUES (?,?,?,?,?,?,?)''',
                (case_number, court, op_date, case_url,
                 brief_url, offense_name, failure_reason),
            )
            conn.commit()
            LOG.info('  FLAGGED: %s', failure_reason)


# ── Email report ──────────────────────────────────────────────────────────────

def _html_report(items: list[dict], target_date: str) -> str:
    rows = ''
    for it in items:
        case_link = (f'<a href="{it["case_url"]}">{it["case_number"]}</a>'
                     if it.get('case_url') else it['case_number'])
        brief_link = (f'<a href="{it["brief_url"]}">Brief PDF</a>'
                      if it.get('brief_url') else '—')
        rows += f"""
        <tr>
          <td>{case_link}</td>
          <td>{it.get('court','')}</td>
          <td>{it.get('offense_name') or '—'}</td>
          <td>{it.get('failure_reason','')}</td>
          <td>{brief_link}</td>
        </tr>"""

    return f"""<!DOCTYPE html><html><head><style>
body{{font-family:sans-serif;font-size:14px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}
th{{background:#f0f0f0}}
.banner{{background:#fff3cd;border:1px solid #ffc107;padding:10px;
         margin-bottom:16px;border-radius:4px}}
</style></head><body>
<h2>Anders Brief Deficiency Report — {target_date}</h2>
<div class="banner"><strong>{len(items)} case(s)</strong> where appointed counsel
filed an Anders brief in a trial case but neither the COA opinion nor the brief
identified the elements of the charged offense and described supporting evidence.
</div>
<table>
<thead><tr>
  <th>Case</th><th>Court</th><th>Offense</th><th>Deficiency</th><th>Brief</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<p style="color:#666;font-size:12px">Generated by andersproject · {date.today().isoformat()}</p>
</body></html>"""


def _text_report(items: list[dict], target_date: str) -> str:
    lines = [
        f'Anders Brief Deficiency Report — {target_date}',
        f'{len(items)} case(s)',
        '',
    ]
    for it in items:
        lines += [
            f"Case:    {it['case_number']}",
            f"Court:   {it.get('court','')}",
            f"Offense: {it.get('offense_name') or '—'}",
            f"Issue:   {it.get('failure_reason','')}",
        ]
        if it.get('case_url'):
            lines.append(f"COA:     {it['case_url']}")
        if it.get('brief_url'):
            lines.append(f"Brief:   {it['brief_url']}")
        lines.append('')
    return '\n'.join(lines)


def send_report(items: list[dict], target_date: str,
                attachments: list[Path], dry_run: bool = False) -> None:
    smtp_host = os.environ.get('EMAIL_SMTP_HOST', 'smtp.gmail.com')
    smtp_port = int(os.environ.get('EMAIL_SMTP_PORT', '587'))
    smtp_user = os.environ.get('EMAIL_AUTH_USER', os.environ.get('EMAIL_FROM', ''))
    smtp_pass = os.environ.get('EMAIL_PASSWORD', '')

    subject = (f'Anders Brief Deficiency Report — {len(items)} case(s)'
               f' — {target_date}')

    msg = MIMEMultipart('mixed')
    msg['From'] = FROM_ADDR
    msg['To'] = TO_ADDR
    msg['Subject'] = subject

    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(_text_report(items, target_date), 'plain'))
    alt.attach(MIMEText(_html_report(items, target_date), 'html'))
    msg.attach(alt)

    for p in attachments:
        if p.exists():
            with open(p, 'rb') as f:
                part = MIMEApplication(f.read(), Name=p.name)
            part['Content-Disposition'] = f'attachment; filename="{p.name}"'
            msg.attach(part)

    if dry_run:
        print('=== DRY RUN — not sending ===')
        print(f'From: {FROM_ADDR}')
        print(f'To:   {TO_ADDR}')
        print(f'Subj: {subject}')
        print(f'Attachments: {[p.name for p in attachments if p.exists()]}')
        print()
        print(_text_report(items, target_date))
        return

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.ehlo()
            server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.sendmail(FROM_ADDR, [TO_ADDR], msg.as_bytes())
        server.quit()
        LOG.info('Report emailed to %s', TO_ADDR)
    except Exception as e:
        LOG.error('Email failed: %s', e)
        raise


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description='Anders brief audit for COA opinions')
    ap.add_argument('--date', default=None,
                    help='opinion date to analyze, YYYY-MM-DD (default: yesterday)')
    ap.add_argument('--dry-run', action='store_true',
                    help='print report without emailing')
    ap.add_argument('--reanalyze', action='store_true',
                    help='re-analyze opinions already in anders_analyses')
    ap.add_argument('--report-only', action='store_true',
                    help='skip analysis; just email pending report items')
    ap.add_argument('--db', default=str(DB_PATH))
    args = ap.parse_args()

    if args.date:
        target_date = args.date
    else:
        yesterday = date.today() - timedelta(days=1)
        target_date = yesterday.isoformat()

    conn = sqlite3.connect(args.db)
    conn.execute('PRAGMA foreign_keys = ON')
    init_schema(conn)

    if not args.report_only:
        # Fetch opinions for target date not yet analyzed
        skip_clause = ('' if args.reanalyze
                       else 'AND o.id NOT IN (SELECT opinion_id FROM anders_analyses)')
        rows = conn.execute(
            f'''SELECT o.id, o.case_number, o.court, o.opinion_date,
                       o.file_path, o.case_url
                FROM opinions o
                WHERE o.opinion_date = ?
                  {skip_clause}
                ORDER BY o.id''',
            (target_date,),
        ).fetchall()

        LOG.info('Opinions to analyze for %s: %d', target_date, len(rows))
        for row in rows:
            try:
                process_opinion(conn, row, reanalyze=args.reanalyze)
            except Exception as e:
                LOG.error('Error processing %s: %s', row[1], e)
            time.sleep(0.5)

    # Collect unsent report items
    items_rows = conn.execute(
        '''SELECT case_number, court, opinion_date, case_url,
                  brief_url, offense_name, failure_reason
           FROM anders_report_items
           WHERE emailed_at IS NULL
           ORDER BY opinion_date DESC, case_number'''
    ).fetchall()

    cols = ['case_number', 'court', 'opinion_date', 'case_url',
            'brief_url', 'offense_name', 'failure_reason']
    items = [dict(zip(cols, r)) for r in items_rows]

    if not items:
        LOG.info('No deficient Anders cases to report for %s.', target_date)
        conn.close()
        return

    LOG.info('%d item(s) to report', len(items))

    # Collect brief PDFs for attachment
    attachments: list[Path] = []
    for it in items:
        row = conn.execute(
            '''SELECT aa.brief_pdf_path FROM anders_analyses aa
               JOIN opinions o ON o.id = aa.opinion_id
               WHERE o.case_number = ? AND aa.brief_pdf_path IS NOT NULL
               LIMIT 1''',
            (it['case_number'],),
        ).fetchone()
        if row and row[0]:
            p = Path(row[0])
            if p.exists():
                attachments.append(p)

    send_report(items, target_date, attachments, dry_run=args.dry_run)

    if not args.dry_run:
        conn.execute(
            "UPDATE anders_report_items SET emailed_at=datetime('now','localtime') "
            "WHERE emailed_at IS NULL"
        )
        conn.commit()

    conn.close()


if __name__ == '__main__':
    main()
