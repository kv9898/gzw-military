"""SASAC "调研" Article Scraper & Military Industry Firm Analyzer.

Pipeline:
  1. Search SASAC for "调研" via AJAX endpoint (httpx, no browser needed)
  2. Fetch full text of each article, saving incrementally to articles.jsonl
  3. Classify each article for mentions of the 10 military industry firms
  4. Report proportion, per-firm breakdown, and per-month breakdown

Usage:
  uv run python main.py          # fresh run (or resume from articles.jsonl)
"""

import asyncio
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from tqdm import tqdm

# ═══════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════

SEARCH_TERM = "调研"
AJAX_ENDPOINT = "http://search.sasac.gov.cn:8080/searchweb/search"
SEARCH_FORM_URL = "http://search.sasac.gov.cn:8080/searchweb/search_gzw.jsp"
PAGE_SIZE = 50  # Fetch 50 search results per AJAX page
OUTPUT_FILE = "articles.jsonl"
ANALYSIS_FILE = "analysis_results.json"

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
    """Fetch all search results from the SASAC AJAX search endpoint."""
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
        client.get(SEARCH_FORM_URL, headers={"User-Agent": USER_AGENT})

        # Fetch first page to get total count
        params = {**base_params, "pageNow": "1"}
        r1 = client.post(
            AJAX_ENDPOINT, content=urlencode(params), headers=headers
        )
        if r1.status_code != 200:
            print(f"❌ Search failed: HTTP {r1.status_code}")
            return results

        data = r1.json()
        total = int(data.get("num", 0))
        total_pages = (total + page_size - 1) // page_size if total else 0
        if max_pages:
            total_pages = min(total_pages, max_pages)

        print(f"  Total search results: {total}")
        print(f"  Pages to fetch: {total_pages} (page size: {page_size})")

        # Process pages with progress bar
        pbar = tqdm(total=total_pages, desc="  Searching SASAC", unit="page",
                     ncols=80, bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} pages")

        for page in range(1, total_pages + 1):
            params["pageNow"] = str(page)
            try:
                r = client.post(
                    AJAX_ENDPOINT,
                    content=urlencode(params),
                    headers=headers,
                )
                if r.status_code != 200:
                    print(f"\n  ⚠️ Page {page}: HTTP {r.status_code}, stopping")
                    break

                data = r.json()
                articles = data.get("array", [])
                if not articles:
                    print(f"\n  Page {page}: empty, stopping")
                    break

                for a in articles:
                    url = a.get("url", "")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    title = re.sub(r"<[^>]+>", "", a.get("name", "")).strip()
                    snippet = re.sub(r"<[^>]+>", "", a.get("summaries", "")).strip()
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

            except Exception as e:
                print(f"\n  ⚠️ Page {page} error: {e}")
                break

            pbar.update(1)

        pbar.close()

    print(f"  ✅ Collected {len(results)} unique articles")
    return results


# ═══════════════════════════════════════════════════════════════════
# Step 2: Fetch full article text (with resume support)
# ═══════════════════════════════════════════════════════════════════


def load_already_fetched(output_file: str) -> set[str]:
    """Read existing output file and return set of already-fetched URLs."""
    fetched_urls: set[str] = set()
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    url = record.get("url", "")
                    if url and not record.get("error"):
                        fetched_urls.add(url)
                except json.JSONDecodeError:
                    continue
        if fetched_urls:
            print(f"  📄 Found {len(fetched_urls)} already-fetched articles in {output_file}")
    return fetched_urls


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
                    for tag in el.select("script, style, nav, .nav, .sidebar, .footer"):
                        tag.decompose()
                    text = el.get_text(separator="\n", strip=True)
                    if len(text) > 100:
                        return text, None

            # Fallback: use body text
            body = soup.find("body")
            if body:
                for tag in body.select(
                    "script, style, nav, .nav, .sidebar, .footer, header, .header"
                ):
                    tag.decompose()
                text = body.get_text(separator="\n", strip=True)
                if len(text) > 50:
                    return text, None

            return "", "No content found"

        except httpx.TimeoutException:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2**attempt)
            else:
                return "", "Timeout"
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2**attempt)
            else:
                return "", str(e)

    return "", "Unknown error"


