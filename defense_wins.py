#!/usr/bin/env python3
"""Find defense wins on the COA hand-down dockets.

Port of tx-judicial-scraper/daily/cca-daily-scrapper.js into PDRbot.
Scans each court of appeals' dated docket page
(Docket.aspx?coa=coaNN&FullDate=MM/DD/YYYY) over a lookback window and
flags criminal cases as defense wins under the legacy scraper's two
rules, read off the docket's own style and disposition columns:

  1. The State appealed and lost — case style starts with "State" or
     "The State" and the disposition starts with "Affirm".
  2. The defendant appealed and won — case style does not start with
     "State"/"The State" and the disposition starts with "Reverse".

Already-reported wins are tracked in data/defense_wins_state.json so
the daily report only carries new ones. The lookback window (default
5 days) means a win posted after the morning run lands in the next
morning's report instead of being missed.

CLI (for testing):
    defense_wins.py [--dry-run] [--lookback DAYS]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://search.txcourts.gov/"
TZ = ZoneInfo("America/Chicago")
STATE_FILE = Path(__file__).resolve().parent / "data" / "defense_wins_state.json"
COA_NUMBERS = range(1, 15)
CRIMINAL_RE = re.compile(r"-cr\b", re.IGNORECASE)
UA = {"User-Agent": "Mozilla/5.0 (PDRbot defense-wins check; mb@ivi3.com)"}

logger = logging.getLogger(__name__)


def load_state(state_file: Path = STATE_FILE) -> dict:
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Defense wins: could not read state file (%s); starting fresh", e)
    return {"reported": {}}


def save_state(state: dict, state_file: Path = STATE_FILE) -> None:
    cutoff = (datetime.now(TZ) - timedelta(days=60)).date().isoformat()
    state["reported"] = {k: v for k, v in state["reported"].items() if v >= cutoff}
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_file)


def _is_state_style(style: str) -> bool:
    s = style.lower().lstrip()
    return s.startswith("state") or s.startswith("the state")


def scrape_docket_wins(coa_num: int, date) -> list[dict]:
    """Return defense-win rows from one COA's docket page for one date."""
    url = f"{BASE_URL}Docket.aspx?coa=coa{coa_num:02d}&FullDate={date.strftime('%m/%d/%Y')}"
    resp = requests.get(url, headers=UA, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    wins = []
    for row in soup.select("tr.rgRow, tr.rgAltRow"):
        first_td = row.find("td")
        if first_td is None:
            continue
        anchor = first_td.find("a")
        if anchor is None:
            continue
        case_no = anchor.get_text(strip=True)
        if not CRIMINAL_RE.search(case_no):
            continue

        style_td = row.select_one("td.caseStyle")
        disp_td = row.select_one("td.caseDisp")
        style = style_td.get_text(" ", strip=True) if style_td else ""
        disposition = disp_td.get_text(" ", strip=True) if disp_td else ""
        disp_lower = disposition.lower()

        state_appealed = _is_state_style(style)
        is_win = (state_appealed and disp_lower.startswith("affirm")) or (
            not state_appealed and disp_lower.startswith("reverse")
        )
        if not is_win:
            continue

        # Docket pages sometimes carry JS-template fragments in media hrefs
        # (e.g. `" + this.CurrentWebState.CurrentCourt + @"`); only keep a
        # PDF link when the href is a clean URL.
        pdf_url = None
        for a in first_td.find_all("a"):
            href = a.get("href", "")
            if ("SearchMedia" in href or href.lower().endswith(".pdf")) \
                    and not re.search(r"[\s\"']", href):
                pdf_url = urljoin(BASE_URL, href)
                break

        wins.append({
            "date": date.isoformat(),
            "court": f"COA{coa_num:02d}",
            "case_number": case_no,
            "style": style,
            "disposition": disposition,
            "case_url": urljoin(BASE_URL, anchor.get("href", "")),
            "pdf_url": pdf_url,
        })
    return wins


def collect_defense_wins(lookback_days: int = 5,
                         state_file: Path = STATE_FILE) -> tuple[list[dict], dict]:
    """Scan all COAs over the lookback window; return (new_wins, state).

    Wins already recorded in the state file are filtered out. Call
    mark_reported() after the wins have actually been emailed.
    """
    state = load_state(state_file)
    today = datetime.now(TZ).date()
    dates = [today - timedelta(days=i) for i in range(lookback_days + 1)]
    dates = [d for d in dates if d.weekday() != 6]  # no Sunday hand-downs

    new_wins: list[dict] = []
    for date in dates:
        for coa_num in COA_NUMBERS:
            try:
                rows = scrape_docket_wins(coa_num, date)
            except requests.RequestException as e:
                logger.warning("Defense wins: COA%02d %s fetch failed (%s)", coa_num, date, e)
                continue
            for win in rows:
                key = f"{win['date']}:{win['case_number']}"
                if key not in state["reported"]:
                    new_wins.append(win)
            time.sleep(0.2)

    new_wins.sort(key=lambda w: (w["date"], w["court"], w["case_number"]), reverse=True)
    logger.info("Defense wins: %d new win(s) in the last %d days", len(new_wins), lookback_days)
    return new_wins, state


def mark_reported(state: dict, wins: list[dict], state_file: Path = STATE_FILE) -> None:
    today = datetime.now(TZ).date().isoformat()
    for win in wins:
        state["reported"][f"{win['date']}:{win['case_number']}"] = today
    save_state(state, state_file)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print findings; do not update state")
    parser.add_argument("--lookback", type=int, default=5)
    args = parser.parse_args()

    wins, state = collect_defense_wins(args.lookback)
    for win in wins:
        print(f"{win['date']}  {win['court']}  {win['case_number']}  "
              f"{win['disposition']}  {win['style'][:60]}")
    if not args.dry_run and wins:
        mark_reported(state, wins)
        print(f"marked {len(wins)} win(s) reported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
