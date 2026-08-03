#!/usr/bin/env python3
"""
Anders Project — daily audit of intermediate COA opinions.

Reads opinions already downloaded by pdrbot, identifies those resolving
an Anders/Gainous brief in a trial (non-plea) case, and checks whether:
  1. The COA opinion identifies the elements of the charged offense AND
     describes the evidence supporting each element.
  2. If not, whether the Anders brief discusses the statutory elements OR
     walks through the trial testimony. Flag only when the brief does
     neither. This is a screening rule for the daily report — not a judgment
     that a testimony walkthrough (or element discussion) is legally
     sufficient under Anders.

A daily heartbeat email is sent to the recipients in TO_ADDRS from the
authenticated EMAIL_FROM identity (PDRbot@iacls.org) summarizing the day's
Anders findings: total opinions, Anders count, plea vs. trial split, and for
trial cases whether the opinion listed elements and evidence, whether the
brief discussed elements or walked through testimony, or neither. Flagged
cases (and their brief PDFs as attachments) are included in the same email.

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

from mwb_claude import call_claude, call_claude_ex

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
LOG = logging.getLogger('andersproject')

DB_PATH = ROOT / 'data' / 'pdrbot.db'

# Send from the identity the SMTP account actually authenticates as. Fastmail
# rejects (551 5.7.1) outbound mail whose From header is not an authorised
# identity on the account, and the account authenticates as EMAIL_FROM.
FROM_ADDR = os.environ.get('EMAIL_FROM', 'PDRbot@iacls.org')
TO_ADDRS = ['mb@ivi3.com', 'mcjernig@cougarnet.uh.edu']

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
  or the opinion does not contain enough information to determine this.
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
  "is_trial": true/false/null,
  "brief_discusses_elements": true/false/null,
  "brief_walks_through_testimony": true/false/null,
  "notes": "one sentence"
}

Definitions:
- "is_trial": true if the underlying conviction followed a jury or bench trial;
  false if it followed a guilty or no-contest plea; null only if the brief
  contains no information about the mode of conviction.
- "brief_discusses_elements": true if the brief discusses the statutory elements
  of the charged offense (names or describes what the State had to prove).
  false if it does not. null if is_trial is false (plea cases are out of scope).
- "brief_walks_through_testimony": true if the brief walks through the trial
  testimony — a narrative of what the witnesses said about the charged
  conduct, not a bare string of record citations. false if it does not.
  null if is_trial is false.

Report only these facts. Do not judge whether the brief is legally sufficient.

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
    brief_discusses_elements INTEGER,
    brief_walks_through_testimony INTEGER,
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
    # Additive migrations for existing DBs created before these columns.
    cols = {r[1] for r in conn.execute('PRAGMA table_info(anders_analyses)')}
    if 'brief_discusses_elements' not in cols:
        conn.execute(
            'ALTER TABLE anders_analyses '
            'ADD COLUMN brief_discusses_elements INTEGER'
        )
    if 'brief_walks_through_testimony' not in cols:
        conn.execute(
            'ALTER TABLE anders_analyses '
            'ADD COLUMN brief_walks_through_testimony INTEGER'
        )
    conn.commit()


def _sql_bool(v) -> int | None:
    return int(v) if v is not None else None


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


def _media_dt(href: str) -> str:
    """Extract and normalize the DT= document-type query param from a media URL."""
    from urllib.parse import unquote
    m = re.search(r'[?&]DT=([^&]*)', href, re.I)
    if not m:
        return ''
    return unquote(m.group(1)).strip().upper()


# Clerk notice-of-filing letters, not the brief itself. TAMES labels these
# DT=ANDERS BRIEF FLD on the same "Anders brief filed" event row. Some courts
# (notably COA03 sex cases, and often COA04/02/06/09/10) post only this notice
# and never link the brief itself.
_NOTICE_DT_MARKERS = (
    'ANDERS BRIEF FLD',
    'NOTICE',
    'LETTER',
    'ACKNOWLEDG',
    'FILE COPY',
)

# Phrases that mark a clerk's notice-of-filing letter rather than an Anders brief.
# Used as a content backstop when DT= is missing or wrong.
_NOTICE_TEXT_MARKERS = (
    'filed an anders brief on behalf of',
    'appellant’s counsel filed an anders brief',
    "appellant's counsel filed an anders brief",
    'no oral argument will be scheduled',
    'motion to withdraw as counsel will remain on the motion docket',
    'if appellant requests a copy of the record, the trial court clerk',
)


def _looks_like_clerk_notice(pdf_path: Path) -> bool:
    """True if the PDF is a clerk notice-of-filing letter, not a brief."""
    text = extract_text(pdf_path, max_chars=4_000).lower()
    if not text:
        return False
    hits = sum(1 for m in _NOTICE_TEXT_MARKERS if m in text)
    if hits >= 2:
        return True
    # Short letterhead "FILE COPY" letters from the COA clerk.
    if 'file copy' in text and 'court of appeals' in text and hits >= 1:
        return True
    return False


def _anders_brief_media_urls(row, base: str) -> list[str]:
    """Return absolute URLs for real brief media on an Anders-filed event row.

    Prefers DT=Brief (same convention as brief_harvest.parse_brief_links).
    Skips notice-of-filing media (DT=ANDERS BRIEF FLD and related). If the row
    has only notice media — common when the brief is sealed or not posted —
    returns an empty list so the caller treats the brief as unavailable.
    """
    brief_urls: list[str] = []
    other_urls: list[str] = []
    notice_only = False

    for a in row.find_all('a'):
        href = a.get('href', '') or ''
        if not href:
            continue
        if not href.startswith('http'):
            href = base + '/' + href.lstrip('/')
        if 'SearchMedia' not in href and not href.lower().endswith('.pdf'):
            continue
        dt = _media_dt(href)
        if dt == 'BRIEF':
            brief_urls.append(href)
            continue
        if any(marker in dt for marker in _NOTICE_DT_MARKERS):
            notice_only = True
            LOG.info('  Skipping notice media DT=%r on Anders row', dt)
            continue
        # No DT= or unknown type — keep as last-resort fallback only when no
        # DT=Brief link exists; content-checked after download.
        other_urls.append(href)

    if brief_urls:
        return brief_urls
    if notice_only and not other_urls:
        # Row present, but only the clerk's notice is linked — brief unavailable.
        return []
    return other_urls


def fetch_anders_brief(case_number: str, court: str) -> tuple[str | None, Path | None]:
    """
    Fetch the Anders brief PDF for a case from search.txcourts.gov.
    Returns (brief_url, local_pdf_path) or (None, None).

    Does not treat the clerk's notice-of-filing letter as a brief. When the
    "Anders brief filed" row links only DT=ANDERS BRIEF FLD (or similar notice
    media) — as on many COA03 sex cases — returns (None, None) so the case is
    classified brief-unavailable rather than analyzed as a deficient brief.
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

    for row in brief_grid.find_all('tr', class_=lambda c: c and ('rgRow' in c or 'rgAltRow' in c)):
        # Match the Event Type column exactly. Substring matching on the whole
        # row picks up the State's "Brief Waiver-Anders Response" rows, which
        # share the word "Anders" but link to a different PDF.
        tds = row.find_all('td', recursive=False)
        if len(tds) < 2:
            continue
        if tds[1].get_text(strip=True).lower() != 'anders brief filed':
            continue

        candidates = _anders_brief_media_urls(row, base)
        if not candidates:
            LOG.info(
                '  Anders brief row for %s has no DT=Brief media '
                '(notice only or sealed) — treating brief as unavailable',
                case_number,
            )
            return None, None

        out_dir = ROOT / 'data' / 'anders_briefs'
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', case_number)
        pdf_path = out_dir / f'{safe}_anders_brief.pdf'
        # Stage under a .part name so a rejected notice never lands at the
        # final brief path and we never need to delete a written file.
        part_path = out_dir / f'{safe}_anders_brief.pdf.part'

        for brief_url in candidates:
            try:
                pr = requests.get(brief_url, headers=headers, timeout=60)
                if pr.status_code != 200 or pr.content[:5] != b'%PDF-':
                    continue
                part_path.write_bytes(pr.content)
                if _looks_like_clerk_notice(part_path):
                    LOG.warning(
                        '  Downloaded PDF for %s is a clerk notice-of-filing '
                        'letter, not an Anders brief — skipping',
                        case_number,
                    )
                    # Leave .part in place for inspection; overwrite on next try.
                    continue
                part_path.replace(pdf_path)
                LOG.info('  Downloaded Anders brief: %s', pdf_path.name)
                return brief_url, pdf_path
            except Exception as e:
                LOG.warning('  Brief download failed: %s', e)

        # Candidates existed but every download failed or was a notice.
        LOG.info(
            '  No usable Anders brief PDF for %s after filtering notices',
            case_number,
        )
        return None, None

    return None, None