async def fetch_all_articles(
    results: list[SearchResult],
    output_file: str = OUTPUT_FILE,
    concurrency: int = 5,
    resume: bool = True,
) -> list[tuple[SearchResult, str, str | None]]:
    """Fetch full text for all search results with controlled concurrency.

    Saves each article incrementally to output_file (JSONL). On resume,
    skips articles already present in the output file.
    """
    # Determine which articles need fetching
    already_fetched: set[str] = set()
    if resume:
        already_fetched = load_already_fetched(output_file)

    todo = [sr for sr in results if sr.url not in already_fetched]
    skipped = len(results) - len(todo)

    if skipped > 0:
        print(f"  ⏭️  Skipping {skipped} already-fetched articles")
    if not todo:
        print("  ✅ All articles already fetched!")
        # Load existing data from file for classification
        gathered = _load_existing_results(output_file, results)
        return gathered

    print(f"  Fetching {len(todo)} remaining articles "
          f"(concurrency: {concurrency}, delay: {REQUEST_DELAY}s)")

    semaphore = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()
    pbar = tqdm(total=len(todo), desc="  Fetching articles", unit="art",
                ncols=80, bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]")

    out_fp = open(output_file, "a", encoding="utf-8")  # append mode for resume

    async def fetch_one(sr: SearchResult):
        async with semaphore:
            await asyncio.sleep(REQUEST_DELAY)
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            ) as client:
                text, error = await fetch_article_text(client, sr.url)

                # Write incrementally
                async with write_lock:
                    record = {
                        "title": sr.title,
                        "url": sr.url,
                        "date": sr.date,
                        "site_name": sr.site_name,
                        "text": text if not error else None,
                        "error": error,
                    }
                    out_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out_fp.flush()

                pbar.update(1)
                return sr, text, error

    gathered: list[tuple[SearchResult, str, str | None]] = []

    # Add pre-existing results from the file before fetching new ones
    if skipped > 0:
        pre_existing = _load_existing_results(output_file, results)
        gathered.extend(pre_existing)

    # Fetch remaining articles in batches
    batch_size = 100
    try:
        for i in range(0, len(todo), batch_size):
            batch = todo[i : i + batch_size]
            tasks = [fetch_one(sr) for sr in batch]
            batch_results = await asyncio.gather(*tasks)
            gathered.extend(batch_results)
    finally:
        pbar.close()
        out_fp.close()

    ok = sum(1 for _, _, e in gathered if not e)
    print(f"  ✅ {ok}/{len(gathered)} articles fetched successfully")
    print(f"  📄 Article texts saved to {output_file}")
    return gathered


