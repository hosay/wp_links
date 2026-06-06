"""Standalone broken link discovery pipeline.

Runs independently of the edit cycle. Populates the broken_links table
so that the daily edit orchestrator always has fixable links ready.

Usage:
    python -m dev.discovery                  # default: 100 articles
    python -m dev.discovery --max-articles 50
"""

import logging
import os
import sys
import time
import random

from dotenv import load_dotenv

from dev.db import (
    init_db,
    add_page,
    add_broken_link,
    set_replacement_url,
    get_broken_links_needing_replacement,
    mark_link_searched,
)
from dev.link_finder import (
    fetch_category_members_api,
    fetch_wikitext_batch_api,
    extract_broken_urls_v2,
)
from dev.link_replacer import (
    find_live_replacement,
    get_usage_stats,
    reset_usage_stats,
    verify_replacement_live,
    verify_replacement_content,
)
from dev.link_validator import classify_confidence

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

DB_PATH = os.path.join(os.path.dirname(__file__), "wp_links.db")


def discover_broken_links(conn, max_articles: int = 100) -> dict:
    """Main discovery pipeline.

    1. Fetch category members via API (no browser needed)
    2. Batch-fetch wikitext via API
    3. Parse all template types for broken URLs
    4. Insert into broken_links with dedup
    5. For each new broken link, run find_live_replacement()
    6. Store results

    Returns summary stats dict.
    """
    stats = {
        "articles_checked": 0,
        "broken_urls_found": 0,
        "replacements_found": 0,
        "high_confidence": 0,
        "medium_confidence": 0,
    }

    # Step 1: Fetch category members
    log.info("Fetching category members (max %d articles)...", max_articles)
    all_titles = []
    cmcontinue = None
    while len(all_titles) < max_articles:
        batch_limit = min(500, max_articles - len(all_titles))
        titles, cmcontinue = fetch_category_members_api(limit=batch_limit, cmcontinue=cmcontinue)
        if not titles:
            break
        all_titles.extend(titles)
        if not cmcontinue:
            break

    log.info("Found %d articles in broken-links category", len(all_titles))

    # Shuffle to avoid always processing the same articles
    random.shuffle(all_titles)
    all_titles = all_titles[:max_articles]

    # Step 2: Batch-fetch wikitext
    log.info("Fetching wikitext for %d articles...", len(all_titles))
    wikitext_map = fetch_wikitext_batch_api(all_titles)
    stats["articles_checked"] = len(wikitext_map)

    # Step 3: Parse templates and insert broken links
    for title, wikitext in wikitext_map.items():
        broken = extract_broken_urls_v2(wikitext)
        if not broken:
            continue

        for item in broken:
            url = item["url"]
            template = item["template"]

            # Get or create page
            page_id = add_page(conn, wiki_title=title, found_via="discovery")
            bl_id = add_broken_link(
                conn, page_id=page_id, original_url=url,
                link_status=0, source="discovery",
                discovery_method=template,
            )
            stats["broken_urls_found"] += 1

    # Step 4: Find replacements for links that need them
    # Budget cap: max 20 Tavily searches per run to conserve credits
    MAX_SEARCH_BUDGET = 20
    needing = get_broken_links_needing_replacement(conn, limit=MAX_SEARCH_BUDGET)
    log.info("Processing %d new links (budget: %d searches max)...", len(needing), MAX_SEARCH_BUDGET)

    from dev.link_replacer import get_usage_stats as _get_current_usage

    for link in needing:
        # Check budget before each search
        current = _get_current_usage()
        if current["tavily_searches"] >= MAX_SEARCH_BUDGET:
            log.info("Tavily budget reached (%d searches) — stopping", MAX_SEARCH_BUDGET)
            break

        url = link["original_url"]
        title = link["wiki_title"]
        log.info("Searching replacement for: %s (article: %s)", url, title)

        result = find_live_replacement(url, page_title=title)

        if result:
            replacement_url = result["replacement_url"]

            # Tier 1: Verify replacement URL is actually live
            liveness = verify_replacement_live(replacement_url)
            if not liveness["alive"]:
                reason = "soft 404" if liveness["soft_404"] else "dead/tiny"
                log.info("  Replacement rejected (%s): %s", reason, replacement_url[:80])
                mark_link_searched(conn, link["id"], f"rejected:{reason}:{replacement_url[:80]}")
                continue

            # Tier 2: Gemini content relevance check
            content_check = verify_replacement_content(
                replacement_url=replacement_url,
                replacement_text=liveness.get("text", ""),
                article_title=title,
                original_url=url,
            )
            if not content_check["is_relevant"]:
                log.info("  Replacement rejected (irrelevant content): %s — %s",
                         replacement_url[:80], content_check.get("reasoning", "")[:80])
                mark_link_searched(conn, link["id"], f"rejected:irrelevant:{replacement_url[:80]}")
                continue

            confidence = classify_confidence(
                url, replacement_url, result["source"],
                similarity_score=result.get("similarity_score", 0.0),
            )

            # Update the broken_link record with all info
            conn.execute(
                "UPDATE broken_links SET replacement_url = ?, confidence = ?, "
                "source = ?, similarity_score = ?, wayback_snapshot_url = ?, "
                "search_query = ?, verified_at = datetime('now') WHERE id = ?",
                (
                    replacement_url,
                    confidence,
                    result["source"],
                    result.get("similarity_score", 0.0),
                    result.get("wayback_snapshot_url"),
                    result.get("search_query"),
                    link["id"],
                ),
            )
            conn.commit()

            stats["replacements_found"] += 1
            if confidence == "high":
                stats["high_confidence"] += 1
            elif confidence == "medium":
                stats["medium_confidence"] += 1

            log.info("  Found: %s (confidence=%s, source=%s)",
                     replacement_url, confidence, result["source"])
        else:
            # Mark as searched so we don't retry next run
            search_note = f"searched:{title}"
            mark_link_searched(conn, link["id"], search_note)
            log.info("  No replacement found (marked as searched)")

        # Brief pause between searches to be respectful to APIs
        time.sleep(random.uniform(1.0, 3.0))

    return stats


def run(max_articles: int = 100):
    """Entry point for discovery cron."""
    log.info("=== Starting link discovery (max %d articles) ===", max_articles)
    reset_usage_stats()
    conn = init_db(DB_PATH)
    stats = discover_broken_links(conn, max_articles=max_articles)
    usage = get_usage_stats()

    log.info("=== Discovery complete ===")
    log.info("Articles checked: %d", stats["articles_checked"])
    log.info("Broken URLs found: %d", stats["broken_urls_found"])
    log.info("Replacements found: %d", stats["replacements_found"])
    log.info("  High confidence: %d", stats["high_confidence"])
    log.info("  Medium confidence: %d", stats["medium_confidence"])
    log.info("API usage — Tavily: %d searches, Gemini: %d calls (%d/%d tokens)",
             usage["tavily_searches"], usage["gemini_calls"],
             usage["gemini_input_tokens"], usage["gemini_output_tokens"])

    conn.close()
    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Discover broken links on es.wikipedia.org")
    parser.add_argument("--max-articles", type=int, default=100)
    args = parser.parse_args()
    run(max_articles=args.max_articles)