# ── Claude calls ──────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    # Prefer the outermost balanced object (first '{' .. last '}'). This handles
    # a brace appearing inside a string value (e.g. the "notes" field), which the
    # old brace-free regex `\{[^{}]*\}` would refuse to match, silently dropping
    # an otherwise-valid verdict to {} — and, for the brief judgment, to a
    # spurious null rather than the model's real True/False.
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    # Fallback: the original brace-free match, in case the outer slice spans two
    # separate objects (e.g. a prose reply that happens to contain stray braces).
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
        raw, model_used = call_claude_ex(OPINION_PROMPT + text, timeout=120)
    except Exception as e:
        LOG.warning('Claude opinion analysis failed: %s', e)
        return {}
    result = _parse_json(raw)
    if result:
        # Record the model that actually answered — call_claude_ex falls
        # CLI(Opus)→SDK(Opus)→Grok, and a silent downgrade to Grok must be
        # visible rather than masked by a hardcoded model string.
        result['_model'] = model_used
    return result


def analyze_brief(pdf_path: Path) -> dict:
    text = extract_text(pdf_path)
    if not text:
        return {}
    try:
        raw, model_used = call_claude_ex(BRIEF_PROMPT + text, timeout=120)
    except Exception as e:
        LOG.warning('Claude brief analysis failed: %s', e)
        return {}
    result = _parse_json(raw)
    if result:
        result['_model'] = model_used
    return result


