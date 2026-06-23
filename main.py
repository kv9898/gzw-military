"""SASAC "调研" Article Scraper & Military Industry Firm Analyzer.

Pipeline:
  1. Search SASAC for "调研" via AJAX endpoint (httpx, no browser needed)
  2. Fetch full text of each article
  3. Classify each article for mentions of the 10 military industry firms
  4. Report proportion, per-firm breakdown, and per-month breakdown
"""

import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════

SEARCH_TERM = "调研"
AJAX_ENDPOINT = "http://search.sasac.gov.cn:8080/searchweb/search"
SEARCH_FORM_URL = "http://search.sasac.gov.cn:8080/searchweb/search_gzw.jsp"
PAGE_SIZE = 50  # Fetch 50 results per page (max observed: 20; try 50)

# The 10 military industry firms (十大军工集团) with their variant names
MILITARY_FIRMS: dict[str, list[str]] = {
    "中国核工业集团": ["中国核工业集团有限公司", "中核集团", "核工业集团", "中国核工业集团"],
    "中国航天科技集团": ["中国航天科技集团有限公司", "航天科技集团", "中国航天科技集团", "航天科技"],
    "中国航天科工集团": ["中国航天科工集团有限公司", "航天科工集团", "中国航天科工集团", "航天科工"],
    "中国航空工业集团": ["中国航空工业集团有限公司", "航空工业集团", "中国航空工业集团", "航空工业"],
    "中国船舶集团": ["中国船舶集团有限公司", "中国船舶集团", "船舶集团", "中国船舶"],
    "中国兵器工业集团": ["中国兵器工业集团有限公司", "兵器工业集团", "中国兵器工业集团", "兵器工业"],
    "中国兵器装备集团": ["中国兵器装备集团有限公司", "兵器装备集团", "中国兵器装备集团", "兵器装备"],
    "中国电子科技集团": ["中国电子科技集团有限公司", "中国电科", "电子科技集团", "中国电子科技集团"],
    "中国航空发动机集团": ["中国航空发动机集团有限公司", "中国航发", "航空发动机集团", "中国航空发动机集团"],
    "中国电子信息产业集团": ["中国电子信息产业集团有限公司", "中国电子", "电子信息产业集团", "CEC"],
}

# Map each variant back to its canonical firm name
VARIANT_TO_FIRM: dict[str, str] = {}
for firm_name, variants in MILITARY_FIRMS.items():
    for v in variants:
        VARIANT_TO_FIRM[v] = firm_name

# Regex pattern for matching any firm variant
FIRM_PATTERN = re.compile(
    "|".join(re.escape(v) for variants in MILITARY_FIRMS.values() for v in variants)
)

# Article content selectors to try, in priority order
CONTENT_SELECTORS = [
    "div.TRS_Editor",
    "div.article-content",
    "div.news-content",
    "div.detail-content",
    "div.content",
    "div.main-content",
    "div#content",
    "article",
    "div.article",
    "div.main",
    "div.body",
]

# Polite scraping
REQUEST_DELAY = 0.3  # seconds between article fetches
MAX_RETRIES = 3
HTTP_TIMEOUT = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


# ═══════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════


@dataclass
class SearchResult:
    """A single search result from the SASAC AJAX endpoint."""

    title: str
    url: str
    date: str  # YYYYMMDD format from showTime
    snippet: str
    index_number: int
    site_name: str = ""

    @property
    def date_obj(self) -> datetime | None:
        try:
            return datetime.strptime(self.date, "%Y%m%d")
        except ValueError:
            return None


@dataclass
class AnalysisResult:
    """Aggregate analysis of all classified articles."""

    total_articles: int = 0
    military_articles: int = 0
    per_firm: Counter = field(default_factory=Counter)
    per_month: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def proportion(self) -> float:
        if self.total_articles == 0:
            return 0.0
        return self.military_articles / self.total_articles


# ═══════════════════════════════════════════════════════════════════
# Step 1: Search SASAC for "调研" articles via AJAX endpoint
# ═══════════════════════════════════════════════════════════════════


