"""Smart replacement URL discovery engine.

Pipeline for each broken URL:
1. Check if URL redirects to a live page (fastest)
2. Fetch Wayback archived version → extract distinctive phrase → Google exact-phrase search
3. Google search for page title/URL (fallback)
4. Load candidate pages, compare content, rate match
5. Optional: Gemini API for content similarity scoring (ambiguous cases only)

Uses only `requests` (no Camoufox, no residential proxy) to save bandwidth.
"""

import logging
import os
import re
from urllib.parse import urlparse, quote_plus

import requests
from dotenv import load_dotenv
from lxml import html as lxml_html
from tavily import TavilyClient

load_dotenv()
log = logging.getLogger(__name__)

TAVILY_KEY = os.environ.get("TAVILY_KEY", "")
GEMINI_API_KEY = os.environ.get("GOOGLE_GEMENI_CONTENT_CREATOR", "")

# ── Token/cost tracking ──────────────────────────────────────────────

_usage_stats = {
    "tavily_searches": 0,
    "gemini_calls": 0,
    "gemini_input_tokens": 0,
    "gemini_output_tokens": 0,
}


def get_usage_stats() -> dict:
    """Return current session usage stats and reset counters."""
    stats = dict(_usage_stats)
    return stats


def reset_usage_stats() -> None:
    """Reset usage counters for a new session."""
    for key in _usage_stats:
        _usage_stats[key] = 0

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"
_HEADERS = {"User-Agent": _UA}


# ── Wayback content extraction ───────────────────────────────────────


def fetch_wayback_content(url: str) -> dict | None:
    """Fetch the most recent Wayback Machine snapshot and extract text content.

    Returns {"snapshot_url": str, "text": str, "title": str} or None.
    """
    api_url = f"https://archive.org/wayback/available?url={url}"
    try:
        resp = requests.get(api_url, timeout=10, headers=_HEADERS)
        if resp.status_code != 200:
            return None
        data = resp.json()
        snapshots = data.get("archived_snapshots", {})
        closest = snapshots.get("closest")
        if not closest or not closest.get("available"):
            return None

        snapshot_url = closest["url"]

        # Fetch the actual archived page
        page_resp = requests.get(snapshot_url, timeout=15, headers=_HEADERS)
        if page_resp.status_code != 200:
            return None

        text, title = _extract_text_from_html(page_resp.text)
        return {
            "snapshot_url": snapshot_url,
            "text": text,
            "title": title,
        }
    except Exception as exc:
        log.warning("Wayback fetch failed for %s: %s", url, exc)
        return None


def _extract_text_from_html(html_content: str) -> tuple[str, str]:
    """Extract body text and title from HTML."""
    try:
        doc = lxml_html.fromstring(html_content)
    except Exception:
        return "", ""

    # Title
    title_el = doc.find(".//title")
    title = title_el.text_content().strip() if title_el is not None else ""

    # Remove script, style, nav, footer, header elements
    for tag in doc.xpath("//script | //style | //nav | //footer | //header | //aside"):
        tag.getparent().remove(tag)

    # Get text from body
    body = doc.find(".//body")
    if body is None:
        return "", title

    text = body.text_content()
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text, title


# ── Distinctive phrase extraction ────────────────────────────────────


_STOP_WORDS = frozenset([
    "home", "about", "contact", "privacy", "policy", "terms", "service",
    "menu", "search", "login", "register", "sign", "subscribe", "click",
    "copyright", "rights", "reserved", "cookie", "cookies",
    "inicio", "contacto", "privacidad", "términos", "buscar", "menú",
])


def extract_distinctive_phrase(text: str, min_words: int = 6, max_words: int = 12) -> str | None:
    """Extract a distinctive phrase from page content for exact-match Google search.

    Picks a phrase that is specific enough for search.
    Avoids generic navigation text, headers, footers.
    """
    if not text or len(text.split()) < min_words:
        return None

    # Split into sentences
    sentences = re.split(r'[.!?]\s+', text)

    # Filter: skip short sentences, sentences with mostly stop words
    candidates = []
    for sentence in sentences:
        words = sentence.split()
        if len(words) < min_words:
            continue
        # Check stop word ratio
        lower_words = [w.lower() for w in words]
        stop_count = sum(1 for w in lower_words if w in _STOP_WORDS)
        if stop_count / len(words) > 0.4:
            continue
        candidates.append(words)

    if not candidates:
        return None

    # Pick the first good candidate, trim to max_words
    best = candidates[0]
    phrase_words = best[:max_words]
    if len(phrase_words) < min_words:
        return None

    return " ".join(phrase_words)


