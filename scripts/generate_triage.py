#!/usr/bin/env python3
"""Generate the daily triaged slip-opinion HTML for iacls.org/triage.

A companion to /slip-opinions/: same data, same masthead, but each case
gets the PDRbot triage summary — PDR score, issue headlines, novel
question, authority conflicts, matched open questions from the
CCA-judges catalog — so a reader can decide at a glance whether to open
the opinion.

Output path: /var/www/iacls.org/html/triage/index.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _slip_common import (  # noqa: E402
    COURT_NAMES,
    DEFAULT_DB,
    _split_court_label,
    group_rows,
    load_rows,
    render_colophon,
    render_masthead,
)

DEFAULT_OUT = Path("/var/www/iacls.org/html/triage/index.html")
# Open-questions catalog used to resolve matched_open_questions entries.
# Same path used by pdrbot.py's load_analysis_prompt().
DEFAULT_CATALOG = Path("/home/ubuntu/github/cca-opinions/reports/special-interests/catalog.json")
# Anchor pattern at iacls.org/cca-judges/ — the published catalog page
# uses id="q-{N}" anchors per question (best-effort link; harmless if
# the anchor doesn't exist).
CCA_JUDGES_URL = "https://iacls.org/cca-judges/"

PAGE_TITLE = "Texas Slip Opinions • Triaged"
PAGE_SUBTITLE_HTML = (
    "A digest of the criminal decisions<br>"
    "of the fourteen Texas courts of appeals hearing criminal cases"
)
PAGE_META = (
    "PDRbot's daily triage of every Texas Court of Appeals criminal "
    "opinion — PDR score, novel issues, authority splits, and matched "
    "open questions from the CCA-judges catalog."
)


# ─────────────────────────── analysis loader ───────────────────────────────

_ROUTINE_REASON_RE = re.compile(
    r"\[Triage:.*?ROUTINE\.\s*ROUTINE:\s*(.+?)\]",
    flags=re.DOTALL | re.IGNORECASE,
)


def _load_catalog(path: Path) -> dict[int, dict]:
    """Return a dict keyed by question id with the catalog entry."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[int, dict] = {}
    for q in data.get("questions") or []:
        qid = q.get("id")
        if isinstance(qid, int):
            out[qid] = q
    return out


def _parse_routine_reason(text: str) -> str | None:
    if not text:
        return None
    m = _ROUTINE_REASON_RE.search(text)
    if m:
        # Collapse internal whitespace.
        return re.sub(r"\s+", " ", m.group(1)).strip()
    # Fall back to the part after "TERSE REPORT" prose, first 200 chars.
    if "TERSE REPORT" in text:
        tail = text.split("TERSE REPORT", 1)[1].strip(": \n")
        return re.sub(r"\s+", " ", tail)[:240].strip() or None
    return None


def _resolve_matched(
    matched: list, catalog: dict[int, dict]
) -> list[dict]:
    resolved: list[dict] = []
    for m in matched or []:
        if not isinstance(m, dict):
            continue
        qid = m.get("id")
        explanation = (m.get("explanation") or "").strip()
        entry = {"id": qid, "explanation": explanation}
        if isinstance(qid, int) and qid in catalog:
            entry["judge"] = catalog[qid].get("judge") or catalog[qid].get("judge_slug") or ""
            entry["question"] = catalog[qid].get("question") or ""
        resolved.append(entry)
    return resolved


