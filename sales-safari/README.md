# Sales Safari

Niche pain-mining pipeline. Forum-first, with Discourse and Reddit public JSON as preferred sources, XenForo/Playwright rendering for open forums, and Firecrawl as an opt-in fallback/discovery backend.

## Status
- [x] M1 - collect (Discourse JSON primary, Firecrawl fallback) + SQLite store + CLI
- [x] M2 - pain extraction (Claude, exact source-span required)
- [x] M3 - embed + cluster
- [x] M4 - demand score + advisory warning flags
- [x] M5 - rank + ideas + competitor intel + review-grounded briefs + report
- [x] GUI - FastAPI + vanilla-JS front-end with live full-pipeline progress

## Web app
```bash
.venv/Scripts/python -m uvicorn webapp.app:app --port 8000
```
Open http://localhost:8000, paste a seed URL, approve the collector(s) for that run, and watch the full pipeline run.

## Setup
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
```
`.env` needs `FIRECRAWL_API_KEY` for Firecrawl collection or topic discovery. Pain extraction is provider-configured; the default config tries a local Qwen endpoint first, then falls back to Claude on configured transient errors.

Optional Playwright fallback for open JS-rendered forums:
```bash
.venv/Scripts/python -m pip install playwright
.venv/Scripts/python -m playwright install chromium
```
Set `collection.fallback` to `playwright` or `auto`, add allowed domains under `collection.playwright.allowed_domains`, and tune `collection.thread_url_patterns` for the forum's topic URL shape. This is only for rendering public pages; it stops on 403/429, CAPTCHA, login walls, bot walls, and configured unsupported domains.

Firecrawl is opt-in. In the web app, check `Firecrawl` to approve it for that run. In config/CLI mode, set `collection.fallback` to `firecrawl` or `auto` explicitly.

Codex extraction fallback uses:
```bash
npx.cmd -y @openai/codex exec --skip-git-repo-check --ephemeral --ignore-rules --sandbox read-only --output-last-message <temp-file> -
```

## Run
```bash
.venv/Scripts/python run.py https://forum.example.com/c/some-category/1 --limit 5
```
Seed = one forum board or a single thread URL. Reports from the web pipeline are written under `reports/`.

After a run has clusters, run stages 6-12 from the CLI:
```bash
.venv/Scripts/python -m pipeline.analyze <run_id>
```

## Checks
```bash
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```