def _load_existing_results(
    output_file: str, results: list[SearchResult]
) -> list[tuple[SearchResult, str, str | None]]:
    """Load previously-fetched articles from the JSONL output file."""
    url_to_result = {sr.url: sr for sr in results}
    gathered: list[tuple[SearchResult, str, str | None]] = []

    if not os.path.exists(output_file):
        return gathered

    with open(output_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                url = record.get("url", "")
                sr = url_to_result.get(url)
                if sr:
                    text = record.get("text") or ""
                    error = record.get("error")
                    gathered.append((sr, text, error))
            except json.JSONDecodeError:
                continue

    return gathered


# ═══════════════════════════════════════════════════════════════════
# Step 3: Classify articles for military industry firm mentions
# ═══════════════════════════════════════════════════════════════════


def classify_article(text: str) -> set[str]:
    """Return set of canonical firm names mentioned in the article text."""
    matched_variants = set(FIRM_PATTERN.findall(text))
    firms = set()
    for variant in matched_variants:
        firm = VARIANT_TO_FIRM.get(variant)
        if firm:
            firms.add(firm)
    return firms


def classify_all(
    fetched: list[tuple[SearchResult, str, str | None]],
) -> tuple[list[tuple[SearchResult, set[str]]], list[tuple[SearchResult, str]]]:
    """Classify all fetched articles."""
    classified: list[tuple[SearchResult, set[str]]] = []
    errors: list[tuple[SearchResult, str]] = []

    for sr, text, error in tqdm(fetched, desc="  Classifying", unit=" articles",
                                 ncols=80, bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}"):
        if error:
            errors.append((sr, error))
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

    pct = result.proportion * 100
    print(f"\n📊 OVERALL")
    print(f"   Total articles:     {result.total_articles}")
    print(f"   Military-related:   {result.military_articles}")
    print(f"   Proportion:         {pct:.1f}% "
          f"({result.military_articles}/{result.total_articles})")
    if errors:
        print(f"   Fetch errors:       {len(errors)}")

    print(f"\n🏭 PER-FIRM BREAKDOWN")
    if result.per_firm:
        for firm, count in result.per_firm.most_common():
            bar = "█" * max(1, count * 50 // max(result.per_firm.values()))
            print(f"   {firm:20s}  {count:4d}  {bar}")
    else:
        print("   No military industry firm mentions found.")

    print(f"\n📅 PER-MONTH BREAKDOWN")
    print(f"   {'Month':<10} {'Total':>6} {'Military':>10} {'Proportion':>12}")
    print(f"   {'-'*10} {'-'*6} {'-'*10} {'-'*12}")
    for month in sorted(result.per_month.keys()):
        stats = result.per_month[month]
        t, m = stats["total"], stats["military"]
        mpct = (m / t * 100) if t > 0 else 0
        print(f"   {month:<10} {t:>6} {m:>10} {mpct:>11.1f}%")

    # Top multi-firm articles
    print(f"\n📰 TOP ARTICLES (most firms mentioned)")
    multi_firm = [(sr, firms) for sr, firms in classified if len(firms) > 1]
    multi_firm.sort(key=lambda x: -len(x[1]))
    for sr, firms in multi_firm[:10]:
        print(f"   [{len(firms)} firms] {sr.title[:90]}")
        print(f"     Firms: {', '.join(sorted(firms))}")
        print(f"     {sr.url}")

    # Sample non-military articles
    non_mil = [(sr, firms) for sr, firms in classified if not firms]
    if non_mil:
        print(f"\n📋 SAMPLE NON-MILITARY ARTICLES (first 5)")
        for sr, _ in non_mil[:5]:
            print(f"   [{sr.date}] {sr.title[:100]}")


def save_results(
    result: AnalysisResult,
    classified: list[tuple[SearchResult, set[str]]],
    errors: list[tuple[SearchResult, str]],
    filename: str = ANALYSIS_FILE,
):
    """Save full analysis results to JSON."""
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
            {"title": sr.title, "url": sr.url, "date": sr.date,
             "firms": sorted(firms)}
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
    print(f"\n📄 Analysis saved to {filename}")


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

    # ── Step 2: Fetch article texts (with resume support) ──
    print(f"\n📄 STEP 2: Fetching full text for {len(search_results)} articles…")
    fetched = asyncio.run(fetch_all_articles(search_results, resume=True))

    errs = [(sr, e) for sr, _, e in fetched if e]
    ok_count = len(fetched) - len(errs)
    print(f"  ✅ {ok_count} articles OK, {len(errs)} errors")

    # ── Step 3: Classify ──
    print(f"\n🔍 STEP 3: Classifying articles for military industry firm mentions…")
    classified, classify_errors = classify_all(fetched)
    all_errors = errs + classify_errors

    # ── Step 4: Analyze & report ──
    print(f"\n📊 STEP 4: Analyzing results…")
    result = analyze(classified)

    print_report(result, classified, all_errors)
    save_results(result, classified, all_errors)

    # ── Output files summary ──
    article_count = sum(1 for _ in open(OUTPUT_FILE)) if os.path.exists(OUTPUT_FILE) else 0
    print(f"\n📁 Output files:")
    print(f"   {OUTPUT_FILE}   — {article_count} articles with full text (JSONL)")
    print(f"   {ANALYSIS_FILE} — aggregate statistics & classification")


if __name__ == "__main__":
    main()