def fetch_search_results(
    query: str = SEARCH_TERM,
    max_pages: int | None = None,
    page_size: int = PAGE_SIZE,
) -> list[SearchResult]:
    """Fetch all search results from the SASAC AJAX search endpoint.

    Paginates through all available pages (or up to max_pages) and returns
    a deduplicated list of SearchResult objects.
    """
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": USER_AGENT,
        "Referer": SEARCH_FORM_URL,
        "Origin": "http://search.sasac.gov.cn:8080",
    }

    base_params = {
        "fullText": query,
        "pageSize": str(page_size),
        "sortType": "0",
        "sortKey": "showTime",
        "sortFlag": "-1",
        "highlighter": "2",
        "keywordNavigation": "1",
        "checkSearch": "1",
        "keyType": "fullText",
        "searchType": "0",
    }

    results: list[SearchResult] = []
    seen_urls: set[str] = set()

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        # Get session cookie
        print(f"  Getting session from {SEARCH_FORM_URL} …")
        r0 = client.get(SEARCH_FORM_URL, headers={"User-Agent": USER_AGENT})
        cookies = r0.cookies

        # Fetch first page to get total count
        params = {**base_params, "pageNow": "1"}
        r1 = client.post(
            AJAX_ENDPOINT, content=urlencode(params), headers=headers, cookies=cookies
        )
        if r1.status_code != 200:
            print(f"  ❌ Search failed: HTTP {r1.status_code}")
            return results

        data = r1.json()
        total = int(data.get("num", 0))
        articles = data.get("array", [])
        total_pages = (total + page_size - 1) // page_size if total else 0

        print(f"  Total results: {total}")
        print(f"  Page size: {page_size}")
        print(f"  Total pages: {total_pages}")

        if max_pages:
            total_pages = min(total_pages, max_pages)

        # Process first page
        _extract_results(articles, results, seen_urls)
        print(f"  Page 1/{total_pages}: {len(articles)} articles (total collected: {len(results)})")

        # Fetch remaining pages
        for page in range(2, total_pages + 1):
            params["pageNow"] = str(page)
            try:
                r = client.post(
                    AJAX_ENDPOINT,
                    content=urlencode(params),
                    headers=headers,
                    cookies=cookies,
                )
                if r.status_code != 200:
                    print(f"  ⚠️ Page {page}: HTTP {r.status_code}, stopping")
                    break

                data = r.json()
                articles = data.get("array", [])
                if not articles:
                    print(f"  Page {page}: no articles, stopping")
                    break

                _extract_results(articles, results, seen_urls)
                if page % 10 == 0 or page == total_pages:
                    print(f"  Page {page}/{total_pages}: collected {len(results)} unique articles so far")

            except Exception as e:
                print(f"  ⚠️ Page {page} error: {e}")
                break

            # Small delay between pages
            time.sleep(0.1)

    print(f"  ✅ Collected {len(results)} unique articles")
    return results


def _extract_results(
    articles: list[dict], results: list[SearchResult], seen_urls: set[str]
) -> None:
    """Extract SearchResult objects from AJAX response array, deduplicating by URL."""
    for a in articles:
        url = a.get("url", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Clean HTML tags from title
        title = a.get("name", "")
        title = re.sub(r"<[^>]+>", "", title).strip()

        # Clean HTML from snippet
        snippet = a.get("summaries", "")
        snippet = re.sub(r"<[^>]+>", "", snippet).strip()

        results.append(
            SearchResult(
                title=title,
                url=url,
                date=a.get("showTime", ""),
                snippet=snippet,
                index_number=int(a.get("index_number", 0)),
                site_name=a.get("indexName", ""),
            )
        )


# ═══════════════════════════════════════════════════════════════════
# Step 2: Fetch full article text
# ═══════════════════════════════════════════════════════════════════


async def fetch_article_text(
    client: httpx.AsyncClient, url: str
) -> tuple[str, str | None]:
    """Fetch and extract the main text content from a SASAC article page.

    Returns (text, error_string).  error_string is None on success.
    """
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url, follow_redirects=True)
            if r.status_code != 200:
                return "", f"HTTP {r.status_code}"

            soup = BeautifulSoup(r.text, "lxml")

            # Try each content selector
            for sel in CONTENT_SELECTORS:
                el = soup.select_one(sel)
                if el:
                    # Remove script/style tags
                    for tag in el.select("script, style, nav, .nav, .sidebar, .footer"):
                        tag.decompose()
                    text = el.get_text(separator="\n", strip=True)
                    if len(text) > 100:  # Sanity check: real content
                        return text, None

            # Fallback: use body text
            body = soup.find("body")
            if body:
                for tag in body.select("script, style, nav, .nav, .sidebar, .footer, header, .header"):
                    tag.decompose()
                text = body.get_text(separator="\n", strip=True)
                if len(text) > 50:
                    return text, None

            return "", "No content found"

        except httpx.TimeoutException:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return "", "Timeout"
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return "", str(e)

    return "", "Unknown error"


