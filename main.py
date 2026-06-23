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
import time
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
SEARCH_RESULTS_FILE = "search_results.json"
CLASSIFICATION_FILE = "classification.json"
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
RETRY_DELAY = 2.0  # base delay in seconds for exponential backoff
HTTP_TIMEOUT = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


# ═══════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════


def retry_request(func, *args, max_retries=MAX_RETRIES, label="request", **kwargs):
    """Call func(*args, **kwargs) with exponential backoff on timeout/error."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_error = e
            if attempt < max_retries:
                delay = RETRY_DELAY * (2 ** attempt)
                print(f"\n  ⚠️  {label} attempt {attempt+1} failed ({e}), "
                      f"retrying in {delay:.0f}s…")
                time.sleep(delay)
        except Exception:
            # Don't retry on non-network errors (e.g., JSON decode errors)
            raise
    raise last_error  # type: ignore[misc]


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


def _load_search_results_file(filepath: str) -> dict:
    """Load saved search results if the file exists and matches the current query."""
    if not os.path.exists(filepath):
        return {"query": SEARCH_TERM, "total": 0, "pages_fetched": [],
                "results": [], "seen_urls": []}
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Invalidate if query changed (e.g., user modified SEARCH_TERM)
    if data.get("query") != SEARCH_TERM:
        print(f"  ⚠️  Saved search results are for query '{data.get('query')}', "
              f"current query is '{SEARCH_TERM}'. Starting fresh.")
        return {"query": SEARCH_TERM, "total": 0, "pages_fetched": [],
                "results": [], "seen_urls": []}
    return data


def _save_search_results_file(filepath: str, data: dict):
    """Atomically save search results state to disk."""
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, filepath)


def _parse_search_result(article: dict) -> SearchResult:
    """Parse a single article dict from the SASAC JSON response into a SearchResult."""
    title = re.sub(r"<[^>]+>", "", article.get("name", "")).strip()
    snippet = re.sub(r"<[^>]+>", "", article.get("summaries", "")).strip()
    return SearchResult(
        title=title,
        url=article.get("url", ""),
        date=article.get("showTime", ""),
        snippet=snippet,
        index_number=int(article.get("index_number", 0)),
        site_name=article.get("indexName", ""),
    )


def fetch_search_results(
    query: str = SEARCH_TERM,
    max_pages: int | None = None,
    page_size: int = PAGE_SIZE,
    save_file: str = SEARCH_RESULTS_FILE,
) -> list[SearchResult]:
    """Fetch all search results from the SASAC AJAX search endpoint.

    Saves results incrementally to ``save_file`` after each page so the
    fetch is resumable.  If the file already exists for the same query,
    only missing pages are fetched.
    """
    save_data = _load_search_results_file(save_file)

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

    # Rebuild in-memory state from saved file
    results: list[SearchResult] = [
        _parse_search_result(a) for a in save_data["results"]
    ]
    seen_urls: set[str] = set(save_data["seen_urls"])
    pages_fetched: set[int] = set(save_data["pages_fetched"])

    if results:
        print(f"  📄 Resumed {len(results)} search results from {save_file} "
              f"({len(pages_fetched)} pages already fetched)")

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        # Get session cookie (always needed)
        retry_request(client.get, SEARCH_FORM_URL,
                      headers={"User-Agent": USER_AGENT})

        # Determine total count – prefer saved, otherwise fetch page 1
        total = save_data.get("total", 0)
        if total == 0:
            params = {**base_params, "pageNow": "1"}
            r1 = retry_request(
                client.post, AJAX_ENDPOINT,
                content=urlencode(params), headers=headers,
                label="search page 1",
            )
            if r1.status_code != 200:
                print(f"❌ Search failed: HTTP {r1.status_code}")
                return results

            page1_data = r1.json()
            total = int(page1_data.get("num", 0))
            save_data["total"] = total

            # Process page 1 results if we haven't already
            if 1 not in pages_fetched:
                articles = page1_data.get("array", [])
                for a in articles:
                    url = a.get("url", "")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    sr = _parse_search_result(a)
                    results.append(sr)
                    save_data["results"].append(a)
                    save_data["seen_urls"].append(url)
                pages_fetched.add(1)
                save_data["pages_fetched"].append(1)
                _save_search_results_file(save_file, save_data)

        if total == 0:
            print("  No search results found.")
            return results

        total_pages = (total + page_size - 1) // page_size
        if max_pages:
            total_pages = min(total_pages, max_pages)

        print(f"  Total search results: {total}")
        print(f"  Pages to fetch: {total_pages} (page size: {page_size})")
        if pages_fetched:
            print(f"  Already fetched: {len(pages_fetched)} pages, "
                  f"{total_pages - len(pages_fetched)} remaining")

        # Build page list, skipping already-fetched pages
        remaining_pages = [
            p for p in range(1, total_pages + 1) if p not in pages_fetched
        ]

        if not remaining_pages:
            print("  ✅ All pages already fetched!")
            return results

        pbar = tqdm(total=total_pages, desc="  Searching SASAC", unit="page",
                     initial=len(pages_fetched),
                     ncols=80, bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} pages")

        for page in remaining_pages:
            params = {**base_params, "pageNow": str(page)}
            try:
                r = retry_request(
                    client.post, AJAX_ENDPOINT,
                    content=urlencode(params), headers=headers,
                    label=f"search page {page}",
                )
                if r.status_code != 200:
                    print(f"\n  ⚠️ Page {page}: HTTP {r.status_code}, stopping")
                    break

                data = r.json()
                articles = data.get("array", [])
                if not articles:
                    print(f"\n  ⚠️ Page {page}: empty, stopping")
                    break

                for a in articles:
                    url = a.get("url", "")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    results.append(_parse_search_result(a))
                    save_data["results"].append(a)
                    save_data["seen_urls"].append(url)

                pages_fetched.add(page)
                save_data["pages_fetched"].append(page)
                _save_search_results_file(save_file, save_data)

            except Exception as e:
                print(f"\n  ⚠️ Page {page} error: {e}, stopping "
                      f"(results saved to {save_file})")
                break

            pbar.update(1)

        pbar.close()

    # All done – write final state
    _save_search_results_file(save_file, save_data)
    print(f"  ✅ Collected {len(results)} unique articles "
          f"(saved to {save_file})")
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


def _load_classification_cache(
    filepath: str, fetched_map: dict[str, tuple[SearchResult, str, str | None]],
) -> tuple[dict[str, set[str]], set[str]]:
    """Load previously-saved classification results.

    Returns (url_to_firms, classified_urls).  Entries whose URL is no longer
    in ``fetched_map`` are dropped.
    """
    url_to_firms: dict[str, set[str]] = {}
    if not os.path.exists(filepath):
        return url_to_firms, set()

    with open(filepath, "r", encoding="utf-8") as f:
        cache_data = json.load(f)

    for url, firms_list in cache_data.get("classified", {}).items():
        if url in fetched_map:
            url_to_firms[url] = set(firms_list)
    return url_to_firms, set(url_to_firms.keys())


def _save_classification_cache(
    filepath: str, url_to_firms: dict[str, set[str]],
):
    """Save classification state to disk."""
    serializable = {url: sorted(firms) for url, firms in url_to_firms.items()}
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"classified": serializable}, f, ensure_ascii=False)
    os.replace(tmp, filepath)


def classify_all(
    fetched: list[tuple[SearchResult, str, str | None]],
    cache_file: str = CLASSIFICATION_FILE,
) -> tuple[list[tuple[SearchResult, set[str]]], list[tuple[SearchResult, str]]]:
    """Classify all fetched articles for military industry firm mentions.

    Saves results incrementally to ``cache_file`` so re-runs skip
    already-classified articles.
    """
    # Build lookup map from URL → (sr, text, err)
    fetched_map: dict[str, tuple[SearchResult, str, str | None]] = {}
    for sr, text, err in fetched:
        fetched_map[sr.url] = (sr, text, err)

    # Load existing classification cache
    url_to_firms, classified_urls = _load_classification_cache(
        cache_file, fetched_map
    )

    if classified_urls:
        print(f"  📄 Resumed {len(classified_urls)} already-classified articles "
              f"from {cache_file}")

    classified: list[tuple[SearchResult, set[str]]] = []
    errors: list[tuple[SearchResult, str]] = []

    # Build the final list in the same order as ``fetched``
    pbar = tqdm(total=len(fetched), desc="  Classifying", unit="art",
                initial=len(classified_urls),
                ncols=80, bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} "
                                      "[{elapsed}]")

    pbar_update_batch = 0  # Save cache every N new classifications

    for sr, text, error in fetched:
        if sr.url in url_to_firms:
            # Already classified – reuse cached result
            firms = url_to_firms[sr.url]
            if error:
                errors.append((sr, error))
            classified.append((sr, firms))
            pbar.update(1)
            continue

        if error:
            errors.append((sr, error))
            firms = classify_article(sr.snippet) if sr.snippet else set()
        else:
            firms = classify_article(text)

        classified.append((sr, firms))
        url_to_firms[sr.url] = firms

        pbar.update(1)
        pbar_update_batch += 1

        # Save periodically (every 500 articles) to disk
        if pbar_update_batch >= 500:
            _save_classification_cache(cache_file, url_to_firms)
            pbar_update_batch = 0

    pbar.close()

    # Final save
    _save_classification_cache(cache_file, url_to_firms)

    mil_count = sum(1 for _, firms in classified if firms)
    print(f"  ✅ Classified {len(classified)} articles "
          f"({mil_count} military-related)")
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
    print(f"   {SEARCH_RESULTS_FILE}   — {len(search_results)} search result links")
    print(f"   {OUTPUT_FILE}           — {article_count} articles with full text (JSONL)")
    print(f"   {CLASSIFICATION_FILE} — classification cache (resume-safe)")
    print(f"   {ANALYSIS_FILE}         — aggregate statistics")


if __name__ == "__main__":
    main()
