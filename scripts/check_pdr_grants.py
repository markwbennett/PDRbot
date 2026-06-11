#!/usr/bin/env python3
"""Check CCA hand-down pages for PDR grants and email new ones to MB.

Port of tx-judicial-scraper/weekly/index.js into the PDRbot daily run.

Scrapes https://search.txcourts.gov/DocketSrch.aspx?coa=coscca for
hand-down pages dated within the lookback window (default 7 days),
finds "PETITION FOR DISCRETIONARY REVIEW GRANTED" sections, and emails
any rows not already emailed (dedup state in data/pdr_grants_state.json).

On Thursdays, if no grant has been emailed in the past week, sends the
"No PDR grants this week" fallback so a weekly email always arrives.

Sends from the texasjudicialscrapper Gmail account
(TX_JUDICIAL_SCRAPER_GMAIL_USER/PASS, Doppler) so existing sieve rules
keep firing; falls back to EMAIL_FROM/EMAIL_PASSWORD if those are unset.

Usage:
    check_pdr_grants.py [--dry-run] [--lookback DAYS] [--no-fallback]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://search.txcourts.gov/"
DOCKET_URL = "https://search.txcourts.gov/DocketSrch.aspx?coa=coscca"
GRANT_HEADER = "PETITION FOR DISCRETIONARY REVIEW GRANTED"
RECIPIENT = "mb@ivi3.com"
TZ = ZoneInfo("America/Chicago")
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "pdr_grants_state.json"
CASE_NO_RE = re.compile(r"\b(?:PD|WR|AP)-\d{4,5}-\d{2}\b")
UA = {"User-Agent": "Mozilla/5.0 (PDRbot grants check; mb@ivi3.com)"}

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("check_pdr_grants")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Grants check: could not read state file (%s); starting fresh", e)
    return {"emailed": {}, "last_fallback": None}


def save_state(state: dict) -> None:
    # Prune dedup entries older than 60 days.
    cutoff = (datetime.now(TZ) - timedelta(days=60)).date().isoformat()
    state["emailed"] = {
        k: v for k, v in state["emailed"].items() if v >= cutoff
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def recent_handdown_pages(lookback_days: int) -> list[tuple[str, str]]:
    """Return (iso_date, url) for hand-down pages dated within the window."""
    resp = requests.get(DOCKET_URL, headers=UA, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    today = datetime.now(TZ).date()
    earliest = today - timedelta(days=lookback_days)
    pages = []
    for row in soup.select("tr.rgRow, tr.rgAltRow"):
        for anchor in row.select("td a"):
            text = anchor.get_text(strip=True)
            try:
                page_date = datetime.strptime(text, "%m/%d/%Y").date()
            except ValueError:
                continue
            href = anchor.get("href")
            if href and earliest <= page_date <= today:
                pages.append((page_date.isoformat(), urljoin(BASE_URL, href)))
    return pages


def extract_grant_rows(page_url: str) -> list[tuple[str, str]]:
    """Return (header_text, row_html) for each grant row on the page."""
    resp = requests.get(page_url, headers=UA, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = []
    for header in soup.select("div.header"):
        header_text = header.get_text(strip=True)
        if GRANT_HEADER not in header_text.upper():
            continue
        for sib in header.find_next_siblings("div"):
            classes = sib.get("class") or []
            if "header" in classes:
                break
            if "rowx" not in classes:
                continue
            for anchor in sib.select("a"):
                if anchor.get("href"):
                    anchor["href"] = urljoin(BASE_URL, anchor["href"])
            rows.append((header_text, sib.decode()))
    return rows


def row_key(page_date: str, row_html: str) -> str:
    """Dedup key: case number if present, else a hash of the row text."""
    text = BeautifulSoup(row_html, "html.parser").get_text(" ", strip=True)
    m = CASE_NO_RE.search(text)
    if m:
        return f"{page_date}:{m.group(0)}"
    return f"{page_date}:{hashlib.sha1(text.encode()).hexdigest()[:16]}"


def send_email(subject: str, body: str, html: bool, dry_run: bool) -> bool:
    user = os.environ.get("TX_JUDICIAL_SCRAPER_GMAIL_USER") or os.environ.get("EMAIL_FROM")
    password = os.environ.get("TX_JUDICIAL_SCRAPER_GMAIL_PASS") or os.environ.get("EMAIL_PASSWORD")
    if os.environ.get("TX_JUDICIAL_SCRAPER_GMAIL_USER"):
        host, port = "smtp.gmail.com", 587
    else:
        host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("EMAIL_SMTP_PORT", "587"))

    if dry_run:
        log.info("Grants check (dry-run): would send %r from %s:\n%s", subject, user, body)
        return True
    if not user or not password:
        log.error("Grants check: no email credentials in environment")
        return False

    msg = MIMEText(body, "html" if html else "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = RECIPIENT
    with smtplib.SMTP(host, port, timeout=60) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [RECIPIENT], msg.as_string())
    log.info("Grants check: sent %r to %s", subject, RECIPIENT)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print instead of emailing/saving state")
    parser.add_argument("--lookback", type=int, default=7, help="days of hand-down pages to scan")
    parser.add_argument("--no-fallback", action="store_true", help="skip the Thursday no-grants email")
    parser.add_argument("--seed", action="store_true",
                        help="mark every currently visible grant row as already emailed, send nothing")
    args = parser.parse_args()

    state = load_state()
    today = datetime.now(TZ).date()

    pages = recent_handdown_pages(args.lookback)
    log.info("Grants check: %d hand-down page(s) in the last %d days", len(pages), args.lookback)

    new_sections: list[str] = []
    new_keys: list[str] = []
    for page_date, url in pages:
        try:
            rows = extract_grant_rows(url)
        except requests.RequestException as e:
            log.warning("Grants check: failed to fetch %s (%s)", url, e)
            continue
        fresh = []
        for header_text, row_html in rows:
            key = row_key(page_date, row_html)
            if key in state["emailed"]:
                continue
            fresh.append(row_html)
            new_keys.append(key)
        if fresh:
            new_sections.append(f"<h3>{GRANT_HEADER} — {page_date}</h3>" + "".join(fresh))
        log.info("Grants check: %s — %d grant row(s), %d new", page_date, len(rows), len(fresh))

    if args.seed:
        for key in new_keys:
            state["emailed"][key] = today.isoformat()
        save_state(state)
        log.info("Grants check: seeded state with %d row(s); no email sent", len(new_keys))
        return 0

    if new_sections:
        html_body = "<ul>" + "".join(f"<li>{s}</li>" for s in new_sections) + "</ul>"
        if send_email("DISCRETIONARY REVIEW GRANTED", html_body, html=True, dry_run=args.dry_run):
            if not args.dry_run:
                for key in new_keys:
                    state["emailed"][key] = today.isoformat()
                save_state(state)
        return 0

    # Thursday fallback: guarantee one email per hand-down week.
    if args.no_fallback or today.weekday() != 3:
        return 0
    week_ago = (today - timedelta(days=6)).isoformat()
    emailed_this_week = any(v >= week_ago for v in state["emailed"].values())
    fallback_this_week = bool(state.get("last_fallback")) and state["last_fallback"] >= week_ago
    if not emailed_this_week and not fallback_this_week:
        if send_email("No PDR grants this week", "No Petition found for this week.",
                      html=False, dry_run=args.dry_run):
            if not args.dry_run:
                state["last_fallback"] = today.isoformat()
                save_state(state)
    else:
        log.info("Grants check: fallback suppressed (already emailed this week)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