def load_analyses(db_path: Path, days: int | None, catalog_path: Path) -> dict[int, dict]:
    """Map opinion_id → triage record.

    Triage record shape:
        {
          "is_interesting":  bool,
          "case_pdr_score":  int,    # 0 when not interesting
          "issues":          list[dict],   # one per issue object in JSON
          "routine_reason":  str | None,
          "raw_text":        str,    # raw analysis_text for fallback
        }
    """
    catalog = _load_catalog(catalog_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    where = ""
    params: tuple = ()
    if days is not None:
        where = "WHERE o.opinion_date >= date('now', ?)"
        params = (f"-{days} days",)
    sql = f"""
        SELECT a.opinion_id, a.analysis_text, a.has_interesting_issues,
               a.issue_count, a.pdr_score
        FROM analysis a
        JOIN opinions o ON o.id = a.opinion_id
        {where}
    """
    out: dict[int, dict] = {}
    for opinion_id, raw, has_interesting, issue_count, pdr_score in conn.execute(sql, params):
        raw = raw or ""
        record = {
            "is_interesting": bool(has_interesting),
            "case_pdr_score": 0,
            "issues":         [],
            "routine_reason": None,
            "raw_text":       raw,
        }
        parsed: dict | None = None
        if raw.lstrip().startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
        if parsed and isinstance(parsed.get("issues"), list):
            for issue in parsed["issues"]:
                if not isinstance(issue, dict):
                    continue
                record["issues"].append({
                    "pdr_score":            int(issue.get("pdr_score") or 0),
                    "headline":             (issue.get("headline") or "").strip(),
                    "issue_description":    (issue.get("issue_description") or "").strip(),
                    "discussion":           (issue.get("discussion") or "").strip(),
                    "authority_conflicts":  (issue.get("authority_conflicts") or "").strip(),
                    "relevant_precedent":   (issue.get("relevant_precedent") or "").strip(),
                    "matched_open_questions":
                        _resolve_matched(issue.get("matched_open_questions") or [], catalog),
                })
            if record["issues"]:
                record["case_pdr_score"] = max(i["pdr_score"] for i in record["issues"])
            else:
                record["case_pdr_score"] = int(pdr_score or 0)
        else:
            record["routine_reason"] = _parse_routine_reason(raw)
            record["case_pdr_score"] = int(pdr_score or 0)
        out[opinion_id] = record
    return out


def attach_triage(by_date, analyses: dict[int, dict]) -> None:
    """Mutate by_date entries in place, adding triage fields."""
    for date_map in by_date.values():
        for court_map in date_map.values():
            for entry in court_map:
                # Pick the analysis with the highest case_pdr_score
                # across all opinion_ids backing this entry — companion
                # cases share one opinion, so this collapses cleanly.
                best: dict | None = None
                for oid in entry["opinion_ids"]:
                    rec = analyses.get(oid)
                    if rec is None:
                        continue
                    if best is None or rec["case_pdr_score"] > best["case_pdr_score"]:
                        best = rec
                if best is None:
                    best = {
                        "is_interesting": False,
                        "case_pdr_score": 0,
                        "issues":         [],
                        "routine_reason": None,
                        "raw_text":       "",
                    }
                entry["triage"] = best
                # Issues sorted by PDR score desc for the renderer.
                entry["triage"]["issues"].sort(
                    key=lambda i: (-i["pdr_score"], i["headline"])
                )
                abbrevs = {op["abbrev"] for op in entry["opinions"]}
                entry["has_dissent"] = "dis" in abbrevs
                entry["has_concurrence"] = "con" in abbrevs


def sort_entries_by_pdr(by_date) -> None:
    """Within each (date, court), sort by case_pdr_score desc, then by
    case_number. Side-effect mutation."""
    for date_map in by_date.values():
        for court, entries in date_map.items():
            entries.sort(
                key=lambda e: (
                    -e["triage"]["case_pdr_score"],
                    e["case_numbers"][0],
                )
            )


# ─────────────────────────────── rendering ─────────────────────────────────

_OWN_STYLE = r"""
  /* Triage card */
  .triage-card {
    margin: 0 0 1.6rem;
    padding: 0;
    break-inside: avoid;
  }
  .triage-card + .triage-card {
    margin-top: 1.6rem; padding-top: 1.3rem;
    border-top: 1px dotted var(--rule-soft);
  }
  .triage-card--routine + .triage-card--routine {
    margin-top: .65rem; padding-top: .65rem;
    border-top: 1px dotted var(--rule-soft);
  }

  .card__head {
    display: grid;
    grid-template-columns: 1fr auto;
    column-gap: .8rem;
    align-items: start;
  }
  .card__nums {
    font-family: var(--mono); font-size: .65em;
    letter-spacing: .015em; color: var(--ink); line-height: 1.4;
  }
  .card__num {
    color: var(--ink); text-decoration: none;
    border-bottom: 1px dotted var(--rule);
    padding-bottom: 1px; white-space: nowrap;
  }
  .card__num:hover { color: var(--accent); border-bottom-color: var(--accent); }
  .card__nums-sep { color: var(--ink); font-family: var(--serif-body); font-size: 1em; }
  .card__style-line { margin-top: .1rem; line-height: 1.3; }
  .card__style {
    font-style: italic; font-size: .85em; color: var(--ink);
    font-feature-settings: "onum" 1, "kern" 1;
  }
  .card__ops { list-style: none; padding: 0; margin: .35rem 0 0; }
  .card__op-line {
    padding-left: 1rem; line-height: 1.4;
    margin: .1rem 0; color: var(--ink);
  }
  .card__op {
    color: var(--accent); text-decoration: none;
    background-image: linear-gradient(var(--accent-soft), var(--accent-soft));
    background-size: 100% 1px; background-position: 0 100%;
    background-repeat: no-repeat;
    transition: background-size .25s ease, color .25s ease;
  }
  .card__op:hover { color: var(--ink); background-image: linear-gradient(var(--accent), var(--accent)); background-size: 100% 2px; }
  .card__op--noref { color: var(--ink); font-style: italic; }
  .side-op {
    font-family: var(--serif-sc); font-size: 1rem;
    letter-spacing: .15em; text-transform: uppercase;
    color: var(--accent); margin-left: .35rem;
    padding: .05em .35em; border: 1px solid var(--accent-soft);
    border-radius: 2px; vertical-align: 1px;
  }

  /* PDR badge */
  .pdr-badge {
    display: inline-flex; align-items: baseline; gap: .35em;
    font-family: var(--serif-sc); font-weight: 600;
    font-size: 1rem; letter-spacing: .18em;
    text-transform: uppercase; color: var(--ink);
    padding: .3em .6em .25em; border: 1px solid var(--rule);
    border-radius: 2px; white-space: nowrap;
  }
  .pdr-badge__n {
    font-family: var(--serif-display); font-weight: 700;
    font-size: 1.5rem; line-height: 1; letter-spacing: 0;
    color: var(--ink);
  }
  .pdr-badge--med { border-color: var(--rule); color: var(--ink); }
  .pdr-badge--med .pdr-badge__n { color: var(--ink); }
  .pdr-badge--high {
    border-color: var(--accent); color: var(--accent);
    background: rgba(125,29,36,.08);
  }
  .pdr-badge--high .pdr-badge__n { color: var(--accent); }

  /* Issues */
  .issues {
    margin: 1rem 0 0; padding-left: 1.2rem;
    border-left: 1px solid var(--rule-soft);
  }
  .issue + .issue { margin-top: 1.1rem; }
  .issue--hi { border-left: 3px solid var(--accent); margin-left: -1.2rem; padding-left: 1.05rem; }
  .issue__top {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: .8rem;
  }
  .issue__num {
    font-family: var(--serif-sc); font-size: 1rem;
    letter-spacing: .2em; text-transform: uppercase; color: var(--ink);
  }
  .issue__headline {
    font-family: var(--serif-display); font-weight: 600;
    font-size: 1.25rem; line-height: 1.25; color: var(--ink);
    margin: .15rem 0 .3rem;
  }
  .issue__desc { color: var(--ink); margin: .25rem 0; line-height: 1.5; }
  .issue__conflict {
    margin: .55rem 0 .15rem;
    padding: .55rem .75rem;
    background: rgba(125,29,36,.06);
    border-left: 3px solid var(--accent-soft);
    color: var(--ink); font-size: 1rem;
  }
  .issue__conflict-label {
    font-family: var(--serif-sc); font-size: 1rem;
    letter-spacing: .18em; text-transform: uppercase;
    color: var(--accent); display: block; margin-bottom: .15rem;
  }
  .issue__open-qs {
    margin: .5rem 0 0; padding: 0; list-style: none;
  }
  .issue__open-q {
    margin: .1rem 0; padding-left: 1.4rem;
    position: relative; color: var(--ink);
    font-size: 1rem;
  }
  .issue__open-q::before {
    content: "⌖"; position: absolute; left: 0; top: 0;
    color: var(--accent); font-size: 1.1rem;
  }
  .issue__open-q a {
    color: var(--accent); text-decoration: none;
    border-bottom: 1px dotted var(--accent-soft);
  }
  .issue__open-q a:hover { color: var(--ink); border-bottom-color: var(--ink); }
  .issue__details {
    margin: .55rem 0 0;
    font-size: 1rem;
    color: var(--ink);
  }
  .issue__details > summary {
    cursor: pointer; font-family: var(--serif-sc);
    font-size: 1rem; letter-spacing: .18em;
    text-transform: uppercase; color: var(--ink);
    list-style: none; user-select: none;
  }
  .issue__details > summary::before { content: "▸ "; color: var(--rule); }
  .issue__details[open] > summary::before { content: "▾ "; }
  .issue__details > div { margin: .5rem 0 0; color: var(--ink); }
  .issue__details p { margin: .25rem 0; line-height: 1.5; color: var(--ink); }
  .issue__details strong {
    font-family: var(--serif-sc); font-weight: 600;
    font-size: 1rem; letter-spacing: .18em;
    text-transform: uppercase; color: var(--ink);
    display: block; margin-top: .55rem;
  }

  /* ROUTINE cards — visually identical brightness to interesting
     cards; the only distinction is the absence of an issues block. */
  .triage-card--routine .card__ops { margin-top: .15rem; }
  .triage-card--routine .card__op-line { margin: .02rem 0; }
  .routine-reason {
    margin: .5rem 0 0;
    font-style: italic; color: var(--ink);
    font-size: 1rem; line-height: 1.45;
  }
  .routine-reason-label {
    font-family: var(--serif-sc); font-style: normal; font-size: .7em;
    letter-spacing: .18em; text-transform: uppercase;
    color: var(--ink); margin-right: .5em;
    vertical-align: .1em;
  }

  /* Section dividers between interesting and routine within a court */
  .court__divider {
    margin: 1.4rem 0 .8rem;
    text-align: center;
    font-family: var(--serif-sc); font-size: 1rem;
    letter-spacing: .22em; text-transform: uppercase;
    color: var(--ink);
  }
"""


def _pdr_badge_class(score: int) -> str:
    if score >= 7: return "pdr-badge pdr-badge--high"
    if score >= 4: return "pdr-badge pdr-badge--med"
    return "pdr-badge"


def _render_interesting_card(entry: dict) -> str:
    nums_html = _render_nums(entry)
    style = entry["style"]
    triage = entry["triage"]
    score = triage["case_pdr_score"]
    badge_class = _pdr_badge_class(score)
    badge = (
        f'<span class="{badge_class}">'
        f'<span class="pdr-badge__label">PDR</span>'
        f'<span class="pdr-badge__n">{score}</span>'
        f'</span>'
        if score else ''
    )

    op_lines: list[str] = []
    for op in entry["opinions"]:
        label = op["label"]
        if op["pdf_url"]:
            link = (
                f'<a class="card__op" href="{html.escape(op["pdf_url"])}" '
                f'target="_blank" rel="noopener">{html.escape(label)}</a>'
            )
        else:
            link = f'<span class="card__op card__op--noref">{html.escape(label)}</span>'
        badges = ""
        if op["abbrev"] == "dis":
            badges = f' <span class="side-op">Dissent</span>'
        elif op["abbrev"] == "con":
            badges = f' <span class="side-op">Concurrence</span>'
        op_lines.append(f'      <li class="card__op-line">{link}{badges}</li>')

    issues_html_parts: list[str] = []
    for i, issue in enumerate(triage["issues"], start=1):
        issues_html_parts.append(_render_issue(i, issue))
    issues_block = (
        '    <div class="issues">\n' + "\n".join(issues_html_parts) + '\n    </div>\n'
        if issues_html_parts else ''
    )

    style_line = (
        f'      <div class="card__style-line"><cite class="card__style">{html.escape(style)}</cite></div>\n'
        if style else ''
    )

    return (
        '    <article class="triage-card">\n'
        '      <div class="card__head">\n'
        f'        <div class="card__head-l">\n'
        f'          <div class="card__nums">{nums_html}</div>\n'
        f'{style_line}'
        f'        </div>\n'
        f'        <div class="card__head-r">{badge}</div>\n'
        f'      </div>\n'
        f'      <ul class="card__ops">\n' + "\n".join(op_lines) + '\n      </ul>\n'
        f'{issues_block}'
        f'    </article>\n'
    )


def _render_issue(num: int, issue: dict) -> str:
    score = issue["pdr_score"]
    hi_class = " issue--hi" if score >= 7 else ""
    badge = (
        f'<span class="{_pdr_badge_class(score)}">'
        f'<span class="pdr-badge__label">PDR</span>'
        f'<span class="pdr-badge__n">{score}</span>'
        f'</span>'
        if score else ''
    )

    head = f'<span class="issue__num">Issue {num}</span>'
    conflict = ''
    if issue["authority_conflicts"]:
        conflict = (
            '<div class="issue__conflict">'
            '<span class="issue__conflict-label">Authority conflict</span>'
            f'{html.escape(issue["authority_conflicts"])}'
            '</div>'
        )

    open_qs_items: list[str] = []
    for q in issue["matched_open_questions"]:
        qid = q.get("id")
        anchor = f"{CCA_JUDGES_URL}#q-{qid}" if isinstance(qid, int) else CCA_JUDGES_URL
        judge = q.get("judge") or ""
        question = q.get("question") or ""
        explanation = q.get("explanation") or ""
        # Compose: "Open question: [judge] on [question]. [explanation]"
        if judge or question:
            lead_text = f"{judge} on {question}" if judge and question else (judge or question)
            link = f'<a href="{html.escape(anchor)}" target="_blank" rel="noopener">{html.escape(lead_text)}</a>'
            tail = f' — {html.escape(explanation)}' if explanation else ''
            open_qs_items.append(f'<li class="issue__open-q">Open question: {link}{tail}</li>')
        elif explanation:
            open_qs_items.append(
                f'<li class="issue__open-q">Open question — '
                f'<a href="{html.escape(anchor)}" target="_blank" rel="noopener">{html.escape(explanation)}</a></li>'
            )
    open_qs = (
        '<ul class="issue__open-qs">' + "".join(open_qs_items) + '</ul>'
        if open_qs_items else ''
    )

    details = ''
    long_desc = issue.get("issue_description") or ''
    discussion = issue.get("discussion") or ''
    precedent = issue.get("relevant_precedent") or ''
    if long_desc or discussion or precedent:
        body_bits = []
        if long_desc:
            body_bits.append(
                f'<strong>Full issue</strong><p>{html.escape(long_desc)}</p>'
            )
        if discussion:
            body_bits.append(
                f'<strong>Discussion</strong><p>{html.escape(discussion)}</p>'
            )
        if precedent:
            body_bits.append(
                f'<strong>Relevant precedent</strong><p>{html.escape(precedent)}</p>'
            )
        details = (
            '<details class="issue__details">'
            '<summary>More detail</summary>'
            f'<div>{"".join(body_bits)}</div>'
            '</details>'
        )

    headline_html = (
        f'<h4 class="issue__headline">{html.escape(issue["headline"])}</h4>'
        if issue["headline"] else ''
    )

    return (
        f'      <div class="issue{hi_class}">\n'
        f'        <div class="issue__top">{head}{badge}</div>\n'
        f'        {headline_html}\n'
        f'        {conflict}\n'
        f'        {open_qs}\n'
        f'        {details}\n'
        f'      </div>'
    )


def _render_routine_card(entry: dict) -> str:
    nums_html = _render_nums(entry)
    style = entry["style"]
    triage = entry["triage"]
    reason = triage.get("routine_reason") or ""

    op_lines: list[str] = []
    for op in entry["opinions"]:
        label = op["label"]
        if op["pdf_url"]:
            link = (
                f'<a class="card__op" href="{html.escape(op["pdf_url"])}" '
                f'target="_blank" rel="noopener">{html.escape(label)}</a>'
            )
        else:
            link = f'<span class="card__op card__op--noref">{html.escape(label)}</span>'
        op_lines.append(f'      <li class="card__op-line">{link}</li>')

    style_line = (
        f'      <div class="card__style-line"><cite class="card__style">{html.escape(style)}</cite></div>\n'
        if style else ''
    )
    reason_html = (
        f'      <p class="routine-reason">'
        f'<span class="routine-reason-label">Triage</span>{html.escape(reason)}</p>\n'
        if reason else ''
    )
    return (
        '    <article class="triage-card triage-card--routine">\n'
        f'      <div class="card__nums">{nums_html}</div>\n'
        f'{style_line}'
        f'      <ul class="card__ops">\n' + "\n".join(op_lines) + '\n      </ul>\n'
        f'{reason_html}'
        f'    </article>\n'
    )


def _render_nums(entry: dict) -> str:
    parts: list[str] = []
    for cn, curl in zip(entry["case_numbers"], entry["case_urls"]):
        if curl:
            parts.append(
                f'<a class="card__num" href="{html.escape(curl)}" '
                f'target="_blank" rel="noopener">{html.escape(cn)}</a>'
            )
        else:
            parts.append(f'<span class="card__num">{html.escape(cn)}</span>')
    nums_html = '<span class="card__nums-sep" aria-hidden="true">, </span>'.join(parts)
    if entry.get("defense_win"):
        nums_html += (
            ' <span class="win-stamp" '
            'title="Defense win: defense appeal reversed, or State appeal affirmed.">'
            'Defense Win</span>'
        )
    return nums_html


def render_html(by_date) -> str:
    now = datetime.now()
    parts: list[str] = [render_masthead(PAGE_TITLE, PAGE_SUBTITLE_HTML, _OWN_STYLE, PAGE_META, now)]

    if not by_date:
        parts.append('  <p class="empty">No opinions are in the register.</p>\n')
    else:
        dates_sorted = sorted(by_date.keys(), reverse=True)
        for idx, d in enumerate(dates_sorted):
            date_label = d.strftime("%A · %B %-d, %Y").upper()
            parts.append(
                f'  <section class="day" style="--stagger:{min(idx, 12)}">\n'
                f'    <aside class="day__margin">\n'
                f'      <h2 class="dateline__date">{html.escape(date_label)}</h2>\n'
                f'    </aside>\n'
                f'    <div class="day__body">\n'
            )
            for court in sorted(by_date[d].keys()):
                court_full = COURT_NAMES.get(court, court)
                court_pretty = _split_court_label(court_full)
                parts.append(
                    f'    <section class="court">\n'
                    f'      <h3 class="court__name">{html.escape(court_pretty)}</h3>\n'
                    f'      <div class="court__cases">\n'
                )

                entries = by_date[d][court]
                interesting = [e for e in entries if e["triage"]["is_interesting"]]
                routine = [e for e in entries if not e["triage"]["is_interesting"]]
                # Both lists are already in the proper order (sort done globally).
                for e in interesting:
                    parts.append(_render_interesting_card(e))
                if interesting and routine:
                    parts.append('      <div class="court__divider">Routine dispositions</div>\n')
                for e in routine:
                    parts.append(_render_routine_card(e))

                parts.append('      </div>\n    </section>\n')
            parts.append('    </div>\n  </section>\n\n')

    parts.append(render_colophon(now))
    return "".join(parts)


# ─────────────────────────────── main ──────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    p.add_argument("--days", type=int, default=60)
    args = p.parse_args()

    if not args.db.exists():
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 1

    days = None if args.days == 0 else args.days
    rows = load_rows(args.db, days)
    by_date = group_rows(rows)
    analyses = load_analyses(args.db, days, args.catalog)
    attach_triage(by_date, analyses)
    sort_entries_by_pdr(by_date)
    html_doc = render_html(by_date)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_text(html_doc, encoding="utf-8")
    os.replace(tmp, args.out)
    n_entries = sum(len(c) for d in by_date.values() for c in d.values())
    n_interesting = sum(
        1 for d in by_date.values() for c in d.values() for e in c if e["triage"]["is_interesting"]
    )
    print(
        f"Wrote {args.out} — {n_entries} cases ({n_interesting} interesting), "
        f"{len(by_date)} dates."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
