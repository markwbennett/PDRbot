# PDRbot

## Identity
- **Purpose**: Daily scraper + Claude-powered analyzer for criminal opinions from all 14 Texas Courts of Appeals. Identifies PDR-worthy legal issues and emails a daily PDF report.
- **Language/stack**: Python 3.12, SQLite, reportlab (PDF), Anthropic Claude CLI.
- **Entry point**: `pdrbot.py` (monolithic; ~155 KB).
- **Key files**: `pdrbot-prompt` (static system prompt), `andersproject.py` (Anders-brief audit), `scraper.py` (COA scraper), `run_daily_pdrbot.sh` (cron wrapper).
- **Schedule**: `/etc/cron.d/pdrbot` — Mon–Sat 9:10 AM America/Chicago.
- **Data**: `data/` symlinks to `~/pdrbot-data/`.

## Now
Two-pass analysis pipeline (Haiku 4.5 triage → Opus 4.7 full pass) writes JSON-schema-constrained issue lists with PDR scores. As of 2026-05-17, `load_analysis_prompt()` appends the cca-opinions open-questions catalog (`/home/ubuntu/github/cca-opinions/reports/special-interests/catalog.json`, 79 entries) to the system prompt; the analyzer can flag matches via the new optional `matched_open_questions` field on each issue, which `render_analysis_prose()` surfaces in TERSE REPORT output and the daily PDF. First live exercise: tomorrow's 9:10 AM run.

## Known
- **Open-questions catalog dependency**: `load_analysis_prompt()` reads `/home/ubuntu/github/cca-opinions/reports/special-interests/catalog.json` on each run. If absent, a warning is logged and the static prompt is used unchanged. The catalog is regenerated every Thursday at 11 AM CT by `~/github/cca-opinions/scripts/run_all.sh`. The HTML report it accompanies is at https://iacls.org/cca-judges/.
- **Triage policy**: the Haiku triage defaults to INTERESTING; ROUTINE only fires on truly cookie-cutter dispositions (Anders, jurisdictional dismissals, etc.). Any concurrence or dissent escalates. Failure of the Haiku pass falls through to the Opus full pass — never silently drop a case.
- **JSON schema is authoritative**: `ANALYSIS_JSON_SCHEMA` defines the issue object shape. `matched_open_questions` is an optional array of `{id, explanation}`; the renderer handles its absence cleanly so pre-2026-05-17 entries continue to render.
