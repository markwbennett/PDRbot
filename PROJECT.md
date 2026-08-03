# PDRbot

## Identity
- **Purpose**: Daily scraper + Claude-powered analyzer for criminal opinions from all 14 Texas Courts of Appeals. Identifies PDR-worthy legal issues and emails a daily PDF report.
- **Language/stack**: Python 3.12, SQLite, reportlab (PDF), Anthropic Claude CLI.
- **Entry point**: `pdrbot.py` (monolithic; ~155 KB).
- **Key files**: `pdrbot-prompt` (static system prompt), `andersproject.py` (Anders-brief audit), `scraper.py` (COA scraper), `run_daily_pdrbot.sh` (cron wrapper).
- **Schedule**: `/etc/cron.d/pdrbot` — Mon–Sat 9:10 AM America/Chicago.
- **Data**: `data/` symlinks to `~/pdrbot-data/`.

## Now
2026-08-03: Anders bot no longer treats clerk notice-of-filing letters as briefs. `fetch_anders_brief()` requires `DT=Brief` media and skips `DT=ANDERS BRIEF FLD`; notice-only rows (e.g. COA03 sex cases such as `03-25-00320-CR`) return unavailable. Also uncommitted-from-prior work now landing with this save: two-field brief screen (`brief_discusses_elements` / `brief_walks_through_testimony`, flag only when neither), and `brief_harvest.enrich_cases` for every daily-email case with State-brief COS fallback. Next: deploy to the daily host and reanalyze any cases already judged against a notice PDF.

## Known
- **Open-questions catalog dependency**: `load_analysis_prompt()` reads `/home/ubuntu/github/cca-opinions/reports/special-interests/catalog.json` on each run. If absent, a warning is logged and the static prompt is used unchanged. The catalog is regenerated every Thursday at 11 AM CT by `~/github/cca-opinions/scripts/run_all.sh`. The HTML report it accompanies is at https://iacls.org/cca-judges/.
- **Triage policy**: the Haiku triage defaults to INTERESTING; ROUTINE only fires on truly cookie-cutter dispositions (Anders, jurisdictional dismissals, etc.). Any concurrence or dissent escalates. Failure of the Haiku pass falls through to the Opus full pass — never silently drop a case.
- **JSON schema is authoritative**: `ANALYSIS_JSON_SCHEMA` defines the issue object shape. `matched_open_questions` is an optional array of `{id, explanation}`; the renderer handles its absence cleanly so pre-2026-05-17 entries continue to render.
- **Anders brief media varies by court**: some COAs (COA03 sex cases especially; also often COA04/02/06/09/10) post only the clerk's notice-of-filing letter in the briefs grid — the brief itself is never publicly linked. The real brief link carries `DT=Brief` in its href; the notice is `DT=ANDERS BRIEF FLD`.
- **`fetch_anders_brief()` requires `DT=Brief` (2026-08-03)**: matches Event Type == `anders brief filed`, then takes only `DT=Brief` media. Notice-only rows (e.g. `03-25-00320-CR` on COA03) return `(None, None)` so the case is brief-unavailable, not analyzed as a deficient brief. Content backstop: if a downloaded PDF looks like a clerk notice-of-filing letter, it is skipped.
- **DEFICIENT flags are verified, not single-shot**: a DEFICIENT flag publicly names an appointed attorney and the court, so the brief judgment is re-run best-of-3 before flagging and the `model` column records which model(s) actually answered. A single flaky sample or a silent Grok fallback can no longer publish a false accusation on its own.
