#!/usr/bin/env python3
"""Generate the daily slip-opinion HTML page for iacls.org/slip-opinions.

Reads the PDRbot SQLite database via _slip_common, groups every scraped
Texas Court of Appeals criminal opinion by release date / court /
companion-case group, and emits a single static HTML page in reverse-
chronological order. Each opinion link points at the original PDF on
search.txcourts.gov and opens in a new tab.

Output path: /var/www/iacls.org/html/slip-opinions/index.html
"""

from __future__ import annotations

import argparse
import html
import os
import sys
from datetime import datetime
from pathlib import Path

# scripts/ is on sys.path because we're run as `python scripts/generate_slip_opinions.py`
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

DEFAULT_OUT = Path("/var/www/iacls.org/html/slip-opinions/index.html")

PAGE_TITLE = "Texas Slip Opinions"
PAGE_SUBTITLE_HTML = (
    "A digest of the criminal decisions<br>"
    "of the fourteen Texas courts of appeals hearing criminal cases"
)
PAGE_META = (
    "A daily register of every criminal opinion released by the Texas "
    "Courts of Appeals. Updated each morning by PDRbot."
)

# Page-specific CSS (concatenated after _slip_common.BASE_STYLE).
_OWN_STYLE = r"""
  /* Case */
  .case {
    margin: 0 0 1.1rem; padding: 0; text-align: left;
    color: var(--ink); break-inside: avoid;
  }
  .case + .case {
    margin-top: 1.1rem; padding-top: 1.1rem;
    border-top: 1px dotted var(--rule-soft);
  }
  .case__nums {
    font-family: var(--mono); font-size: .65em;
    letter-spacing: .015em; color: var(--ink); line-height: 1.4;
  }
  .case__num {
    color: var(--ink); text-decoration: none;
    border-bottom: 1px dotted var(--rule);
    padding-bottom: 1px; white-space: nowrap;
  }
  .case__num:hover { color: var(--accent); border-bottom-color: var(--accent); }
  .case__nums-sep { color: var(--ink); font-family: var(--serif-body); font-size: 1em; }
  .case__style-line { margin-top: .1rem; line-height: 1.3; }
  .case__style {
    font-style: italic; font-size: .85em; color: var(--ink);
    font-feature-settings: "onum" 1, "kern" 1;
  }
  .case__ops { list-style: none; padding: 0; margin: .35rem 0 0; }
  .case__op-line {
    padding-left: 1rem; line-height: 1.4;
    margin: .1rem 0; color: var(--ink);
  }
  .case__op {
    color: var(--accent); text-decoration: none;
    background-image: linear-gradient(var(--accent-soft), var(--accent-soft));
    background-size: 100% 1px; background-position: 0 100%;
    background-repeat: no-repeat;
    transition: background-size .25s ease, color .25s ease;
  }
  .case__op:hover {
    color: var(--ink);
    background-image: linear-gradient(var(--accent), var(--accent));
    background-size: 100% 2px;
  }
  .case__op--noref { color: var(--ink); font-style: italic; }
  /* slip-stamp-moved-to-base: stamp CSS now lives in _slip_common.BASE_STYLE */
"""


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
            court_map = by_date[d]
            for court in sorted(court_map.keys()):
                court_full = COURT_NAMES.get(court, court)
                court_pretty = _split_court_label(court_full)
                parts.append(
                    f'    <section class="court">\n'
                    f'      <h3 class="court__name">{html.escape(court_pretty)}</h3>\n'
                    f'      <div class="court__cases">\n'
                )
                for entry in court_map[court]:
                    nums_html_parts = []
                    for cn, curl in zip(entry["case_numbers"], entry["case_urls"]):
                        if curl:
                            nums_html_parts.append(
                                f'<a class="case__num" href="{html.escape(curl)}" '
                                f'target="_blank" rel="noopener">{html.escape(cn)}</a>'
                            )
                        else:
                            nums_html_parts.append(
                                f'<span class="case__num">{html.escape(cn)}</span>'
                            )
                    nums_html = (
                        '<span class="case__nums-sep" aria-hidden="true">, </span>'
                        .join(nums_html_parts)
                    )

                    style = entry["style"]
                    op_lines = []
                    for op in entry["opinions"]:
                        label = op["label"]
                        if op["pdf_url"]:
                            op_lines.append(
                                f'        <li class="case__op-line">'
                                f'<a class="case__op" href="{html.escape(op["pdf_url"])}" '
                                f'target="_blank" rel="noopener">{html.escape(label)}</a></li>\n'
                            )
                        else:
                            op_lines.append(
                                f'        <li class="case__op-line">'
                                f'<span class="case__op case__op--noref">{html.escape(label)}</span></li>\n'
                            )

                    parts.append('        <article class="case">\n')
                    stamp_html = ''
                    if entry.get("defense_win"):
                        stamp_html = (
                            ' <span class="win-stamp" '
                            'title="Defense win: '
                            'defense appeal reversed, or State appeal affirmed.">'
                            'Defense Win</span>'
                        )
                    parts.append(f'          <div class="case__nums">{nums_html}{stamp_html}</div>\n')
                    if style:
                        parts.append(
                            f'          <div class="case__style-line">'
                            f'<cite class="case__style">{html.escape(style)}</cite></div>\n'
                        )
                    parts.append('          <ul class="case__ops">\n')
                    parts.extend(op_lines)
                    parts.append('          </ul>\n')
                    parts.append('        </article>\n')
                parts.append('      </div>\n    </section>\n')
            parts.append('    </div>\n  </section>\n\n')

    parts.append(render_colophon(now))
    return "".join(parts)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--days", type=int, default=60,
                   help="How many days back to include (0 = all).")
    args = p.parse_args()

    if not args.db.exists():
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 1

    days = None if args.days == 0 else args.days
    rows = load_rows(args.db, days)
    by_date = group_rows(rows)
    html_doc = render_html(by_date)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_text(html_doc, encoding="utf-8")
    os.replace(tmp, args.out)
    n_entries = sum(len(c) for d in by_date.values() for c in d.values())
    print(f"Wrote {args.out} — {n_entries} cases, {len(by_date)} dates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
