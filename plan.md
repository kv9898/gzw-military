# Implementation Plan: SASAC "调研" Scraper

## Context

The project scrapes the SASAC website (国资委) for articles containing "调研" and analyzes what proportion mention military industry firms (十大军工集团). The project is currently a skeleton — `main.py` is a stub. SASAC times out with plain HTTP clients (curl) but returns 200 with a browser User-Agent, so Playwright is needed for site exploration and likely for scraping.

## Key Findings from Exploration

- **SASTIND (military firms list)**: Accessible via HTTPS, UTF-8 HTML. Lists **9 firms** on the page; the well-known 10th (中国兵器装备集团有限公司) must be added manually.
- **SASAC (target site)**: `http://www.sasac.gov.cn/index.html` returns HTTP 200 with a browser UA, but curl times out consistently — a real browser (Playwright) is required.
- **Playwright**: Node.js v1.61.0 is installed system-wide; Python bindings (`playwright` PyPI package) need to be added to the project.

## The 10 Military Industry Firms (to hardcode from SASTIND + manual addition)

1. 中国核工业集团有限公司
2. 中国航天科技集团有限公司
3. 中国航天科工集团有限公司
4. 中国航空工业集团有限公司
5. 中国船舶集团有限公司
6. 中国兵器工业集团有限公司
7. 中国兵器装备集团有限公司 ← not on SASTIND page, add manually
8. 中国电子科技集团有限公司
9. 中国航空发动机集团有限公司
10. 中国电子信息产业集团有限公司

For classification, we'll match on short names/variants too (e.g. "中核集团", "航天科技", "兵器工业", "中国电科", etc.).

## Implementation Steps

### Step 1: Add dependencies

```
uv add playwright beautifulsoup4 httpx lxml
playwright install chromium
```

- `playwright` — browser automation for site exploration and JS-heavy scraping
- `httpx` — HTTP client for API calls and lightweight page fetches once endpoints are known
- `beautifulsoup4` + `lxml` — HTML parsing for search results and article text

### Step 2: Discovery script — reverse-engineer SASAC search

Write a standalone `discover_search.py` script that uses Playwright to:
1. Navigate to `http://www.sasac.gov.cn/index.html`
2. Locate the search input field and form
3. Submit a search for "调研"
4. Capture the resulting URL, query parameters, and page structure
5. Identify pagination controls and the selector for search result items
6. Print the key findings: search URL pattern, result list selector, pagination selector, total result count

This is a **one-time discovery step** — once the search API/structure is understood, the production scraper uses direct HTTP if possible, or a streamlined Playwright script.

### Step 3: Implement the scraper in `main.py`

Architecture (single file, `main.py`, ~200-300 lines):

```
main.py
├── MILITARY_FIRMS: dict — full_name → list of variant names
├── SearchResult: dataclass — title, url, date, snippet
├── fetch_search_results(query, max_pages) → list[SearchResult]
│   Uses Playwright (or httpx if possible) to paginate through SASAC search
├── fetch_article_text(url) → str
│   Fetches full article body, stripping nav/sidebar/footer
├── classify_article(text) → set[str]
│   Returns set of matched firm names (empty = non-military)
├── analyze(results: list[tuple[SearchResult, set[str]]]) → dict
│   Computes proportion, per-firm breakdown, per-month breakdown
└── main()
    Orchestrates the pipeline and prints/saves results
```

### Step 4: Output

- Print summary: "X of Y articles ({P}%) mention military industry firms"
- Per-firm breakdown: how many articles mention each firm
- Optionally save results as JSON for further analysis

## Verification

1. Run `uv run python discover_search.py` — visually confirm search URL, result CSS selectors, and pagination are correctly identified
2. Run `uv run python main.py` — should output total article count, military proportion, and per-firm breakdown
3. Spot-check a few article URLs manually to verify text extraction quality
4. Manually verify 2-3 classification results (read the article, confirm firms are/aren't mentioned)