async def fetch_all_articles(
    results: list[SearchResult],
    concurrency: int = 5,
    output_file: str | None = "articles.jsonl",
) -> list[tuple[SearchResult, str, str | None]]:
    """Fetch full text for all search results with controlled concurrency.

    Saves each article's full text + metadata to output_file (JSONL format)
    as they are fetched, so progress is preserved even if the script crashes.

    Returns list of (SearchResult, article_text, error_string).
    """
    semaphore = asyncio.Semaphore(concurrency)
    total = len(results)
    completed = 0
    write_lock = asyncio.Lock()

    # Open output file for incremental writing
    out_fp = None
    if output_file:
        out_fp = open(output_file, "w", encoding="utf-8")

    async def fetch_one(sr: SearchResult):
        nonlocal completed
        async with semaphore:
            await asyncio.sleep(REQUEST_DELAY)
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            ) as client:
                text, error = await fetch_article_text(client, sr.url)
                completed += 1
                if completed % 50 == 0 or completed == total:
                    err_count = sum(1 for _, _, e in gathered if e)
                    print(f"  Fetched {completed}/{total} articles ({err_count} errors)")
                return sr, text, error

    async def fetch_and_save(sr: SearchResult):
        sr_result, text, error = await fetch_one(sr)
        # Write to JSONL incrementally
        if out_fp and write_lock:
            async with write_lock:
                record = {
                    "title": sr_result.title,
                    "url": sr_result.url,
                    "date": sr_result.date,
                    "site_name": sr_result.site_name,
                    "text": text if not error else None,
                    "error": error,
                }
                out_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_fp.flush()
        return sr_result, text, error

    gathered: list[tuple[SearchResult, str, str | None]] = []
    batch_size = 100
    try:
        for i in range(0, total, batch_size):
            batch = results[i: i + batch_size]
            tasks = [fetch_and_save(sr) for sr in batch]
            batch_results = await asyncio.gather(*tasks)
            gathered.extend(batch_results)
    finally:
        if out_fp:
            out_fp.close()
            if output_file:
                print(f"  📄 Article texts saved to {output_file}")

    return gathered


# ═══════════════════════════════════════════════════════════════════
# Step 3: Classify articles for military industry firm mentions
# ═══════════════════════════════════════════════════════════════════


def classify_article(text: str) -> set[str]:
    """Return set of canonical firm names mentioned in the article text."""
    matched_variants = set(FIRM_PATTERN.findall(text))
    # Map variants to canonical names
    firms = set()
    for variant in matched_variants:
        firm = VARIANT_TO_FIRM.get(variant)
        if firm:
            firms.add(firm)
    return firms


def classify_all(
    fetched: list[tuple[SearchResult, str, str | None]],
) -> tuple[list[tuple[SearchResult, set[str]]], list[tuple[SearchResult, str]]]:
    """Classify all fetched articles.

    Returns:
      - classified: list of (SearchResult, set of matched firm names)
      - errors: list of (SearchResult, error_string) for articles that failed to fetch
    """
    classified: list[tuple[SearchResult, set[str]]] = []
    errors: list[tuple[SearchResult, str]] = []

    for sr, text, error in fetched:
        if error:
            errors.append((sr, error))
            # Try classifying from snippet as fallback
            firms = classify_article(sr.snippet) if sr.snippet else set()
        else:
            firms = classify_article(text)
        classified.append((sr, firms))

    return classified, errors


# ═══════════════════════════════════════════════════════════════════
# Step 4: Analyze & report
# ═══════════════════════════════════════════════════════════════════


def analyze(
    classified: list[tuple[SearchResult, set[str]]],
) -> AnalysisResult:
    """Compute aggregate statistics from classified articles."""
    result = AnalysisResult()
    result.total_articles = len(classified)

    for sr, firms in classified:
        if firms:
            result.military_articles += 1
            for firm in firms:
                result.per_firm[firm] += 1

        # Per-month breakdown
        if sr.date_obj:
            month_key = sr.date_obj.strftime("%Y-%m")
            if month_key not in result.per_month:
                result.per_month[month_key] = {"total": 0, "military": 0}
            result.per_month[month_key]["total"] += 1
            if firms:
                result.per_month[month_key]["military"] += 1

    return result


