"""
kiwix_opds.py : live Kiwix OPDS catalog access.

Two responsibilities:

1. hydrate_catalog(catalog) : refresh URLs, sizes, dates on our curated
   40-ZIM list using the live OPDS feed. Fails silent: if Kiwix is
   unreachable we return the baked-in catalog unchanged.

2. browse(query, page, page_size) : paginated, search-filtered access
   to the full Kiwix library (~3500+ entries). Powers the "Browse all
   Kiwix" tab on the admin page.

Both functions use a 5-minute in-process cache so repeated calls during
a session don't hammer Kiwix.

OPDS reference:
    https://library.kiwix.org/catalog/v2/entries?count=N&start=M
    https://library.kiwix.org/catalog/v2/entries?q=<search>
"""
from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

KIWIX_OPDS_BASE = "https://library.kiwix.org/catalog/v2/entries"
HTTP_TIMEOUT = 8  # seconds; users are waiting on this synchronously
CACHE_TTL = 300   # 5 minutes

# XML namespaces used by Kiwix OPDS feed
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/terms/",
    "opds": "https://specs.opds.io/opds-1.2",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

# In-process cache: {cache_key: (timestamp, value)}
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str):
    hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: Any) -> None:
    _cache[key] = (time.time(), val)


def _http_get(url: str, timeout: int = HTTP_TIMEOUT) -> bytes:
    """GET helper. Raises on any error."""
    req = urllib.request.Request(url, headers={"User-Agent": "Apocalypse/1.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_entry(entry_el: ET.Element) -> dict[str, Any]:
    """Convert an Atom <entry> element into a flat dict."""
    def _text(tag: str, ns: str = "atom") -> str:
        el = entry_el.find(f"{{{_NS[ns]}}}{tag}")
        return el.text.strip() if el is not None and el.text else ""

    # Find the open-access acquisition link (the actual ZIM download)
    download_url = ""
    size_bytes = 0
    for link in entry_el.findall(f"{{{_NS['atom']}}}link"):
        if link.get("rel") == "http://opds-spec.org/acquisition/open-access":
            href = link.get("href", "")
            # Kiwix gives us .zim.meta4; strip to get direct .zim
            if href.endswith(".meta4"):
                href = href[:-len(".meta4")]
            download_url = href
            try:
                size_bytes = int(link.get("length", "0"))
            except ValueError:
                size_bytes = 0
            break

    # Thumbnail
    thumbnail = ""
    for link in entry_el.findall(f"{{{_NS['atom']}}}link"):
        if link.get("rel") == "http://opds-spec.org/image/thumbnail":
            href = link.get("href", "")
            # Make absolute
            if href.startswith("/"):
                href = "https://library.kiwix.org" + href
            thumbnail = href
            break

    # Article count
    try:
        article_count = int(_text("articleCount"))
    except ValueError:
        article_count = 0

    return {
        "uuid": _text("id").replace("urn:uuid:", ""),
        "title": _text("title"),
        "summary": _text("summary"),
        "language": _text("language"),
        "name": _text("name"),
        "flavour": _text("flavour"),
        "category": _text("category"),
        "tags": _text("tags"),
        "article_count": article_count,
        "issued": _text("issued", ns="dc"),
        "download_url": download_url,
        "size_bytes": size_bytes,
        "thumbnail": thumbnail,
    }


def fetch_opds_page(start: int = 0, count: int = 100, query: str = "") -> dict[str, Any]:
    """
    Fetch one page of the Kiwix OPDS feed.

    Returns:
        {
            "total": int,            # total matching entries on Kiwix
            "start": int,            # this page's start offset
            "count": int,            # entries returned
            "entries": [ {...}, ... ]
        }

    Raises urllib/socket errors on network failure. Caller is responsible
    for catching them.
    """
    params = {"count": count, "start": start}
    if query:
        params["q"] = query

    url = f"{KIWIX_OPDS_BASE}?{urllib.parse.urlencode(params)}"
    cache_key = f"opds:{url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    body = _http_get(url)
    root = ET.fromstring(body)

    total = 0
    # Kiwix puts totalResults in the default atom namespace, not opensearch
    for ns_key in ("atom", "opensearch"):
        total_el = root.find(f"{{{_NS[ns_key]}}}totalResults")
        if total_el is not None and total_el.text:
            try:
                total = int(total_el.text)
                break
            except ValueError:
                pass

    entries = [_parse_entry(e) for e in root.findall(f"{{{_NS['atom']}}}entry")]

    result = {"total": total, "start": start, "count": len(entries), "entries": entries}
    _cache_set(cache_key, result)
    return result


def _fetch_full_library() -> list[dict[str, Any]]:
    """
    Fetch the entire Kiwix library (paginated under the hood).
    Used by the hydrator so we can build a name -> entry index.

    Cached for CACHE_TTL.
    """
    cache_key = "opds:full"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # 2000 covers current size (~3500 entries) in 2 pages; keeps requests low.
    all_entries: list[dict[str, Any]] = []
    page_size = 2000
    start = 0
    while True:
        page = fetch_opds_page(start=start, count=page_size)
        all_entries.extend(page["entries"])
        if start + len(page["entries"]) >= page["total"]:
            break
        start += len(page["entries"])
        if len(all_entries) > 10000:  # safety brake
            break

    _cache_set(cache_key, all_entries)
    return all_entries


def hydrate_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """
    Refresh URLs, sizes, dates, article counts on the curated catalog
    using the live OPDS feed.

    Matches by (kiwix_name, kiwix_flavour). If the live feed has multiple
    versions of the same name+flavour, picks the one with the most recent
    `issued` date.

    Returns a *new* catalog dict with hydrated items. Adds a top-level
    `hydrated_at` timestamp on success.

    On any network failure, returns the input catalog unchanged plus a
    `hydration_error` field describing what went wrong, so the wizard can
    surface "you're seeing potentially-stale data" if it wants to.
    """
    out = dict(catalog)
    out["items"] = [dict(item) for item in catalog.get("items", [])]

    try:
        live_entries = _fetch_full_library()
    except Exception as e:
        out["hydration_error"] = f"{type(e).__name__}: {e}"
        return out

    # Build index: (name, flavour) -> entry. Keep the most recently issued.
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in live_entries:
        key = (entry["name"], entry["flavour"])
        existing = index.get(key)
        if existing is None or entry["issued"] > existing["issued"]:
            index[key] = entry

    hydrated_count = 0
    missing: list[str] = []

    for item in out["items"]:
        key = (item.get("kiwix_name", ""), item.get("kiwix_flavour", ""))
        live = index.get(key)
        if live is None:
            missing.append(f"{key[0]}/{key[1] or '<no-flavour>'}")
            continue
        if live["download_url"]:
            item["download_url"] = live["download_url"]
        if live["size_bytes"] > 0:
            item["size_bytes"] = live["size_bytes"]
        if live["article_count"] > 0:
            item["article_count"] = live["article_count"]
        if live["issued"]:
            item["issued"] = live["issued"]
        if live["thumbnail"]:
            item["thumbnail"] = live["thumbnail"]
        hydrated_count += 1

    out["hydrated_at"] = int(time.time())
    out["hydrated_count"] = hydrated_count
    if missing:
        out["hydration_missing"] = missing
    return out


def browse(query: str = "", page: int = 1, page_size: int = 30) -> dict[str, Any]:
    """
    Search-filtered, paginated view of the full Kiwix library.

    Returns:
        {
            "total": int,         # total matching entries
            "page": int,          # 1-indexed
            "page_size": int,
            "total_pages": int,
            "entries": [ {...}, ... ],
            "error": str | None,  # set if Kiwix was unreachable
        }
    """
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 30

    start = (page - 1) * page_size

    try:
        result = fetch_opds_page(start=start, count=page_size, query=query)
    except Exception as e:
        return {
            "total": 0,
            "page": page,
            "page_size": page_size,
            "total_pages": 0,
            "entries": [],
            "error": f"{type(e).__name__}: {e}",
        }

    total = result["total"]
    total_pages = (total + page_size - 1) // page_size if total else 0

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "entries": result["entries"],
        "error": None,
    }


if __name__ == "__main__":
    # Smoke test
    import json
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "browse"
    if cmd == "browse":
        q = sys.argv[2] if len(sys.argv) > 2 else ""
        print(json.dumps(browse(query=q, page=1, page_size=5), indent=2))
    elif cmd == "hydrate":
        cat_path = sys.argv[2] if len(sys.argv) > 2 else "data/catalog.json"
        catalog = json.load(open(cat_path))
        hydrated = hydrate_catalog(catalog)
        print(f"hydrated {hydrated.get('hydrated_count')} of {len(catalog['items'])}")
        if hydrated.get("hydration_missing"):
            print(f"missing: {hydrated['hydration_missing']}")
        if hydrated.get("hydration_error"):
            print(f"error: {hydrated['hydration_error']}")
    else:
        print(f"unknown cmd: {cmd}")