# ── Web Search (Tavily) ──────────────────────────────────────────────


def google_search(query: str, num_results: int = 10) -> list[dict]:
    """Execute a web search via Tavily API.

    Returns list of {"title": str, "link": str, "snippet": str}.
    Name kept as google_search for interface compatibility.
    """
    if not TAVILY_KEY:
        log.warning("Tavily API not configured (missing TAVILY_KEY)")
        return []

    try:
        client = TavilyClient(api_key=TAVILY_KEY)
        response = client.search(
            query=query,
            max_results=min(num_results, 10),
            search_depth="advanced",
        )
        _usage_stats["tavily_searches"] += 1
        results = response.get("results", [])
        return [
            {"title": r.get("title", ""), "link": r.get("url", ""), "snippet": r.get("content", "")}
            for r in results
        ]
    except Exception as exc:
        log.warning("Tavily search failed for '%s': %s", query, exc)
        return []


# ── Candidate page loading ───────────────────────────────────────────


def fetch_candidate_page(url: str, timeout: int = 10) -> dict | None:
    """Fetch a candidate replacement page via plain requests (no proxy).

    Returns {"url": str, "text": str, "title": str, "status": int} or None.
    """
    try:
        resp = requests.get(url, timeout=timeout, headers=_HEADERS)
        text, title = _extract_text_from_html(resp.text)
        return {
            "url": resp.url,
            "text": text,
            "title": title,
            "status": resp.status_code,
        }
    except Exception as exc:
        log.debug("Failed to fetch candidate %s: %s", url, exc)
        return None


# ── Similarity scoring ───────────────────────────────────────────────


def compute_similarity(text_a: str, text_b: str) -> float:
    """Quick heuristic similarity using Jaccard word overlap.

    Returns float 0.0-1.0.
    """
    if not text_a or not text_b:
        return 0.0

    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())

    if not words_a or not words_b:
        return 0.0

    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def score_with_gemini(
    original_text: str,
    candidate_text: str,
    original_url: str,
    candidate_url: str,
) -> dict:
    """Use Gemini API for content similarity scoring.

    Only called for ambiguous cases (similarity 0.5-0.7).
    Returns {"score": float, "reasoning": str, "is_same_content": bool}.
    """
    if not GEMINI_API_KEY:
        return {"score": 0.0, "reasoning": "Gemini API not configured", "is_same_content": False}

    # Truncate to avoid excessive token usage
    orig_truncated = original_text[:2000]
    cand_truncated = candidate_text[:2000]

    prompt = (
        "You are evaluating whether two web pages contain the same content "
        "(i.e., one is a migrated/moved version of the other).\n\n"
        f"Original URL: {original_url}\n"
        f"Candidate URL: {candidate_url}\n\n"
        f"Original page text (truncated):\n{orig_truncated}\n\n"
        f"Candidate page text (truncated):\n{cand_truncated}\n\n"
        "Respond with ONLY a JSON object:\n"
        '{"score": 0.0-1.0, "reasoning": "brief explanation", "is_same_content": true/false}\n'
        "Score 1.0 = identical content, 0.0 = completely unrelated."
    )

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        _usage_stats["gemini_calls"] += 1
        # Track token usage from response metadata
        usage_meta = data.get("usageMetadata", {})
        _usage_stats["gemini_input_tokens"] += usage_meta.get("promptTokenCount", 0)
        _usage_stats["gemini_output_tokens"] += usage_meta.get("candidatesTokenCount", 0)
        text_response = data["candidates"][0]["content"]["parts"][0]["text"]

        # Parse JSON from response
        import json
        # Handle potential markdown wrapping
        text_response = text_response.strip()
        if text_response.startswith("```"):
            text_response = re.sub(r'^```(?:json)?\s*', '', text_response)
            text_response = re.sub(r'\s*```$', '', text_response)
        result = json.loads(text_response)
        return {
            "score": float(result.get("score", 0.0)),
            "reasoning": result.get("reasoning", ""),
            "is_same_content": bool(result.get("is_same_content", False)),
        }
    except Exception as exc:
        log.warning("Gemini scoring failed: %s", exc)
        return {"score": 0.0, "reasoning": f"API error: {exc}", "is_same_content": False}


# ── Main pipeline ────────────────────────────────────────────────────


