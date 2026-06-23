"""Experiment: how many outlets we actually care about have a discoverable RSS/Atom
feed or sitemap, before designing elephant/fetch.py around that assumption.

Strategy per domain:
1. Try a handful of common feed paths directly (/feed, /rss, /rss.xml, ...).
2. Fall back to parsing the homepage HTML for <link rel="alternate" type="...rss/atom...">.
3. Note whether a robots.txt sitemap is advertised, as a secondary signal.

No LLM calls — this is plain HTTP, fast and free. Output tells us what fraction of a
realistic outlet mix (international wire, state media x2, tech, science, regional) is
fetchable directly vs. needing the scrape-then-LLM or Perplexity fallback.

    uv run python scripts/experiments/probe_feed_discovery.py
"""

from __future__ import annotations

import asyncio
import re

import httpx
from _harness import save_json

CANDIDATE_DOMAINS = [
    "bbc.com",
    "reuters.com",
    "tass.com",  # Russian state media — flagged in research.py as Perplexity gap
    "rt.com",  # Russian state media
    "aljazeera.com",
    "techcrunch.com",
    "habr.com",  # Russian-language tech, mentioned by the user as a known-good source
    "pubmed.ncbi.nlm.nih.gov",
    "theguardian.com",
    "arstechnica.com",
]

_COMMON_FEED_PATHS = [
    "/feed",
    "/feed/",
    "/rss",
    "/rss.xml",
    "/rss/all/all/",
    "/feeds/posts/default",
]
_LINK_RE = re.compile(
    r'<link[^>]+type=["\'](?:application/(?:rss|atom)\+xml)["\'][^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


async def _try_common_paths(client: httpx.AsyncClient, domain: str) -> str | None:
    for path in _COMMON_FEED_PATHS:
        url = f"https://{domain}{path}"
        try:
            resp = await client.get(url, timeout=8, follow_redirects=True)
            if resp.status_code == 200 and (
                "xml" in resp.headers.get("content-type", "") or "<rss" in resp.text[:500].lower()
            ):
                return url
        except Exception:  # noqa: BLE001 — probing, expected to fail often
            continue
    return None


async def _try_homepage_link_tag(client: httpx.AsyncClient, domain: str) -> str | None:
    try:
        resp = await client.get(f"https://{domain}", timeout=8, follow_redirects=True)
        match = _LINK_RE.search(resp.text)
        if match:
            href = match.group(1)
            return href if href.startswith("http") else f"https://{domain}{href}"
    except Exception:  # noqa: BLE001
        pass
    return None


async def _probe_domain(domain: str) -> dict[str, str | bool | None]:
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (research probe)"}) as client:
        feed_url = await _try_common_paths(client, domain)
        method = "common_path" if feed_url else None
        if not feed_url:
            feed_url = await _try_homepage_link_tag(client, domain)
            method = "homepage_link_tag" if feed_url else None
        try:
            robots = await client.get(
                f"https://{domain}/robots.txt", timeout=8, follow_redirects=True
            )
            has_sitemap = "sitemap" in robots.text.lower()
        except Exception:  # noqa: BLE001
            has_sitemap = False
    return {
        "domain": domain,
        "feed_found": feed_url is not None,
        "feed_url": feed_url,
        "discovery_method": method,
        "sitemap_advertised": has_sitemap,
    }


async def main() -> None:
    results = await asyncio.gather(*(_probe_domain(d) for d in CANDIDATE_DOMAINS))

    print(f"\n{'domain':<28} {'feed?':<7} {'method':<20} {'sitemap?':<10} feed_url")
    print("-" * 110)
    found = 0
    for r in results:
        if r["feed_found"]:
            found += 1
        print(
            f"{r['domain']:<28} {'YES' if r['feed_found'] else 'no':<7} "
            f"{r['discovery_method'] or '-':<20} {'yes' if r['sitemap_advertised'] else 'no':<10} "
            f"{r['feed_url'] or ''}"
        )

    print(f"\n{found}/{len(CANDIDATE_DOMAINS)} domains had a discoverable feed.")

    path = save_json("feed_discovery", results)
    print(f"Full log saved to {path}")


if __name__ == "__main__":
    asyncio.run(main())
