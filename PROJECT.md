# PDRbot

## Identity
- **Purpose**: Daily scraper + Claude-powered analyzer for criminal opinions from all 14 Texas Courts of Appeals. Identifies PDR-worthy legal issues and emails a daily PDF report.
- **Language/stack**: Python 3.12, SQLite, reportlab (PDF), Anthropic Claude CLI.
- **Entry point**: `pdrbot.py` (monolithic; ~155 KB).
- **Key files**: `pdrbot-prompt` (static system prompt), `andersproject.py` (Anders-brief audit), `scraper.py` (COA scraper), `run_daily_pdrbot.sh` (cron wrapper).
- **Schedule**: `/etc/cron.d/pdrbot` — Mon–Sat 9:10 AM America/Chicago.
- **Data**: `data/` symlinks to `~/pdrbot-data/`.

## Now
New 2026-07-24: `brief_harvest.py` — on each defense win (from `defense_wins.collect_defense_wins()`), fetches the TAMES case page, downloads the defense side's brief PDFs to `data/briefs/<case>/`, and harvests winning counsel's name/SBOT number/email from the signature block (nearest-name-above-email attribution; skips Certificate of Service zones, eFile envelope/service-sheet pages, and blocks nearest a State-side marker). Contacts upsert into the `lawyer_contacts` table in `data/pdrbot.db` (UNIQUE on name+email; CSV mirror at `data/lawyer_contacts.csv`). Wired into both `send_email_report()` (fallback scrape path) and `run_daily_automation()` (main scrape path, in `pdrbot.py`) — win cards in the HTML and plain email now show "Winning counsel: name — SBOT — email". Tested against live wins on 2026-07-17..24; harvested cleanly (no prosecutor/service-contact leakage). Never raises — a harvest failure logs a warning and the win still gets emailed unenriched.

## Known
- **Open-questions catalog dependency**: `load_analysis_prompt()` reads `/home/ubuntu/github/cca-opinions/reports/special-interests/catalog.json` on each run. If absent, a warning is logged and the static prompt is used unchanged. The catalog is regenerated every Thursday at 11 AM CT by `~/github/cca-opinions/scripts/run_all.sh`. The HTML report it accompanies is at https://iacls.org/cca-judges/.
- **Triage policy**: the Haiku triage defaults to INTERESTING; ROUTINE only fires on truly cookie-cutter dispositions (Anders, jurisdictional dismissals, etc.). Any concurrence or dissent escalates. Failure of the Haiku pass falls through to the Opus full pass — never silently drop a case.
- **JSON schema is authoritative**: `ANALYSIS_JSON_SCHEMA` defines the issue object shape. `matched_open_questions` is an optional array of `{id, explanation}`; the renderer handles its absence cleanly so pre-2026-05-17 entries continue to render.
- **Anders brief media varies by court**: some COAs (COA04, and sometimes COA02/03/06/09/10) post only the clerk's notice-of-filing letter in the briefs grid — the brief itself is never publicly linked. The "Anders Brief Filed" row's link order is unreliable; the real brief link carries `DT=Brief` in its href, the notice `DT=ANDERS BRIEF FLD`.
- **`fetch_anders_brief()` does NOT content-verify (as of master `78f9c55`)**: it matches the row by Event Type == `anders brief filed` (commit `4ecb594`) and takes the *first* PDF link, assuming the brief precedes the notice. A page-count verification pass was prototyped in an earlier session but never committed to master and is absent from the code. For courts that post only a notice, or where link order is reversed, the fetch can still grab the wrong PDF; the brief judgment now runs best-of-3, but wrong-file *input* is a separate, still-unaddressed risk.
- **DEFICIENT flags are verified, not single-shot**: a DEFICIENT flag publicly names an appointed attorney and the court, so the brief judgment is re-run best-of-3 before flagging and the `model` column records which model(s) actually answered. A single flaky sample or a silent Grok fallback can no longer publish a false accusation on its own.