def find_live_replacement(url: str, page_title: str | None = None) -> dict | None:
    """Full pipeline to find a live replacement for a broken URL.

    Strategy order:
    1. Check redirect (fastest)
    2. Wayback phrase search: archived content → extract phrase → Google search
    3. Google title/URL search (fallback)

    Returns: {
        "replacement_url": str,
        "source": "redirect" | "google_phrase_match" | "google_title_search",
        "similarity_score": float,
        "wayback_snapshot_url": str | None,
        "search_query": str | None,
    } or None.
    """
    original_domain = urlparse(url).netloc

    # Strategy 1: Redirect check (try HEAD then GET)
    try:
        head_resp = requests.head(url, allow_redirects=True, timeout=10, headers=_HEADERS)
        if head_resp.history and head_resp.status_code == 200:
            final_url = str(head_resp.url)
            if final_url != url:
                return {
                    "replacement_url": final_url,
                    "source": "redirect",
                    "similarity_score": 1.0,
                    "wayback_snapshot_url": None,
                    "search_query": None,
                }
        # Some servers block HEAD — try GET if HEAD returned 4xx/5xx but had redirects
        if head_resp.history and head_resp.status_code >= 400:
            get_resp = requests.get(url, allow_redirects=True, timeout=10, headers=_HEADERS)
            if get_resp.history and get_resp.status_code == 200:
                final_url = str(get_resp.url)
                if final_url != url:
                    return {
                        "replacement_url": final_url,
                        "source": "redirect",
                        "similarity_score": 1.0,
                        "wayback_snapshot_url": None,
                        "search_query": None,
                    }
    except Exception:
        pass  # URL is dead, proceed to next strategy

    # Strategy 2: Wayback phrase search
    wayback = fetch_wayback_content(url)
    wayback_text = ""
    wayback_snapshot_url = None

    if wayback:
        wayback_text = wayback["text"]
        wayback_snapshot_url = wayback["snapshot_url"]
        phrase = extract_distinctive_phrase(wayback_text)

        if phrase:
            query = f'"{phrase}"'
            results = google_search(query, num_results=10)
            # Filter out archive.org results and the original dead domain
            results = [
                r for r in results
                if "web.archive.org" not in r["link"]
                and original_domain not in r["link"]
            ]

            best = _score_candidates(results, wayback_text, url)
            if best:
                return {
                    "replacement_url": best["url"],
                    "source": "google_phrase_match",
                    "similarity_score": best["score"],
                    "wayback_snapshot_url": wayback_snapshot_url,
                    "search_query": query,
                }

    # Strategy 3: Title search — only if Wayback had content (so we have reference
    # text to compare against). Blind title searches without reference text waste
    # Tavily credits with near-zero hit rate.
    if not wayback_text:
        return None

    search_terms = []
    if wayback and wayback.get("title"):
        title = wayback["title"]
        title = re.sub(r'Wayback Machine', '', title).strip()
        if title:
            search_terms.append(title)
    if page_title and page_title not in search_terms:
        search_terms.append(page_title)

    for term in search_terms[:1]:  # Max 1 title search to conserve credits
        results = google_search(term, num_results=10)
        results = [
            r for r in results
            if "web.archive.org" not in r["link"]
            and original_domain not in r["link"]
        ]

        best = _score_candidates(results, wayback_text, url)
        if best:
            return {
                "replacement_url": best["url"],
                "source": "google_title_search",
                "similarity_score": best["score"],
                "wayback_snapshot_url": wayback_snapshot_url,
                "search_query": term,
            }

    return None


def _score_candidates(
    search_results: list[dict],
    reference_text: str,
    original_url: str,
) -> dict | None:
    """Load and score candidate pages against reference text.

    Returns {"url": str, "score": float} for the best match above threshold, or None.
    """
    from dev.link_validator import is_same_org

    best = None
    best_score = 0.0
    threshold = 0.3  # minimum to even consider

    for result in search_results[:5]:  # Only check top 5 to save time
        candidate = fetch_candidate_page(result["link"])
        if not candidate or candidate["status"] != 200:
            continue

        score = compute_similarity(reference_text, candidate["text"])

        # Boost score for same-org matches
        if is_same_org(original_url, result["link"]):
            score = min(1.0, score + 0.15)

        if score > best_score and score >= threshold:
            best_score = score
            best = {"url": result["link"], "score": score}

    # For ambiguous cases, optionally use Gemini
    if best and 0.5 <= best_score <= 0.7 and reference_text:
        candidate = fetch_candidate_page(best["url"])
        if candidate:
            gemini_result = score_with_gemini(
                reference_text, candidate["text"], original_url, best["url"]
            )
            if gemini_result["is_same_content"]:
                best["score"] = max(best["score"], gemini_result["score"])

    # Only return if we have reasonable confidence
    if best and best["score"] >= 0.4:
        return best
    return None