def print_report(result: AnalysisResult, classified, errors):
    """Print analysis results to stdout."""
    print()
    print("=" * 70)
    print("RESULTS: Military Industry Firm Mentions in SASAC '调研' Articles")
    print("=" * 70)

    # 1. Overall proportion
    pct = result.proportion * 100
    print(f"\n📊 OVERALL")
    print(f"   Total articles:     {result.total_articles}")
    print(f"   Military-related:   {result.military_articles}")
    print(f"   Proportion:         {result.military_articles}/{result.total_articles} = {pct:.1f}%")
    if errors:
        print(f"   Fetch errors:       {len(errors)}")

    # 2. Per-firm breakdown
    print(f"\n🏭 PER-FIRM BREAKDOWN")
    if result.per_firm:
        for firm, count in result.per_firm.most_common():
            bar = "█" * max(1, count * 50 // max(result.per_firm.values()))
            print(f"   {firm:20s}  {count:4d}  {bar}")
    else:
        print("   No military industry firm mentions found.")

    # 3. Per-month breakdown
    print(f"\n📅 PER-MONTH BREAKDOWN")
    print(f"   {'Month':<10} {'Total':>6} {'Military':>10} {'Proportion':>12}")
    print(f"   {'-'*10} {'-'*6} {'-'*10} {'-'*12}")
    for month in sorted(result.per_month.keys()):
        stats = result.per_month[month]
        t, m = stats["total"], stats["military"]
        mpct = (m / t * 100) if t > 0 else 0
        print(f"   {month:<10} {t:>6} {m:>10} {mpct:>11.1f}%")

    # 4. Top articles by number of firms mentioned
    print(f"\n📰 TOP ARTICLES (most firms mentioned)")
    multi_firm = [(sr, firms) for sr, firms in classified if len(firms) > 1]
    multi_firm.sort(key=lambda x: -len(x[1]))
    for sr, firms in multi_firm[:10]:
        print(f"   [{len(firms)} firms] {sr.title[:90]}")
        print(f"     Firms: {', '.join(sorted(firms))}")
        print(f"     {sr.url}")

    # 5. Sample non-military articles
    non_mil = [(sr, firms) for sr, firms in classified if not firms]
    if non_mil:
        print(f"\n📋 SAMPLE NON-MILITARY ARTICLES (first 5)")
        for sr, _ in non_mil[:5]:
            print(f"   [{sr.date}] {sr.title[:100]}")


def save_results(
    result: AnalysisResult,
    classified: list[tuple[SearchResult, set[str]]],
    errors: list[tuple[SearchResult, str]],
    filename: str = "analysis_results.json",
):
    """Save full results to JSON."""
    output = {
        "summary": {
            "total_articles": result.total_articles,
            "military_articles": result.military_articles,
            "proportion": result.proportion,
            "fetch_errors": len(errors),
        },
        "per_firm": dict(result.per_firm.most_common()),
        "per_month": {
            month: {"total": stats["total"], "military": stats["military"]}
            for month, stats in sorted(result.per_month.items())
        },
        "military_articles": [
            {
                "title": sr.title,
                "url": sr.url,
                "date": sr.date,
                "firms": sorted(firms),
            }
            for sr, firms in classified
            if firms
        ],
        "non_military_sample": [
            {"title": sr.title, "url": sr.url, "date": sr.date}
            for sr, firms in classified
            if not firms
        ][:50],
        "errors": [
            {"title": sr.title, "url": sr.url, "error": e} for sr, e in errors
        ],
    }
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n📄 Full results saved to {filename}")


# ═══════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════


def main():
    print("=" * 70)
    print("SASAC '调研' Article Scraper — Military Industry Firm Analyzer")
    print("=" * 70)

    # ── Step 1: Fetch search results ──
    print("\n📡 STEP 1: Fetching search results from SASAC…")
    search_results = fetch_search_results(SEARCH_TERM)

    if not search_results:
        print("❌ No search results found. Exiting.")
        sys.exit(1)

    # ── Step 2: Fetch article texts ──
    print(f"\n📄 STEP 2: Fetching full text for {len(search_results)} articles…")
    fetched = asyncio.run(fetch_all_articles(search_results))

    # Count errors
    errs = [(sr, e) for sr, _, e in fetched if e]
    ok_count = len(fetched) - len(errs)
    print(f"  ✅ {ok_count} articles fetched successfully, {len(errs)} errors")

    # ── Step 3: Classify ──
    print(f"\n🔍 STEP 3: Classifying articles for military industry firm mentions…")
    classified, classify_errors = classify_all(fetched)
    all_errors = errs + classify_errors  # fetch errors + classification fallback errors

    # ── Step 4: Analyze & report ──
    print(f"\n📊 STEP 4: Analyzing results…")
    result = analyze(classified)

    # ── Print report ──
    print_report(result, classified, all_errors)

    # ── Save analysis JSON ──
    save_results(result, classified, all_errors)

    print("\n📁 Output files:")
    print(f"   articles.jsonl       — all {len(search_results)} articles with full text")
    print("   analysis_results.json — aggregate statistics & classification")


if __name__ == "__main__":
    main()
