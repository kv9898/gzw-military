"""Discovery script: reverse-engineer SASAC's site search for "调研" articles.

This script discovers the search mechanism and prints actionable findings for main.py.

Key approach: The SASAC search form POSTs to search_gzw.jsp, which renders JS that
makes an AJAX POST to /searchweb/search returning JSON. We can call this AJAX
endpoint directly with httpx — no browser required for the actual scraper.

Output: discover_findings.json (full findings) + stdout summary.
"""

import json
from urllib.parse import urlencode

import httpx

# ── Hardcoded (discovered from Playwright exploration) ──
SEARCH_FORM_ACTION = "http://search.sasac.gov.cn:8080/searchweb/search_gzw.jsp"
AJAX_ENDPOINT = "http://search.sasac.gov.cn:8080/searchweb/search"
CONTEXT_PATH = "/searchweb"

# CSS selectors (for Playwright-based fallback scraping)
SELECTORS = {
    "result_container": "div#info ul",
    "result_item": "div#info ul li",
    "result_title": "p.zsy_schead a",
    "result_snippet": "p.zsy_scdescribe",
    "result_date": "p.zsy_scdate",
    "pagination": "div#page",
}


def test_ajax_endpoint(search_term: str = "调研") -> dict:
    """Test the AJAX search API and return findings."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": SEARCH_FORM_ACTION,
        "Origin": "http://search.sasac.gov.cn:8080",
    }

    params = {
        "fullText": search_term,
        "pageNow": "1",
        "pageSize": "10",
        "sortType": "0",
        "sortKey": "showTime",
        "sortFlag": "-1",
        "highlighter": "2",
        "keywordNavigation": "1",
        "checkSearch": "1",
        "keyType": "fullText",
        "searchType": "0",
    }

    print("Testing AJAX endpoint…")
    with httpx.Client(timeout=30.0) as client:
        # Get session cookie from the search JSP page
        print(f"  GET {SEARCH_FORM_ACTION}")
        r0 = client.get(SEARCH_FORM_ACTION, headers={
            "User-Agent": headers["User-Agent"],
        })
        print(f"  Session cookie: {dict(r0.cookies)}")

        # Make the AJAX search call
        body = urlencode(params)
        print(f"  POST {AJAX_ENDPOINT}")
        r = client.post(AJAX_ENDPOINT, content=body, headers=headers, cookies=r0.cookies)

        if r.status_code != 200:
            print(f"  ❌ HTTP {r.status_code}")
            return {"error": f"HTTP {r.status_code}", "body": r.text[:500]}

        data = r.json()
        total = data.get("num", 0)
        articles = data.get("array", [])
        max_page = data.get("maxPage", 0)
        page_count = data.get("pageCount", 0)

        print(f"  ✅ Success!")
        print(f"  Total results:  {total}")
        print(f"  Max pages:      {max_page}")
        print(f"  Page count:     {page_count}")
        print(f"  Per page:       {len(articles)}")

        # Check article fields
        article_fields = list(articles[0].keys()) if articles else []
        print(f"  Article fields: {article_fields}")

        # Show samples
        print(f"\n  Sample articles:")
        for i, a in enumerate(articles[:5]):
            name = a.get("name", "").replace("<font color='red'>", "").replace("</font>", "")
            print(f"  {i+1}. [{a.get('showTime','')}] {name[:100]}")
            print(f"     {a.get('url','')}")

        # Verify domains are all www.sasac.gov.cn
        from urllib.parse import urlparse
        domains = set()
        for a in articles:
            domains.add(urlparse(a.get("url", "")).netloc)
        print(f"\n  Domains in results: {domains}")

        # Test page 2
        params["pageNow"] = "2"
        body2 = urlencode(params)
        r2 = client.post(AJAX_ENDPOINT, content=body2, headers=headers, cookies=r0.cookies)
        if r2.status_code == 200:
            d2 = r2.json()
            print(f"\n  Page 2: {len(d2.get('array', []))} articles")
            if d2.get("array"):
                a2 = d2["array"][0]
                name2 = a2.get("name", "").replace("<font color='red'>", "").replace("</font>", "")
                print(f"  First: [{a2.get('showTime','')}] {name2[:100]}")

        findings = {
            "ajax_endpoint": AJAX_ENDPOINT,
            "ajax_method": "POST",
            "ajax_params": params,
            "ajax_headers": headers,
            "total_results": total,
            "max_page": max_page,
            "page_size": 10,
            "article_fields": article_fields,
            "domains": list(domains),
            "selectors": SELECTORS,
            "search_term": search_term,
            "pagination": "change pageNow parameter in POST body",
        }

        return findings


def main():
    print("=" * 60)
    print("SASAC Search Discovery — Step 2")
    print("=" * 60)
    print()

    # ── AJAX endpoint test (primary discovery) ──
    findings = test_ajax_endpoint("调研")

    # ── Summary ──
    print()
    print("=" * 60)
    print("ACTIONABLE SUMMARY FOR main.py")
    print("=" * 60)
    print(f"""
  AJAX endpoint:  POST {findings['ajax_endpoint']}
  Total results:  {findings['total_results']}
  Max pages:      {findings['max_page']}
  Article fields: {findings['article_fields']}
  Domains:        {findings['domains']}

  Pagination:     Add &pageNow=N to POST body
  Page size:      Can set &pageSize=N (up to at least 20)

  CSS selectors (if using Playwright fallback):
    Container:    {SELECTORS['result_container']}
    Item:         {SELECTORS['result_item']}
    Title:        {SELECTORS['result_title']}
    Snippet:      {SELECTORS['result_snippet']}
    Date:         {SELECTORS['result_date']}
    Pagination:   {SELECTORS['pagination']}

  Approach for main.py:
    1. Use httpx to POST to AJAX endpoint, paginating pageNow 1..maxPage
    2. Each response gives JSON with article name, url, showTime, summaries
    3. Fetch full article text from each url (httpx GET + BeautifulSoup)
    4. Classify each article against the 10 military industry firm names
""")

    # Save JSON
    with open("discover_findings.json", "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=2)
    print("  📄 Full findings saved to discover_findings.json")


if __name__ == "__main__":
    main()
