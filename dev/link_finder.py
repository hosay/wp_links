"""Broken link discovery for es.wikipedia.org.

Two discovery channels:
1. Wikipedia's own broken-link reports (scraped via Camoufox)
2. SemRush backlink audit via seopack.org (separate module)

This module also provides helpers for parsing wikitext markers
like {{enlace roto}} that flag known dead links.
"""

import logging
import os
import random
import re
import time

from lxml import html as lxml_html

log = logging.getLogger(__name__)

# Wikipedia special pages for broken links (Spanish Wikipedia)
DEAD_LINKS_CATEGORY = "https://es.wikipedia.org/wiki/Categoría:Wikipedia:Artículos_con_enlaces_externos_rotos"
LINKSEARCH_BASE = "https://es.wikipedia.org/wiki/Especial:BuscarEnlaces"


def _human_delay(min_s: float = 2.0, max_s: float = 5.0):
    time.sleep(random.uniform(min_s, max_s))


# ── HTML parsing ──────────────────────────────────────────────────────


def parse_dead_links_report(html_content: str) -> list[dict]:
    """Parse a Wikipedia broken-links report page and extract page/URL pairs.

    Returns list of dicts: [{"wiki_title": ..., "broken_url": ...}, ...]
    """
    results = []
    try:
        doc = lxml_html.fromstring(html_content)
    except Exception:
        return results

    # Pattern: <li> containing a wiki link and an external link
    for li in doc.xpath("//li"):
        wiki_links = li.xpath('.//a[starts-with(@href, "/wiki/")]')
        ext_links = li.xpath('.//a[contains(@class, "external") or starts-with(@href, "http")]')

        if wiki_links and ext_links:
            title_el = wiki_links[0]
            title = title_el.get("title") or title_el.text_content().strip()
            # Skip special pages
            if title.startswith("Especial:") or title.startswith("Wikipedia:"):
                continue

            for ext in ext_links:
                href = ext.get("href", "")
                if href.startswith("http"):
                    results.append({
                        "wiki_title": title,
                        "broken_url": href,
                    })

    return results


# ── wikitext parsing ──────────────────────────────────────────────────


_ENLACE_ROTO_RE = re.compile(
    r'\{\{enlace roto(?:\s*\|[^}]*)?\}\}',
    re.IGNORECASE,
)

_URL_IN_TEMPLATE_RE = re.compile(
    r'\|url\s*=\s*(https?://[^\s\|\}]+)',
    re.IGNORECASE,
)


def extract_broken_urls_from_wikitext(wikitext: str) -> list[str]:
    """Extract URLs flagged with {{enlace roto}} templates.

    These are links that Wikipedia editors have already marked as dead.
    """
    broken = []
    lines = wikitext.split("\n")

    for i, line in enumerate(lines):
        if not _ENLACE_ROTO_RE.search(line):
            continue

        # Check for url= param inside the enlace roto template
        for match in _ENLACE_ROTO_RE.finditer(line):
            template_text = match.group(0)
            url_match = _URL_IN_TEMPLATE_RE.search(template_text)
            if url_match:
                broken.append(url_match.group(1))
                continue

        # Also grab any URL on the same line (from external link or cite template)
        urls_on_line = re.findall(r'https?://[^\s\]\|\}<>"]+', line)
        for url in urls_on_line:
            if url not in broken:
                broken.append(url)

    return broken


# ── browser-based discovery ───────────────────────────────────────────


def fetch_dead_links_category(page, max_pages: int = 3) -> list[dict]:
    """Scrape the 'articles with broken external links' category.

    Args:
        page: Playwright page object (already logged in).
        max_pages: Max number of category pages to scrape.

    Returns:
        List of {"wiki_title": ..., "broken_url": ...} dicts.
    """
    results = []
    url = DEAD_LINKS_CATEGORY
    pages_scraped = 0

    while url and pages_scraped < max_pages:
        log.info("Fetching dead links category page %d: %s", pages_scraped + 1, url)
        page.goto(url, wait_until="load")
        _human_delay()

        # Extract article titles from the category listing
        content = page.content()
        doc = lxml_html.fromstring(content)

        # Category pages list articles in #mw-pages div
        article_links = doc.xpath('//div[@id="mw-pages"]//a[starts-with(@href, "/wiki/")]')
        for link in article_links:
            title = link.get("title") or link.text_content().strip()
            if title and not title.startswith(("Wikipedia:", "Especial:", "Categoría:")):
                results.append({
                    "wiki_title": title,
                    "broken_url": "",  # Will be filled when we check the article
                })

        # Find "next page" link
        next_link = doc.xpath('//a[contains(text(), "página siguiente")]/@href')
        if next_link:
            url = "https://es.wikipedia.org" + next_link[0]
        else:
            url = None

        pages_scraped += 1
        _human_delay(3, 7)

    log.info("Found %d articles with potential broken links", len(results))
    return results


def find_broken_links_in_article(page, title: str) -> list[str]:
    """Fetch an article's wikitext and return any URLs marked as broken."""
    from dev.wiki_browser import get_wikitext
    wikitext = get_wikitext(page, title)
    return extract_broken_urls_from_wikitext(wikitext)


def fetch_semrush_broken_links(page, domain: str = "es.wikipedia.org") -> list[dict]:
    """Use SemRush (via seopack.org) to find broken backlinks.

    This navigates SemRush's Backlink Audit tool to find dead links.
    Returns list of {"wiki_title": ..., "broken_url": ...} dicts.

    NOTE: This requires the page to already be on the SemRush tool
    (opened via seopack.org Access SemRush button).
    """
    results = []

    # Navigate to Backlink Analytics > Backlinks
    log.info("Navigating to SemRush Backlink Analytics for %s", domain)

    # Go to backlink analytics
    backlinks_link = page.query_selector('a[href*="backlinks"]')
    if backlinks_link:
        backlinks_link.click()
        page.wait_for_load_state("load")
        _human_delay()

    # Enter domain in search
    search_input = page.query_selector('input[name="q"], input[placeholder*="domain"], input[type="text"]')
    if search_input:
        search_input.click()
        page.keyboard.press("Control+A")
        _human_delay(0.3, 0.5)
        page.type('input[type="text"]', domain,
                  delay=random.uniform(50, 120))
        _human_delay(0.5, 1.0)
        page.keyboard.press("Enter")
        page.wait_for_load_state("load")
        _human_delay(3, 6)

    # This is exploratory — the actual SemRush UI may differ
    # Log what we find for manual refinement
    content = page.content()
    log.info("SemRush page length: %d chars", len(content))

    return results
