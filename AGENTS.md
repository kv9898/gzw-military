# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Scrape the 国务院国有资产监督管理委员会 (SASAC) website (`http://www.sasac.gov.cn/index.html`) for articles containing "调研", then analyze the results to measure the proportion of military industry firms mentioned. The ten military industry firms are defined by SASTIND at `https://www.sastind.gov.cn/n10115275/n10119040/index.html`.

## Commands

```bash
# Install dependencies (add packages to pyproject.toml first, then:)
uv sync

# Run the main script
uv run python main.py

# Add a dependency
uv add <package-name>
```

## Architecture

Single-module project (`main.py`) managed with `uv` and Python 3.14. No dependencies yet — scraping libraries (e.g., `httpx` or `requests`, `beautifulsoup4` or `lxml`) will need to be added.

### Intended pipeline

1. **Fetch military firm list** — scrape or hardcode the 10 military industry firm names from SASTIND.
2. **Search SASAC** — query SASAC's site search for "调研", paginate through all result pages.
3. **Scrape articles** — fetch the full text of each search result article.
4. **Classify** — check each article for mentions of any of the 10 military industry firms.
5. **Report** — output the proportion (military-related / total) and any supporting breakdowns.

## Key Data Sources

| Source | URL |
|---|---|
| SASAC homepage | `http://www.sasac.gov.cn/index.html` |
| SASTIND military firms list | `https://www.sastind.gov.cn/n10115275/n10119040/index.html` |

## Notes

- SASAC's site search endpoint and result page structure need to be reverse-engineered (browser dev tools on the SASAC site search).
- The site may use pagination, session cookies, or anti-scraping measures — plan for polite scraping (delays, user-agent headers).
- The 10 military industry firms are centrally-administered state-owned enterprises (央企) under defense/ aerospace sectors. Their names may appear in variant forms in articles (full names vs. abbreviations).