def brief_field_bools(br: dict) -> tuple[bool | None, bool | None]:
    """Return (discusses_elements, walks_through_testimony) from a brief verdict.

    Accepts the two-field schema and the legacy single brief_lists_elements
    field (legacy True/False maps to discusses only; walks stays None).
    """
    if not br:
        return None, None
    discusses = br.get('brief_discusses_elements')
    walks = br.get('brief_walks_through_testimony')
    if discusses is None and walks is None and 'brief_lists_elements' in br:
        return br.get('brief_lists_elements'), None
    return discusses, walks


def brief_has_elements_or_testimony(br: dict) -> bool | None:
    """Screening helper: True if the brief discusses elements OR walks through
    testimony; False only if it does neither; None if unparseable.

    This is not a legal-sufficiency judgment. It only decides whether the brief
    clears the daily-report screen (flag only when both are absent).
    """
    discusses, walks = brief_field_bools(br)
    if discusses is True or walks is True:
        return True
    if discusses is False and walks is False:
        return False
    # Legacy single-field responses: walks is None; discusses holds the old value.
    if walks is None and discusses is not None and 'brief_lists_elements' in br:
        return discusses
    return None


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
    op_model = result.get('_model')

    LOG.info('  is_anders=%s is_trial=%s opinion_lists_elements=%s (model=%s)',
             is_anders, is_trial, opinion_lists, op_model)

    brief_lists = None
    brief_discusses = None
    brief_walks = None
    brief_url = None
    brief_pdf_path = None
    # Every model that answered a brief judgment for this case, in order — so the
    # stored provenance shows if any brief verdict came from a fallback (Grok).
    brief_models: list = []

    def _record_brief_verdict(br: dict) -> None:
        nonlocal brief_lists, brief_discusses, brief_walks
        d, w = brief_field_bools(br)
        if d is not None:
            brief_discusses = d
        if w is not None:
            brief_walks = w
        brief_lists = brief_has_elements_or_testimony(br)
        LOG.info(
            '  brief_has_elements_or_testimony=%s (discusses_elements=%s '
            'walks_through_testimony=%s model=%s)',
            brief_lists, brief_discusses, brief_walks, br.get('_model'),
        )

    # If Anders and is_trial ambiguous from opinion, consult the brief
    if is_anders and is_trial is None:
        LOG.info('  is_trial ambiguous from opinion; fetching brief to resolve')
        brief_url, brief_pdf = fetch_anders_brief(case_number, court)
        if brief_pdf:
            brief_pdf_path = str(brief_pdf)
            br = analyze_brief(brief_pdf)
            brief_models.append(br.get('_model'))
            resolved = br.get('is_trial')
            if resolved is not None:
                is_trial = resolved
                LOG.info('  is_trial resolved from brief: %s', is_trial)
            _record_brief_verdict(br)
        else:
            LOG.info('  Anders brief not found or not downloadable')
        time.sleep(1.0)

    # If Anders + trial + opinion did NOT list elements, check the brief (if not already fetched)
    if is_anders and is_trial and opinion_lists is False and brief_url is None:
        LOG.info('  Fetching Anders brief from search.txcourts.gov...')
        brief_url, brief_pdf = fetch_anders_brief(case_number, court)
        if brief_pdf:
            brief_pdf_path = str(brief_pdf)
            br = analyze_brief(brief_pdf)
            brief_models.append(br.get('_model'))
            _record_brief_verdict(br)
        else:
            LOG.info('  Anders brief not found or not downloadable')
        time.sleep(1.0)

    # Verify before flagging DEFICIENT. A DEFICIENT flag publicly accuses a named
    # appointed attorney and the court, so a single brief judgment is too weak to
    # rest it on — one flaky sample, or a silent fallback to Grok during an Opus
    # outage, produced exactly that false positive on 02-25-00442-CR (Criminal
    # Trespass), whose brief does recite the elements and the supporting evidence.
    # When the cheap first pass would flag (opinion False + brief has neither
    # elements discussion nor testimony walkthrough), re-run the brief judgment
    # to best-of-3 and require a majority of explicit False votes to stand. A
    # tie, a majority True, or unparseable votes clear the flag.
    if (is_anders and is_trial and opinion_lists is False
            and brief_lists is False and brief_pdf_path):
        brief_pdf = Path(brief_pdf_path)
        false_votes = 1  # the first pass already returned False
        true_votes = 0
        last_true_br = None
        for _ in range(2):
            time.sleep(1.0)
            br = analyze_brief(brief_pdf)
            brief_models.append(br.get('_model'))
            v = brief_has_elements_or_testimony(br)
            if v is True:
                true_votes += 1
                last_true_br = br
            elif v is False:
                false_votes += 1
        if false_votes >= 2:
            brief_lists = False
            brief_discusses = False
            brief_walks = False
            LOG.info('  brief FLAG confirmed (%d neither / %d has-content of 3); '
                     'models=%s', false_votes, true_votes, brief_models)
        else:
            if last_true_br is not None:
                _record_brief_verdict(last_true_br)
            else:
                brief_lists = True
            LOG.info('  brief NOT flagged on re-check (%d neither / %d has-content '
                     'of 3); clearing flag; models=%s',
                     false_votes, true_votes, brief_models)

    # Provenance of the models that actually answered — replaces the old
    # hardcoded 'claude-opus-4-7', which lied whenever the chain fell back.
    def _fmt_models(models: list) -> str:
        seen = [m for m in models if m]
        # de-dup preserving order
        uniq = list(dict.fromkeys(seen))
        return '+'.join(uniq) if uniq else 'unknown'

    model_provenance = f"opinion={_fmt_models([op_model])}"
    if brief_models:
        model_provenance += f" brief={_fmt_models(brief_models)}"

    # Upsert analysis record
    if existing and reanalyze:
        conn.execute(
            '''UPDATE anders_analyses SET
               is_anders=?, is_trial=?, opinion_lists_elements=?,
               brief_lists_elements=?, brief_discusses_elements=?,
               brief_walks_through_testimony=?, brief_url=?, brief_pdf_path=?,
               offense_name=?, notes=?, analyzed_at=datetime('now','localtime'),
               model=?
               WHERE opinion_id=?''',
            (
                _sql_bool(is_anders), _sql_bool(is_trial),
                _sql_bool(opinion_lists), _sql_bool(brief_lists),
                _sql_bool(brief_discusses), _sql_bool(brief_walks),
                brief_url, brief_pdf_path, offense_name, notes,
                model_provenance, op_id,
            ),
        )
    else:
        conn.execute(
            '''INSERT OR REPLACE INTO anders_analyses
               (opinion_id, case_number, court, opinion_date,
                is_anders, is_trial, opinion_lists_elements,
                brief_lists_elements, brief_discusses_elements,
                brief_walks_through_testimony, brief_url, brief_pdf_path,
                offense_name, notes, model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                op_id, case_number, court, op_date,
                _sql_bool(is_anders), _sql_bool(is_trial),
                _sql_bool(opinion_lists), _sql_bool(brief_lists),
                _sql_bool(brief_discusses), _sql_bool(brief_walks),
                brief_url, brief_pdf_path, offense_name, notes,
                model_provenance,
            ),
        )
    conn.commit()

    # Flag if Anders + trial + opinion misses elements+evidence AND the brief
    # neither discusses elements nor walks through testimony.
    if is_anders and is_trial:
        failure_reason = None
        if opinion_lists is False and brief_lists is False:
            failure_reason = (
                'Opinion does not list elements and supporting evidence; '
                'Anders brief neither discusses the elements nor walks '
                'through the trial testimony'
            )
        elif opinion_lists is False and brief_lists is None:
            failure_reason = (
                'Opinion does not list elements and supporting evidence; '
                'brief not available for check'
            )

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

def build_summary(conn: sqlite3.Connection, target_date: str) -> dict:
    """Compile the daily Anders summary used by the heartbeat email."""
    total = conn.execute(
        'SELECT COUNT(*) FROM opinions WHERE opinion_date=?',
        (target_date,),
    ).fetchone()[0]

    rows = conn.execute(
        '''SELECT aa.case_number, aa.court, aa.is_trial,
                  aa.opinion_lists_elements, aa.brief_lists_elements,
                  aa.brief_discusses_elements, aa.brief_walks_through_testimony,
                  aa.brief_url, aa.offense_name, o.case_url
             FROM anders_analyses aa
             LEFT JOIN opinions o ON o.id = aa.opinion_id
            WHERE aa.opinion_date=? AND aa.is_anders=1
            ORDER BY aa.case_number''',
        (target_date,),
    ).fetchall()

    cols = ('case_number', 'court', 'is_trial', 'opinion_lists',
            'brief_lists', 'brief_discusses', 'brief_walks',
            'brief_url', 'offense', 'case_url')
    anders = [dict(zip(cols, r)) for r in rows]

    plea = [a for a in anders if a['is_trial'] == 0]
    trial = [a for a in anders if a['is_trial'] == 1]
    unknown = [a for a in anders if a['is_trial'] not in (0, 1)]

    opinion_covers = [a for a in trial if a['opinion_lists'] == 1]
    # Opinion missed elements+evidence; brief discusses the statutory elements.
    brief_discusses_elements = [
        a for a in trial
        if a['opinion_lists'] == 0 and a['brief_discusses'] == 1
    ]
    # Opinion missed; brief walks through evidence/testimony but never specifies
    # the elements — note in the email; not treated as DEFICIENT by itself.
    brief_testimony_no_elements = [
        a for a in trial
        if a['opinion_lists'] == 0
        and a['brief_discusses'] != 1
        and a['brief_walks'] == 1
    ]
    # Union used for subject-line / legacy "brief_covers" counts.
    brief_covers = [
        a for a in trial
        if a['opinion_lists'] == 0 and a['brief_lists'] == 1
    ]
    deficient = [a for a in trial if a['opinion_lists'] == 0
                 and a['brief_lists'] == 0]
    brief_unavailable = [a for a in trial if a['opinion_lists'] == 0
                         and a['brief_lists'] is None]

    return {
        'total_opinions': total,
        'anders_count': len(anders),
        'plea_count': len(plea),
        'trial_count': len(trial),
        'unknown_count': len(unknown),
        'opinion_covers': opinion_covers,
        'brief_covers': brief_covers,
        'brief_discusses_elements': brief_discusses_elements,
        'brief_testimony_no_elements': brief_testimony_no_elements,
        'deficient': deficient,
        'brief_unavailable': brief_unavailable,
        'all_anders': anders,
    }


def _subject(summary: dict, target_date: str) -> str:
    flagged = len(summary['deficient']) + len(summary['brief_unavailable'])
    if flagged:
        return (f'Anders Project — DEFICIENT: {flagged} case(s) — '
                f'{target_date}')
    if summary['anders_count'] == 0:
        return f'Anders Project — {target_date} — no Anders opinions'
    if summary['trial_count'] == 0:
        return (f'Anders Project — {target_date} — '
                f'{summary["anders_count"]} Anders (all plea/revocation)')
    testimony_only = len(summary.get('brief_testimony_no_elements') or [])
    if testimony_only:
        return (f'Anders Project — {target_date} — '
                f'{testimony_only} brief(s) walk through evidence, '
                f'no elements')
    if summary['brief_covers']:
        return (f'Anders Project — {target_date} — '
                f'{summary["trial_count"]} trial Anders, '
                f'{len(summary["brief_covers"])} covered by brief only')
    return (f'Anders Project — {target_date} — '
            f'{summary["trial_count"]} trial Anders, opinions list elements')


def _bullet_list_text(cases: list[dict]) -> list[str]:
    out = []
    for a in cases:
        offense = a.get('offense') or '—'
        out.append(f'      • {a["case_number"]} ({a["court"]}) — {offense}')
    return out


def _html_report(items: list[dict], target_date: str,
                 summary: dict | None = None) -> str:
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

    deficiency_section = ''
    if items:
        deficiency_section = f"""
<h3>Deficient cases</h3>
<div class="banner"><strong>{len(items)} case(s)</strong> where appointed counsel
filed an Anders brief in a trial case, the COA opinion does not list the elements
and supporting evidence, and the brief neither discusses the elements nor walks
through the trial testimony.
</div>
<table>
<thead><tr>
  <th>Case</th><th>Court</th><th>Offense</th><th>Deficiency</th><th>Brief</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
"""

    heartbeat_section = ''
    if summary is not None:
        def _li(cases):
            if not cases:
                return ''
            lis = ''.join(
                f'<li>{a["case_number"]} ({a["court"]}) — '
                f'{a.get("offense") or "—"}</li>'
                for a in cases
            )
            return f'<ul>{lis}</ul>'

        s = summary
        trial_block = ''
        if s['trial_count']:
            parts = [f'<li>Trial: {s["trial_count"]}']
            if s['opinion_covers']:
                parts.append(
                    f'<div>Opinion lists elements: '
                    f'{len(s["opinion_covers"])}{_li(s["opinion_covers"])}</div>'
                )
            if s.get('brief_discusses_elements'):
                parts.append(
                    f'<div>Opinion does not; brief discusses elements: '
                    f'{len(s["brief_discusses_elements"])}'
                    f'{_li(s["brief_discusses_elements"])}</div>'
                )
            if s.get('brief_testimony_no_elements'):
                parts.append(
                    f'<div><strong>NOTE — brief walks through the evidence '
                    f'but does not specify the elements:</strong> '
                    f'{len(s["brief_testimony_no_elements"])}'
                    f'{_li(s["brief_testimony_no_elements"])}'
                    f'<p style="margin:6px 0 0;font-size:13px">'
                    f'For some offenses (murder, theft) the elements may be '
                    f'obvious from the narrative. For others there may be more '
                    f'complexity than an Anders lawyer recognized.</p></div>'
                )
            if s['deficient']:
                parts.append(
                    f'<div><strong>Neither (DEFICIENT):</strong> '
                    f'{len(s["deficient"])}{_li(s["deficient"])}</div>'
                )
            if s['brief_unavailable']:
                parts.append(
                    f'<div>Brief unavailable: '
                    f'{len(s["brief_unavailable"])}'
                    f'{_li(s["brief_unavailable"])}</div>'
                )
            parts.append('</li>')
            trial_block = ''.join(parts)
        else:
            trial_block = '<li>Trial: 0</li>'

        heartbeat_section = f"""
<h3>Daily heartbeat</h3>
<ul>
  <li>Opinions analyzed: {s['total_opinions']}</li>
  <li>Anders briefs identified: {s['anders_count']}</li>
  <li>Plea: {s['plea_count']}</li>
  {trial_block}
</ul>
"""

    case_pages_section = ''
    if summary is not None and summary.get('all_anders'):
        links = ''.join(
            (f'<li><a href="{a["case_url"]}">{a["case_number"]}</a> '
             f'({a["court"]}) — {a.get("offense") or "—"}</li>'
             if a.get('case_url') else
             f'<li>{a["case_number"]} ({a["court"]}) — '
             f'{a.get("offense") or "—"}</li>')
            for a in summary['all_anders']
        )
        case_pages_section = f"""
<h3>Anders case pages</h3>
<ul>{links}</ul>
"""

    return f"""<!DOCTYPE html><html><head><style>
body{{font-family:sans-serif;font-size:14px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}
th{{background:#f0f0f0}}
.banner{{background:#fff3cd;border:1px solid #ffc107;padding:10px;
         margin-bottom:16px;border-radius:4px}}
ul{{margin:4px 0}}
</style></head><body>
<h2>Anders Project — {target_date}</h2>
{heartbeat_section}
{case_pages_section}
{deficiency_section}
<p style="color:#666;font-size:12px">Generated by andersproject · {date.today().isoformat()}</p>
</body></html>"""


def _text_report(items: list[dict], target_date: str,
                 summary: dict | None = None) -> str:
    lines = [f'Anders Project — daily summary for {target_date}', '']

    if summary is not None:
        s = summary
        lines += [
            f'Opinions analyzed: {s["total_opinions"]}',
            f'Anders briefs identified: {s["anders_count"]}',
            f'  Plea: {s["plea_count"]}',
        ]
        lines.append(f'  Trial: {s["trial_count"]}')
        if s['opinion_covers']:
            lines.append(f'    Opinion lists elements: {len(s["opinion_covers"])}')
            lines += _bullet_list_text(s['opinion_covers'])
        if s.get('brief_discusses_elements'):
            lines.append(
                f'    Opinion does not; brief discusses elements: '
                f'{len(s["brief_discusses_elements"])}'
            )
            lines += _bullet_list_text(s['brief_discusses_elements'])
        if s.get('brief_testimony_no_elements'):
            lines.append(
                f'    NOTE — brief walks through the evidence but does not '
                f'specify the elements: '
                f'{len(s["brief_testimony_no_elements"])}'
            )
            lines += _bullet_list_text(s['brief_testimony_no_elements'])
            lines.append(
                '      (For some offenses (murder, theft) the elements may be '
                'obvious from the narrative. For others there may be more '
                'complexity than an Anders lawyer recognized.)'
            )
        if s['deficient']:
            lines.append(f'    NEITHER (deficient): {len(s["deficient"])}')
            lines += _bullet_list_text(s['deficient'])
        if s['brief_unavailable']:
            lines.append(
                f'    Brief unavailable: {len(s["brief_unavailable"])}'
            )
            lines += _bullet_list_text(s['brief_unavailable'])
        if s['unknown_count']:
            lines.append(f'  is_trial unknown: {s["unknown_count"]}')
        lines.append('')

    if summary is not None and summary.get('all_anders'):
        lines.append('Anders case pages:')
        for a in summary['all_anders']:
            url = a.get('case_url') or '(no link)'
            lines.append(
                f"  • {a['case_number']} ({a['court']}) — "
                f"{a.get('offense') or '—'}: {url}"
            )
        lines.append('')

    if items:
        lines.append('---')
        lines.append(f'Deficient case details — {len(items)} case(s)')
        lines.append('')
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
                attachments: list[Path], dry_run: bool = False,
                summary: dict | None = None) -> None:
    smtp_host = os.environ.get('EMAIL_SMTP_HOST', 'smtp.gmail.com')
    smtp_port = int(os.environ.get('EMAIL_SMTP_PORT', '587'))
    smtp_user = os.environ.get('EMAIL_AUTH_USER', os.environ.get('EMAIL_FROM', ''))
    smtp_pass = os.environ.get('EMAIL_PASSWORD', '')

    if summary is not None:
        subject = _subject(summary, target_date)
    else:
        subject = (f'Anders Brief Deficiency Report — {len(items)} case(s)'
                   f' — {target_date}')

    msg = MIMEMultipart('mixed')
    msg['From'] = FROM_ADDR
    msg['To'] = ', '.join(TO_ADDRS)
    msg['Subject'] = subject

    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(_text_report(items, target_date, summary), 'plain'))
    alt.attach(MIMEText(_html_report(items, target_date, summary), 'html'))
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
        print(f'To:   {", ".join(TO_ADDRS)}')
        print(f'Subj: {subject}')
        print(f'Attachments: {[p.name for p in attachments if p.exists()]}')
        print()
        print(_text_report(items, target_date, summary))
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
        server.sendmail(FROM_ADDR, TO_ADDRS, msg.as_bytes())
        server.quit()
        LOG.info('Report emailed to %s', ', '.join(TO_ADDRS))
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

    # Build the daily heartbeat summary
    summary = build_summary(conn, target_date)
    LOG.info(
        'Heartbeat: %d opinions, %d Anders (%d plea, %d trial); '
        'opinion-covers=%d, brief-covers=%d, deficient=%d, brief-unavail=%d',
        summary['total_opinions'], summary['anders_count'],
        summary['plea_count'], summary['trial_count'],
        len(summary['opinion_covers']), len(summary['brief_covers']),
        len(summary['deficient']), len(summary['brief_unavailable']),
    )

    # Collect unsent report items (deficient cases)
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

    if items:
        LOG.info('%d deficient item(s) to report', len(items))

    # Collect brief PDFs for attachment (deficient cases only)
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

    send_report(items, target_date, attachments,
                dry_run=args.dry_run, summary=summary)

    if items and not args.dry_run:
        conn.execute(
            "UPDATE anders_report_items SET emailed_at=datetime('now','localtime') "
            "WHERE emailed_at IS NULL"
        )
        conn.commit()

    conn.close()


if __name__ == '__main__':
    main()
