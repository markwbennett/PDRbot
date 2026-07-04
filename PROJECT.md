# PDRbot

## Identity
- **Purpose**: Daily scraper + Claude-powered analyzer for criminal opinions from all 14 Texas Courts of Appeals. Identifies PDR-worthy legal issues and emails a daily PDF report.
- **Language/stack**: Python 3.12, SQLite, reportlab (PDF), Anthropic Claude CLI.
- **Entry point**: `pdrbot.py` (monolithic; ~155 KB).
- **Key files**: `pdrbot-prompt` (static system prompt), `andersproject.py` (Anders-brief audit), `scraper.py` (COA scraper), `run_daily_pdrbot.sh` (cron wrapper).
- **Schedule**: `/etc/cron.d/pdrbot` — Mon–Sat 9:10 AM America/Chicago.
- **Data**: `data/` symlinks to `~/pdrbot-data/`.

## Now
The Anders project (`andersproject.py`) now verifies every PDF it pulls from the search.txcourts.gov briefs grid before treating it as the Anders brief: `_is_brief_pdf()` gates on page count (a brief runs four or more pages; a notice of filing runs one or two) and rejects "FILE COPY"/"notice of filing" letterhead, and `fetch_anders_brief()` tries every PDF link in the "Anders Brief Filed" row, keeping the first that verifies and returning (None, None) when none does. Validated 23/23 against the on-disk corpus on 2026-07-02; the eight notices previously saved as briefs were quarantined to `data/anders_briefs/notices/` and their seven DB rows had `brief_pdf_path`/`brief_url` nulled (backup: `data/pdrbot.db.bak.<timestamp>`). Next step: watch the next 9:10 AM run; the change is uncommitted in the repo alongside pre-existing uncommitted edits to `pdrbot.py` and `run_daily_pdrbot.sh`.

## Known
- **Open-questions catalog dependency**: `load_analysis_prompt()` reads `/home/ubuntu/github/cca-opinions/reports/special-interests/catalog.json` on each run. If absent, a warning is logged and the static prompt is used unchanged. The catalog is regenerated every Thursday at 11 AM CT by `~/github/cca-opinions/scripts/run_all.sh`. The HTML report it accompanies is at https://iacls.org/cca-judges/.
- **Triage policy**: the Haiku triage defaults to INTERESTING; ROUTINE only fires on truly cookie-cutter dispositions (Anders, jurisdictional dismissals, etc.). Any concurrence or dissent escalates. Failure of the Haiku pass falls through to the Opus full pass — never silently drop a case.
- **JSON schema is authoritative**: `ANALYSIS_JSON_SCHEMA` defines the issue object shape. `matched_open_questions` is an optional array of `{id, explanation}`; the renderer handles its absence cleanly so pre-2026-05-17 entries continue to render.
- **Anders brief media varies by court**: some COAs (COA04, and sometimes COA02/03/06/09/10) post only the clerk’s notice-of-filing letter in the briefs grid — the brief itself is never publicly linked. The “Anders Brief Filed” row’s link order is unreliable; the real brief link carries `DT=Brief` in its href, the notice `DT=ANDERS BRIEF FLD`. `fetch_anders_brief()` therefore content-verifies each candidate by page count instead of trusting link position, and text markers are useless because a notice of filing for an Anders brief may itself cite Anders.
