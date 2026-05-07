#!/usr/bin/env python3
"""
Kiwix-serve compatible shim for macOS+exFAT.

Replaces the broken kiwix-serve binary (which throws MMapException on exFAT/macOS)
with a thin Python HTTP server using libzim directly. Same endpoints the
Apocalypse.html UI expects:

  GET /search?books.name=<book>&pattern=<query>&pageLength=<n>
      -> JSON list of {path, title, snippet} hits

  GET /content/<book>/<path...>
      -> raw entry content with proper mimetype

  GET /                                  -> simple book index
  GET /viewer#<book>/<path>              -> redirects to /content/<book>/<path>

Run with:  python3 kiwix_shim.py [--port 8888] [--zim-dir <dir>]
"""

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from libzim.reader import Archive
    from libzim.search import Searcher, Query
    from libzim.suggestion import SuggestionSearcher
except ImportError as _e:
    import traceback
    print(f"ERROR: libzim not loadable: {_e}", file=sys.stderr)
    traceback.print_exc()
    sys.exit(1)


ZIM_DIR = None
ARCHIVES = {}


def load_archives(zim_dir):
    """Open every .zim in zim_dir and key by filename-without-extension."""
    archives = {}
    for fn in sorted(os.listdir(zim_dir)):
        if not fn.endswith('.zim'):
            continue
        path = os.path.join(zim_dir, fn)
        # Use full filename minus extension as the book key (matches kiwix convention)
        key = fn[:-4]
        try:
            archives[key] = Archive(path)
            print(f"  loaded: {key} ({archives[key].entry_count:,} entries)")
        except Exception as e:
            print(f"  FAILED: {key}: {e}", file=sys.stderr)
    return archives


def guess_mime(path):
    """Best-effort mimetype for a ZIM entry path."""
    if path.endswith('.html') or path.endswith('.htm') or '/' not in path:
        return 'text/html; charset=utf-8'
    mime, _ = mimetypes.guess_type(path)
    return mime or 'application/octet-stream'


# Patterns that signal "this article is a stub/disambig: the real content is elsewhere"
# Examples from Wikipedia ZIM:
#   "For the primary active ingredient of Tylenol, see Acetaminophen."
#   "For other uses, see Foo (disambiguation)."
#   "Main article: Acetaminophen"
#   "(Redirected from Tylenol)"
HATNOTE_PATTERNS = [
    # "For X, see <a href="link">Article</a>"  (most common)
    re.compile(r'For [^<.]{3,80}?,?\s*see\s*<a[^>]+href="([^"#]+)"[^>]*>([^<]+)</a>', re.IGNORECASE),
    # "Main article: <a href="link">Article</a>"
    re.compile(r'Main\s+article:\s*<a[^>]+href="([^"#]+)"[^>]*>([^<]+)</a>', re.IGNORECASE),
    # "See also: <a href="link">Article</a>"  (lower confidence, only if article is short)
    re.compile(r'See\s+also:\s*<a[^>]+href="([^"#]+)"[^>]*>([^<]+)</a>', re.IGNORECASE),
]

# Extract the first internal article link from the lede (fallback when no hatnote)
LEDE_LINK_PATTERN = re.compile(r'<p>.*?<a[^>]+href="([^"#]+)"[^>]*>([^<]+)</a>', re.IGNORECASE | re.DOTALL)


def resolve_exact_title(archive, query):
    """
    Try to resolve `query` to an exact Wikipedia article path. This is the
    PRIMARY search strategy for "what is X" / "who is Y" / single-noun queries.

    Wikipedia articles sit at predictable paths: "Detroit" the city is at
    `Detroit`, not `Detroit_station_(Detroit)`. Title-suggest will return the
    bus station first because it has more title-prefix matches, but the
    canonical city article is right there at the obvious path.

    We also drop one trailing word at a time so "tylenol work" -> "tylenol"
    catches the article when the user added a verb.

    Returns (path, title) or None.
    """
    if not query or not query.strip():
        return None
    words = query.strip().split()
    # Try the full query first, then progressively shorter prefixes
    for end in range(len(words), 0, -1):
        prefix = ' '.join(words[:end])
        # Wikipedia title formats to try, in order of likelihood
        underscored = prefix.replace(' ', '_')
        candidates = [
            underscored,                                  # exact, raw
            underscored.capitalize(),                     # First-letter cap
            '_'.join(w.capitalize() for w in words[:end]), # Title Case
            f"A/{underscored}",
            f"A/{underscored.capitalize()}",
            f"A/{'_'.join(w.capitalize() for w in words[:end])}",
        ]
        for cand in candidates:
            try:
                e = archive.get_entry_by_path(cand)
                if e.is_redirect:
                    e = e.get_redirect_entry()
                # Reject disambig pages, they're never what users want
                if '(disambiguation)' in e.path.lower() or '(disambiguation)' in (e.title or '').lower():
                    continue
                return (e.path, e.title or e.path)
            except Exception:
                continue
    return None


def is_disambig_or_stub_path(path):
    """Heuristic: paths that almost never contain the answer the user wants."""
    p = path.lower()
    return ('(disambiguation)' in p
            or p.endswith('_(disambiguation)')
            or p.endswith('_disambiguation')
            or '/list_of_' in p)


def title_quality_score(path):
    """
    Lower score = better. Canonical articles have short, unqualified titles.
    "Detroit" beats "Detroit_station_(Detroit)" beats "List_of_Detroit_things".
    """
    p = path.lstrip('A/')
    score = len(p)
    if '(' in p:
        score += 30   # qualifiers are usually less canonical
    if p.lower().startswith('list_of_'):
        score += 100  # almost never the answer
    if '_' not in p:
        score -= 5    # single-word titles are usually canonical
    return score


# =====================================================================
# RAG pipeline helpers (LLM-driven question answering)
# =====================================================================

# Configurable via env vars (set by launcher) so we don't hardcode the URL.
# When LLAMAFILE_URL env var is unset, we auto-detect by probing common
# llamafile/llama-server ports. This makes the app work whether the user
# launched the LLM via the bundled LlamaServerManager (8081), manually
# (typically 8080), or with a custom port.
LLAMAFILE_URL = os.environ.get('LLAMAFILE_URL', '')  # populated by _detect_llm_url
_LLM_PROBE_PORTS = ['8081', '8080', '8082', '8083', '18081']
_LLM_URL_LAST_PROBE = 0  # timestamp; re-probe every 30s if cached URL fails


def _detect_llm_url(force=False):
    """Probe the candidate ports, return first one that responds to /v1/models.

    Returns http://127.0.0.1:<port> or '' if none alive. Caches the result in
    LLAMAFILE_URL until a request fails (caller invalidates by calling with
    force=True after a connection error).
    """
    global LLAMAFILE_URL, _LLM_URL_LAST_PROBE
    import urllib.request as _ur
    now = time.time()
    if LLAMAFILE_URL and not force and (now - _LLM_URL_LAST_PROBE) < 30:
        return LLAMAFILE_URL
    _LLM_URL_LAST_PROBE = now
    # If env var was explicitly set, honor it without probing.
    env_url = os.environ.get('LLAMAFILE_URL', '').strip()
    if env_url:
        LLAMAFILE_URL = env_url
        return LLAMAFILE_URL
    for port in _LLM_PROBE_PORTS:
        url = f'http://127.0.0.1:{port}'
        try:
            req = _ur.Request(f'{url}/v1/models', headers={'Accept': 'application/json'})
            with _ur.urlopen(req, timeout=1.5) as r:
                if r.status == 200:
                    LLAMAFILE_URL = url
                    return LLAMAFILE_URL
        except Exception:
            continue
    LLAMAFILE_URL = ''
    return ''

# Title prediction: ask the LLM what Wikipedia article(s) would answer the
# question. The few-shot examples are critical. Without them the 3B model
# invents titles like "How to Build a Bridge".
TITLE_PREDICTION_SYSTEM = '''You are a Wikipedia expert. Given a user question, identify 2-4 SHORT, EXISTING Wikipedia article titles that contain the answer.

Rules:
- Use the actual canonical Wikipedia article title (one or two words usually)
- For "how does X work" use the article about X (not "How X works")
- For "what causes Y" suggest the article about Y itself
- For "how do I do Z" suggest articles about the SUBJECT of Z (not how-to titles)
- Output ONLY a valid JSON array of strings, nothing else

Examples:
"how do I build a bridge?" -> ["Bridge", "Civil engineering", "Structural engineering"]
"why is the sky blue?" -> ["Rayleigh scattering", "Diffuse sky radiation", "Sky"]
"how does Tylenol work?" -> ["Paracetamol", "Acetaminophen", "Analgesic"]
"what causes earthquakes?" -> ["Earthquake", "Plate tectonics", "Seismic wave"]
"who was Albert Einstein?" -> ["Albert Einstein"]
"what is jazz?" -> ["Jazz"]'''


def llm_complete(system, user, max_tokens=300, temperature=0.2, timeout=60):
    """Call the local llamafile chat endpoint, return the assistant text."""
    import urllib.request as _ur
    url = _detect_llm_url()
    if not url:
        raise ConnectionError('No LLM detected on any candidate port')
    body = {
        'model': 'local',
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    try:
        req = _ur.Request(
            f'{url}/v1/chat/completions',
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'},
        )
        with _ur.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
    except (ConnectionError, OSError):
        # Cached URL stale (LLM moved to a different port mid-session).
        # Force re-probe and try once more.
        url = _detect_llm_url(force=True)
        if not url:
            raise
        req = _ur.Request(
            f'{url}/v1/chat/completions',
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'},
        )
        with _ur.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
    text = resp['choices'][0]['message']['content']
    # Strip llama 3.x EOT marker
    text = text.replace('<|eot_id|>', '').strip()
    return text


def predict_titles(question):
    """
    Ask the LLM for likely Wikipedia article titles. Returns a list of
    title strings (may be empty if parsing fails).
    """
    try:
        raw = llm_complete(TITLE_PREDICTION_SYSTEM, question,
                           max_tokens=150, temperature=0.1, timeout=30)
        # Find the first JSON array in the response
        m = re.search(r'\[.*?\]', raw, re.DOTALL)
        if not m:
            return []
        titles = json.loads(m.group(0))
        return [t for t in titles if isinstance(t, str) and t.strip()]
    except Exception as e:
        sys.stderr.write(f"predict_titles failed: {e}\n")
        return []


def html_to_text(html_bytes_or_str):
    """Strip HTML tags and collapse whitespace. ~10x faster than DOMParser."""
    if isinstance(html_bytes_or_str, bytes):
        s = html_bytes_or_str.decode('utf-8', errors='replace')
    else:
        s = html_bytes_or_str
    # Drop scripts and styles entirely (they contain garbage that confuses LLM)
    s = re.sub(r'<script[^>]*>.*?</script>', ' ', s, flags=re.DOTALL | re.I)
    s = re.sub(r'<style[^>]*>.*?</style>', ' ', s, flags=re.DOTALL | re.I)
    # Drop common navbox/infobox/citation noise
    s = re.sub(r'<table[^>]*class="[^"]*(?:navbox|infobox|metadata|reference)[^"]*"[^>]*>.*?</table>',
               ' ', s, flags=re.DOTALL | re.I)
    s = re.sub(r'<sup[^>]*class="[^"]*reference[^"]*"[^>]*>.*?</sup>', ' ', s, flags=re.DOTALL | re.I)
    # Tags -> space
    s = re.sub(r'<[^>]+>', ' ', s)
    # Decode HTML entities
    s = s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    s = s.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def extract_lede(text, max_chars=600):
    """First N chars of cleaned article text (the lede paragraph)."""
    return text[:max_chars]


def extract_relevant_section(text, question, max_chars=4000):
    """
    Pick the most relevant ~4KB chunk of an article for the question.
    Strategy: tokenize question, find the densest cluster of question-noun
    matches in the article, return that slice.

    This is dumb but effective: better than just taking the first 6KB,
    because it surfaces the actual section that answers "how does X work"
    (which is often deep in the article, not the lede).
    """
    if len(text) <= max_chars:
        return text
    # Extract significant words from the question (>3 chars, not stop words)
    stop = {'how', 'why', 'what', 'when', 'where', 'who', 'does', 'did',
            'the', 'and', 'for', 'this', 'that', 'with', 'from', 'about',
            'work', 'works', 'cause', 'causes', 'tell', 'have', 'been',
            'into', 'than', 'they', 'their', 'there', 'which'}
    words = [w.lower() for w in re.findall(r'[a-zA-Z]{4,}', question)
             if w.lower() not in stop]
    if not words:
        return text[:max_chars]

    # Sliding window: find the position with the highest match count
    text_lower = text.lower()
    window = max_chars
    best_score = -1
    best_pos = 0
    # Always consider the lede (gives important context if question terms aren't there)
    lede_score = sum(text_lower[:window].count(w) for w in words)
    best_score = lede_score
    # Step through in 500-char increments
    step = 500
    for pos in range(0, max(0, len(text) - window), step):
        score = sum(text_lower[pos:pos + window].count(w) for w in words)
        if score > best_score:
            best_score = score
            best_pos = pos
    # If the best position isn't the lede, prepend a snippet of the lede
    # so the LLM has the article's basic context (what the article IS about)
    if best_pos > 1000:
        lede_snippet = text[:800].rsplit('.', 1)[0] + '.'
        body = text[best_pos:best_pos + max_chars - len(lede_snippet) - 20]
        return f"{lede_snippet}\n\n[...]\n\n{body}"
    return text[best_pos:best_pos + max_chars]


def gather_candidates(question, max_candidates=6):
    """
    Run the full retrieval pipeline. Returns a list of dicts:
      [{book, path, title, source: 'llm'|'exact'|'fulltext', text}]

    Strategy:
    1. LLM predicts likely article titles
    2. Resolve each via exact-path in priority order (Wikipedia first)
    3. ALSO run keyword search on cleaned question (catch cases LLM missed)
    4. Deduplicate by (book, resolved_path)
    """
    # Pick Wikipedia-family books in priority order
    wiki_books = []
    for prefix in ('wikipedia_en_all', 'wikipedia_en_medicine',
                   'wikipedia_', 'wikimed', 'wikivoyage', 'wikibooks',
                   'wikispecies'):
        for k in ARCHIVES:
            if k.startswith(prefix) and k not in wiki_books:
                wiki_books.append(k)
    other_books = [k for k in ARCHIVES if k not in wiki_books]

    candidates = []
    seen = set()  # (book, path)

    # Stage 1: LLM-predicted titles, resolved against Wikipedia books
    titles = predict_titles(question)
    for title in titles:
        for book in wiki_books:
            archive = ARCHIVES.get(book)
            if not archive:
                continue
            res = resolve_exact_title(archive, title)
            if res:
                path, real_title = res
                key = (book, path)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({
                    'book': book, 'path': path, 'title': real_title,
                    'source': 'llm', 'llm_suggested': title,
                })
                break  # found in highest-priority book, don't dilute
        if len(candidates) >= max_candidates:
            break

    # Stage 2: Keyword search as a backup (catches things the LLM missed)
    # Only runs if we have fewer than 3 LLM-resolved candidates
    if len(candidates) < 3:
        # Strip stop words for the keyword search
        stop = r'\b(how|do|does|did|the|a|an|of|to|i|me|my|you|your|what|when|where|why|tell|about|please|is|are|was|were|can|could|should|would|who)\b'
        cleaned = re.sub(stop, ' ', question.lower())
        cleaned = re.sub(r'[?!.,;:]', ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip() or question

        # If no Wikipedia-family ZIMs are installed (small library, niche
        # corpus, etc.), fall through to the other books rather than returning
        # "no results" while a perfectly searchable ZIM sits idle.
        search_books = wiki_books[:2] if wiki_books else other_books[:5]

        for book in search_books:
            archive = ARCHIVES.get(book)
            if not archive:
                continue
            # Try title-suggestion first. Works on every ZIM regardless of
            # whether the publisher built a Xapian full-text index. Many ZIMs
            # (PhET, niche encyclopedias, Stack Exchange snapshots) ship
            # without FT indexes, so Searcher() raises "Cannot create Search
            # without FT Xapian index" and silently swallowing that left us
            # with zero candidates. SuggestionSearcher walks the title list,
            # so it always works.
            try:
                ss = SuggestionSearcher(archive)
                # Try cleaned full query, then individual content words.
                title_queries = [cleaned] + [w for w in cleaned.split() if len(w) > 2]
                seen_in_book = set()
                for tq in title_queries:
                    if not tq.strip():
                        continue
                    sug = list(ss.suggest(tq).getResults(0, 3))
                    sug = [h for h in sug if not is_disambig_or_stub_path(h) and h not in seen_in_book]
                    for h in sug:
                        seen_in_book.add(h)
                        try:
                            e = archive.get_entry_by_path(h)
                            if e.is_redirect:
                                e = e.get_redirect_entry()
                            path, title = e.path, e.title or e.path
                        except Exception:
                            path, title = h, h
                        key = (book, path)
                        if key in seen:
                            continue
                        seen.add(key)
                        candidates.append({
                            'book': book, 'path': path, 'title': title,
                            'source': 'suggest',
                        })
                        if len(candidates) >= max_candidates:
                            break
                    if len(candidates) >= max_candidates:
                        break
                    # If the cleaned query already produced hits, don't dilute
                    # with single-word fallbacks that match many things.
                    if sug and tq == cleaned:
                        break
            except Exception:
                pass

            # Then full-text. Many ZIMs lack a Xapian FT index; the
            # exception is expected and not an error.
            if len(candidates) >= max_candidates:
                break
            try:
                s = Searcher(archive)
                q = Query().set_query(cleaned)
                results = s.search(q)
                for h in list(results.getResults(0, 3)):
                    if is_disambig_or_stub_path(h):
                        continue
                    try:
                        e = archive.get_entry_by_path(h)
                        if e.is_redirect:
                            e = e.get_redirect_entry()
                        path, title = e.path, e.title or e.path
                    except Exception:
                        path, title = h, h
                    key = (book, path)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append({
                        'book': book, 'path': path, 'title': title,
                        'source': 'fulltext',
                    })
                    if len(candidates) >= max_candidates:
                        break
            except Exception:
                # No FT index on this ZIM. SuggestionSearcher above already
                # ran; we tried our best.
                pass
            if len(candidates) >= max_candidates:
                break

    # Stage 3: Pull the actual text content for each candidate (text + lede)
    # This is the slow part. Happens after dedup so we only fetch each once.
    for c in candidates:
        archive = ARCHIVES.get(c['book'])
        if not archive:
            c['text'] = ''
            c['lede'] = ''
            continue
        try:
            entry = archive.get_entry_by_path(c['path'])
            if entry.is_redirect:
                entry = entry.get_redirect_entry()
            text = html_to_text(bytes(entry.get_item().content))
            c['text'] = text
            c['lede'] = extract_lede(text)
        except Exception as e:
            c['text'] = ''
            c['lede'] = ''
            sys.stderr.write(f"fetch failed {c['book']}/{c['path']}: {e}\n")

    return candidates


def score_candidates(question, candidates):
    """
    Quick relevance pass: drop candidates whose lede has no overlap with
    the question's significant words. Cheap heuristic, no LLM call.
    """
    stop = {'how', 'why', 'what', 'when', 'where', 'who', 'does', 'did',
            'the', 'and', 'for', 'this', 'that', 'with', 'from', 'about',
            'have', 'been', 'into', 'than', 'they', 'their'}
    words = {w.lower() for w in re.findall(r'[a-zA-Z]{4,}', question)
             if w.lower() not in stop}
    if not words:
        return candidates  # can't score, return as-is
    scored = []
    for c in candidates:
        if not c.get('lede'):
            continue
        title_lower = c['title'].lower()
        lede_lower = c['lede'].lower()
        # Count question-word matches in lede + title
        score = sum(1 for w in words if w in lede_lower or w in title_lower)
        # Bonus if title itself is one of the question words
        if any(w == title_lower for w in words):
            score += 5
        # Bonus for LLM-suggested candidates (they're trustworthy)
        if c.get('source') == 'llm':
            score += 2
        c['score'] = score
        scored.append(c)
    scored.sort(key=lambda x: -x['score'])
    # Drop zero-score candidates IF we have at least 2 with score > 0
    nonzero = [c for c in scored if c['score'] > 0]
    if len(nonzero) >= 2:
        return nonzero
    return scored


# =====================================================================
# End of RAG helpers
# =====================================================================


def follow_hatnote(archive, book_key, entry_path, max_chars=8000):
    """
    Look at the first ~8KB of an article for a hatnote pointing to the
    'real' article (e.g. Tylenol -> Acetaminophen). Return (path, title)
    of the linked article if found and resolvable in this archive,
    otherwise None.
    """
    try:
        entry = archive.get_entry_by_path(entry_path)
        if entry.is_redirect:
            entry = entry.get_redirect_entry()
        content = bytes(entry.get_item().content).decode('utf-8', errors='replace')
    except Exception:
        return None

    head = content[:max_chars]
    for pat in HATNOTE_PATTERNS:
        m = pat.search(head)
        if not m:
            continue
        link = m.group(1)
        title = m.group(2).strip()
        # Normalize the link to a ZIM-resolvable path. Wikipedia ZIM links
        # look like "Acetaminophen" (relative) or "A/Acetaminophen".
        link = link.lstrip('./')
        if link.startswith('_'):  # _res_, _mw_ etc are CSS/JS, skip
            continue
        # Try a few candidate paths
        candidates = [link, f'A/{link}', urllib.parse.unquote(link), f'A/{urllib.parse.unquote(link)}']
        for cand in candidates:
            try:
                test_entry = archive.get_entry_by_path(cand)
                # Resolve redirects to make sure target exists
                if test_entry.is_redirect:
                    test_entry = test_entry.get_redirect_entry()
                # Make sure it's not the SAME article (loop prevention)
                if test_entry.path == entry.path:
                    continue
                return (test_entry.path, test_entry.title or title)
            except Exception:
                continue
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quieter: only log errors
        if args and isinstance(args[0], str) and args[0].startswith(('4', '5')):
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            # Setup wizard / admin / api routes (if enabled)
            try:
                import setup_routes
                if setup_routes.is_initialized():
                    result = setup_routes.dispatch_get(path, qs)
                    if result is not None:
                        status, headers, body = result
                        self.send_response(status)
                        self._cors()
                        for k, v in headers:
                            self.send_header(k, v)
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
            except ImportError:
                pass

            if path == '/' or path == '/index.html':
                return self._index()
            if path == '/search':
                return self._search(qs)
            if path == '/answer':
                # Allow GET for ease of testing (body comes from query string)
                return self._answer({'q': (qs.get('q') or qs.get('question') or [''])[0]})
            if path.startswith('/content/'):
                return self._content(path[len('/content/'):])
            if path.startswith('/viewer'):
                # /viewer?book=<kiwix_name_prefix>  -> find matching archive
                # and 302 to /content/<full_key>/<main_entry>. Falls back to
                # the old behaviour (list of books) when no book param is set.
                book_param = (qs.get('book') or [''])[0].strip()
                if book_param:
                    # Match by exact key first, then by prefix (so callers can
                    # pass either 'wikipedia_en_all_maxi_2026-02' or just
                    # 'wikipedia_en_all_maxi').
                    full_key = None
                    if book_param in ARCHIVES:
                        full_key = book_param
                    else:
                        for k in ARCHIVES.keys():
                            if k.startswith(book_param):
                                full_key = k
                                break
                    if full_key is None:
                        self._json(404, {
                            'error': 'no installed ZIM matches that book name',
                            'requested': book_param,
                            'installed': list(ARCHIVES.keys()),
                        })
                        return
                    archive = ARCHIVES[full_key]
                    try:
                        main = archive.main_entry.get_item().path
                    except Exception:
                        main = 'index.html'
                    target = f'/content/{full_key}/{main}'
                    self.send_response(302)
                    self.send_header('Location', target)
                    self.end_headers()
                    return
                return self._index()
            self._json(404, {"error": "not found", "path": path})
        except Exception as e:
            self._json(500, {"error": str(e), "type": type(e).__name__})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length) if length > 0 else b''
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}

            # Setup/admin POST routes
            try:
                import setup_routes
                if setup_routes.is_initialized():
                    result = setup_routes.dispatch_post(path, body)
                    if result is not None:
                        status, headers, body_out = result
                        self.send_response(status)
                        self._cors()
                        for k, v in headers:
                            self.send_header(k, v)
                        self.send_header('Content-Length', str(len(body_out)))
                        self.end_headers()
                        self.wfile.write(body_out)
                        return
            except ImportError:
                pass

            if path == '/answer':
                return self._answer(body)
            self._json(404, {"error": "not found", "path": path})
        except Exception as e:
            self._json(500, {"error": str(e), "type": type(e).__name__})

    def _index(self):
        body = ['<!DOCTYPE html><html><head><meta charset="utf-8">',
                '<title>Apocalypse Kiwix Shim</title>',
                '<style>body{font-family:system-ui;max-width:800px;margin:40px auto;padding:0 20px;}',
                'h1{color:#2c3e50;}li{margin:8px 0;}</style></head><body>',
                f'<h1>📚 Apocalypse · {len(ARCHIVES)} ZIMs loaded</h1>',
                '<p>Python+libzim shim (replaces kiwix-serve on macOS+exFAT)</p>',
                '<ul>']
        for key, a in ARCHIVES.items():
            try:
                main = a.main_entry.get_item().path
            except Exception:
                main = ''
            body.append(
                f'<li><a href="/content/{key}/{main}">{key}</a> '
                f'<small>({a.entry_count:,} entries)</small></li>'
            )
        body.append('</ul></body></html>')
        html = '\n'.join(body).encode('utf-8')
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _search(self, qs):
        book = (qs.get('books.name') or qs.get('book') or [None])[0]
        pattern = (qs.get('pattern') or qs.get('q') or [''])[0]
        page_len = int((qs.get('pageLength') or ['10'])[0])
        fmt = (qs.get('format') or ['html'])[0]

        if not pattern:
            return self._json(400, {"error": "missing 'pattern'"})

        # If a specific book was requested, just search that one
        if book and book in ARCHIVES:
            targets = [book]
        else:
            # Order matters: we search high-priority books first and only
            # fall through to the rest when those return nothing useful.
            # Encyclopedias > Q&A > literature.
            priority_order = []
            for prefix in ('wikipedia_', 'wikimed', 'wikipedia_en_medicine',
                           'wikivoyage', 'wikibooks', 'wikisource',
                           'khanacademy', 'wikispecies'):
                for k in ARCHIVES:
                    if k.startswith(prefix) and k not in priority_order:
                        priority_order.append(k)
            # Stack Exchange next
            for k in ARCHIVES:
                if any(k.startswith(p) for p in (
                    'stackoverflow', 'askubuntu', 'serverfault', 'superuser',
                    'unix.stackexchange', 'math.stackexchange', 'physics.stackexchange',
                )) and k not in priority_order:
                    priority_order.append(k)
            # Gutenberg dead last (it's literature, almost always low-relevance
            # for factual questions.
            for k in ARCHIVES:
                if k not in priority_order:
                    priority_order.append(k)
            targets = priority_order

        # For each book do BOTH a title-suggestion search (matches article
        # titles, finds "Paracetamol" when you search "paracetamol") AND a
        # full-text search. Title hits get prepended because exact-title
        # matches are almost always what the user wants for "what is X" /
        # "how does X work" questions.
        per_book = []
        for key in targets:
            archive = ARCHIVES.get(key)
            if not archive:
                continue
            book_hits = []
            seen = set()

            # 0. EXACT TITLE MATCH (by far the highest-quality signal).
            # "Detroit" -> the city article at path "Detroit", not the bus
            # station. "Tylenol" -> the Tylenol article. Try the full query,
            # then progressively shorter prefixes ("tylenol work" -> "tylenol").
            # This catches the canonical article in ~95% of "what is X" cases.
            if key.startswith(('wikipedia_', 'wikimed', 'wikivoyage', 'wikibooks', 'wikispecies')):
                exact = resolve_exact_title(archive, pattern)
                if exact:
                    exact_path, exact_title = exact
                    if exact_path not in seen:
                        seen.add(exact_path)
                        book_hits.append({
                            "book": key, "path": exact_path, "title": exact_title,
                            "url": f"/content/{key}/{urllib.parse.quote(exact_path)}",
                            "match_type": "exact",
                        })

            # 1. Title suggestions: try the full query first, then fall back
            # to each word individually. Title-suggest matches title prefixes,
            # so multi-word queries usually return nothing, but ANY single
            # noun word ("tylenol", "paracetamol") will hit the right article.
            title_queries = [pattern]
            words = [w for w in pattern.split() if len(w) > 2]
            for w in words:
                if w not in title_queries:
                    title_queries.append(w)

            try:
                ss = SuggestionSearcher(archive)
                for tq in title_queries:
                    sug_results = ss.suggest(tq)
                    sug_hits = list(sug_results.getResults(0, 5))
                    # Filter out disambig and rank by title quality
                    # (canonical short titles beat qualified ones)
                    sug_hits = [h for h in sug_hits if not is_disambig_or_stub_path(h)]
                    sug_hits.sort(key=title_quality_score)
                    for h in sug_hits[:3]:
                        if h in seen:
                            continue
                        seen.add(h)
                        try:
                            e = archive.get_entry_by_path(h)
                            if e.is_redirect:
                                e = e.get_redirect_entry()
                            title = e.title or h
                            real_path = e.path
                            if real_path in seen and real_path != h:
                                continue
                            seen.add(real_path)
                        except Exception:
                            title = h
                            real_path = h
                        book_hits.append({
                            "book": key, "path": real_path, "title": title,
                            "url": f"/content/{key}/{urllib.parse.quote(real_path)}",
                            "match_type": "title",
                        })
                    # If we got hits from the full query, don't dilute with single-word fallbacks
                    if sug_hits and tq == pattern:
                        break
            except Exception:
                pass

            # 2. Full-text search
            try:
                s = Searcher(archive)
                q = Query().set_query(pattern)
                results = s.search(q)
                est = results.getEstimatedMatches()
                if est > 0:
                    n = min(page_len, 5)
                    for h in list(results.getResults(0, n)):
                        if h in seen or is_disambig_or_stub_path(h):
                            continue
                        seen.add(h)
                        try:
                            e = archive.get_entry_by_path(h)
                            if e.is_redirect:
                                e = e.get_redirect_entry()
                            title = e.title or h
                            real_path = e.path
                        except Exception:
                            title = h
                            real_path = h
                        book_hits.append({
                            "book": key, "path": real_path, "title": title,
                            "url": f"/content/{key}/{urllib.parse.quote(real_path)}",
                            "match_type": "fulltext",
                        })
            except Exception:
                pass

            # 3. Hatnote following: for Wikipedia-family books, look at the
            # top hit and check if it's a brand/stub page that points to the
            # article with the actual content. Examples:
            #   "Tylenol" -> "For the active ingredient, see Acetaminophen"
            #   -> Acetaminophen redirects to Paracetamol (the real article
            #      with mechanism, pharmacology, etc.)
            # Strict filters prevent routing to disambig pages or chasing
            # "for other uses" hatnotes from canonical articles.
            if (book_hits
                    and key.startswith(('wikipedia_', 'wikimed', 'wikivoyage', 'wikibooks'))):
                top = book_hits[0]
                hatnote = follow_hatnote(archive, key, top["path"])
                if hatnote:
                    main_path, main_title = hatnote
                    # Only accept the hatnote if it's substantively different
                    # from the top hit (not a self-loop, not disambig, not the
                    # exact same article path stem).
                    top_stem = top["path"].lstrip('A/').lower().split('_(')[0]
                    main_stem = main_path.lstrip('A/').lower().split('_(')[0]
                    if (main_path not in seen
                            and not is_disambig_or_stub_path(main_path)
                            and main_stem != top_stem):
                        seen.add(main_path)
                        # Insert AFTER the exact match (so users still see the
                        # literal article they searched for first), but before
                        # the noisier title/fulltext results. If the top hit
                        # ISN'T an exact match, prepend so the better article
                        # leads.
                        if top.get("match_type") == "exact":
                            book_hits.insert(1, {
                                "book": key, "path": main_path, "title": main_title,
                                "url": f"/content/{key}/{urllib.parse.quote(main_path)}",
                                "match_type": "hatnote",
                            })
                        else:
                            book_hits.insert(0, {
                                "book": key, "path": main_path, "title": main_title,
                                "url": f"/content/{key}/{urllib.parse.quote(main_path)}",
                                "match_type": "hatnote",
                            })

            if book_hits:
                per_book.append({"book": key, "hits": book_hits})

        # Interleave with priority. Strategy:
        #   Slot 0: top hit from highest-priority book (usually exact match)
        #   Slot 1: hatnote follow-up from same book if it exists (Tylenol -> Paracetamol)
        #   Slot 2+: top hit from each remaining book
        #   Then cycle through second/third hits round-robin
        # Global dedup by (path, title): when both wikipedia_all and
        # wikipedia_medicine have "Caffeine", only show once.
        out = []
        seen_titles = set()

        def _dedup_key(r):
            # Same article in different ZIMs has the same title, dedup on title only
            return (r["title"].lower().strip())

        def _add(r):
            k = _dedup_key(r)
            if k in seen_titles:
                return False
            seen_titles.add(k)
            out.append(r)
            return True

        if per_book:
            top_book = per_book[0]
            if top_book["hits"]:
                _add(top_book["hits"][0])
            # If top book's #2 hit is a hatnote, it goes RIGHT after exact match
            if (len(top_book["hits"]) > 1
                    and top_book["hits"][1].get("match_type") == "hatnote"
                    and len(out) < page_len):
                _add(top_book["hits"][1])
            # Top hit from every other book
            for b in per_book[1:]:
                if b["hits"] and len(out) < page_len:
                    _add(b["hits"][0])
        # Round-robin through remaining hits
        offset = 1
        while len(out) < page_len:
            added_this_round = False
            for b in per_book:
                if offset < len(b["hits"]) and len(out) < page_len:
                    if _add(b["hits"][offset]):
                        added_this_round = True
            if not added_this_round:
                break
            offset += 1

        if fmt == 'json':
            return self._json(200, {
                "results": out, "query": pattern,
                "books_with_hits": [b["book"] for b in per_book],
            })

        # Default: kiwix-serve compatible HTML
        body = ['<!DOCTYPE html><html><head><meta charset="utf-8">',
                f'<title>Search: {pattern}</title></head><body>',
                f'<h2>Results for "{pattern}"</h2>',
                '<ul class="results">']
        for r in out:
            body.append(
                f'<li><a href="/content/{r["book"]}/{urllib.parse.quote(r["path"])}">'
                f'{r["title"]}</a> '
                f'<span class="book-title">from {r["book"]}</span></li>'
            )
        body.append('</ul></body></html>')
        html = '\n'.join(body).encode('utf-8')
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _answer(self, body):
        """
        Full RAG pipeline endpoint:
          POST /answer  body: {"q": "how do I build a bridge?", "stream": false}
          GET  /answer?q=...

        Returns JSON:
          {
            "question": "...",
            "answer": "...",
            "sources": [{book, path, title, url, score, source}],
            "timing": {"predict": 1.2, "fetch": 8.5, "score": 0.0, "answer": 14.3}
          }
        """
        question = (body.get('q') or body.get('question') or '').strip()
        if not question:
            return self._json(400, {"error": "missing 'q'"})

        import time
        timing = {}

        # Stage 1+2+3: gather candidates (LLM titles + keyword backup + fetch text)
        t0 = time.time()
        candidates = gather_candidates(question, max_candidates=5)
        timing['gather'] = round(time.time() - t0, 2)

        # Stage 4: relevance score (drop irrelevant articles)
        t0 = time.time()
        candidates = score_candidates(question, candidates)
        timing['score'] = round(time.time() - t0, 2)

        if not candidates:
            return self._json(200, {
                "question": question,
                "answer": "I couldn't find any relevant articles in the offline library for this question. Try rephrasing or asking about a more specific topic.",
                "sources": [],
                "timing": timing,
            })

        # Stage 5: build context using extracted relevant sections (not lede)
        # Take top 3 candidates by score
        top_candidates = candidates[:3]
        context_parts = []
        sources_out = []
        for i, c in enumerate(top_candidates, start=1):
            section = extract_relevant_section(c['text'], question, max_chars=4000)
            context_parts.append(f"[Source {i}: {c['title']}]\n{section}")
            sources_out.append({
                'book': c['book'],
                'path': c['path'],
                'title': c['title'],
                'url': f"/content/{c['book']}/{urllib.parse.quote(c['path'])}",
                'score': c.get('score', 0),
                'source': c.get('source', 'unknown'),
            })

        context = '\n\n---\n\n'.join(context_parts)

        # Stage 6: ask the LLM to answer using the context
        answer_system = (
            "You are an offline reference assistant. Answer the user's question "
            "using ONLY the provided sources.\n\n"
            "Rules:\n"
            "1. Use ONLY information from the sources below. Do NOT add facts "
            "from your general knowledge.\n"
            "2. Cite each claim by [Source N].\n"
            "3. If the sources do not directly answer the question, say so plainly "
            "and describe what information IS available in the sources.\n"
            "4. Write a complete, useful answer (at least 3-5 sentences when the "
            "sources support it. Don't truncate to a single line.\n"
            "5. Never write a sentence without a [Source N] citation."
        )
        user_msg = f"Sources:\n\n{context}\n\n---\n\nQuestion: {question}"

        t0 = time.time()
        try:
            answer = llm_complete(answer_system, user_msg,
                                  max_tokens=600, temperature=0.3, timeout=180)
        except Exception as e:
            # Graceful search-only mode: when the LLM isn't running, return
            # the sources without a synthesized answer rather than a scary
            # "[LLM error: ...]" message. The frontend can render the
            # sources directly so the user still gets useful results.
            err_msg = str(e).lower()
            if 'connection refused' in err_msg or 'connection reset' in err_msg or 'no route' in err_msg:
                answer = (
                    "Search-only mode: the local LLM isn't running, so I can't "
                    "synthesize an answer. The relevant sources from your "
                    "offline library are listed below. Click any to read."
                )
            else:
                answer = f"[LLM error: {e}]"
        timing['answer'] = round(time.time() - t0, 2)

        return self._json(200, {
            "question": question,
            "answer": answer,
            "sources": sources_out,
            "timing": timing,
        })

    def _content(self, rest):
        # rest = "<book>/<entry/path>"
        rest = urllib.parse.unquote(rest)
        if '/' not in rest:
            return self._json(400, {"error": "expected /content/<book>/<path>"})
        book, entry_path = rest.split('/', 1)
        archive = ARCHIVES.get(book)
        if not archive:
            return self._json(404, {"error": f"unknown book: {book}"})

        # Try direct, then via redirect
        try:
            entry = archive.get_entry_by_path(entry_path)
        except Exception:
            # Try main entry fallback
            if not entry_path or entry_path == '/':
                entry = archive.main_entry
            else:
                return self._json(404, {"error": f"not in book: {entry_path}"})

        # Follow redirects (kiwix entries can redirect)
        try:
            if entry.is_redirect:
                entry = entry.get_redirect_entry()
        except Exception:
            pass

        item = entry.get_item()
        content = bytes(item.content)
        mime = item.mimetype or guess_mime(item.path)

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global ZIM_DIR, ARCHIVES

    # Make sure HTTPS works in PyInstaller-frozen bundles. The bundled Python
    # has no system cert store path configured (no homebrew on user's Mac,
    # no /etc/ssl on Windows), so any urllib.request HTTPS call would fail
    # with "certificate verify failed" -> URLError to the user. Fix by
    # pointing both env vars AND ssl's default context at certifi's bundle.
    try:
        import certifi
        import ssl as _ssl
        ca_file = certifi.where()
        os.environ['SSL_CERT_FILE'] = ca_file
        os.environ['REQUESTS_CA_BUNDLE'] = ca_file
        # Override ssl.create_default_context so every urlopen() call picks up
        # the certifi bundle, regardless of whether OpenSSL read the env var.
        _orig_create_default = _ssl.create_default_context
        def _patched_create_default(*a, **kw):
            kw.setdefault('cafile', ca_file)
            return _orig_create_default(*a, **kw)
        _ssl.create_default_context = _patched_create_default
    except Exception as _e:
        # If certifi isn't bundled (unlikely), fall back to system store.
        pass

    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=8888)
    p.add_argument('--install-dir', default=None,
                   help='Apocalypse install directory (e.g. /Volumes/Media/apocalypse). '
                        'If set, ZIM dir defaults to <install-dir>/kiwix/zim and setup '
                        'wizard is enabled.')
    p.add_argument('--zim-dir', default=None,
                   help='Override ZIM directory (defaults to <install-dir>/kiwix/zim or /Volumes/Media/apocalypse/kiwix/zim)')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--catalog', default=None,
                   help='Path to data/catalog.json (defaults to ../data/catalog.json relative to this script)')
    args = p.parse_args()

    install_dir = args.install_dir
    # When running as a PyInstaller-frozen subprocess, stdout/stderr are
    # often discarded (console=False bundles). Tee them to a log file so
    # we can debug user reports.
    if hasattr(sys, '_MEIPASS') and install_dir:
        try:
            log_dir = os.path.join(install_dir, 'logs')
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, 'shim.log')
            log_fp = open(log_path, 'a', buffering=1)
            log_fp.write(f"\n=== shim start {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} ===\n")
            log_fp.write(f"argv: {sys.argv}\n")
            log_fp.write(f"_MEIPASS: {sys._MEIPASS}\n")
            log_fp.flush()
            sys.stdout = log_fp
            sys.stderr = log_fp
        except Exception as _e:
            pass
    if install_dir:
        install_dir = os.path.abspath(install_dir)
    zim_dir = args.zim_dir
    if not zim_dir:
        if install_dir:
            zim_dir = os.path.join(install_dir, 'kiwix', 'zim')
        else:
            zim_dir = '/Volumes/Media/apocalypse/kiwix/zim'

    # Make sure zim_dir exists (don't fail, first-run setup)
    os.makedirs(zim_dir, exist_ok=True)
    ZIM_DIR = zim_dir

    # Wire up setup routes if we have an install dir
    if install_dir:
        catalog_path = args.catalog
        if not catalog_path:
            here = os.path.dirname(os.path.abspath(__file__))
            catalog_path = os.path.join(os.path.dirname(here), 'data', 'catalog.json')
        if os.path.exists(catalog_path):
            try:
                # Make sure the shim's own dir is on sys.path so setup_routes
                # is importable in PyInstaller bundles too
                shim_dir = os.path.dirname(os.path.abspath(__file__))
                if shim_dir not in sys.path:
                    sys.path.insert(0, shim_dir)
                import setup_routes
                def restart_hook():
                    global ARCHIVES
                    ARCHIVES = load_archives(ZIM_DIR)
                def relocate_hook(new_zim_dir):
                    global ZIM_DIR, ARCHIVES
                    ZIM_DIR = new_zim_dir
                    ARCHIVES = load_archives(ZIM_DIR)
                setup_routes.init(install_dir, catalog_path, restart_hook=restart_hook, relocate_hook=relocate_hook)
                print(f"Setup routes enabled (install_dir={install_dir})")
                print(f"  Wizard: http://{args.host}:{args.port}/setup")
                print(f"  Admin:  http://{args.host}:{args.port}/admin")
            except Exception as e:
                print(f"Warning: setup routes failed to init: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
        else:
            print(f"Warning: catalog.json not found at {catalog_path}", file=sys.stderr)

    print(f"Loading ZIMs from {ZIM_DIR}...")
    ARCHIVES = load_archives(ZIM_DIR)
    if not ARCHIVES:
        print(f"No ZIMs loaded yet. The setup wizard at http://{args.host}:{args.port}/setup will install them.")
    else:
        print(f"Serving {len(ARCHIVES)} books on http://{args.host}:{args.port}/")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)

    # Graceful shutdown on SIGTERM / SIGINT / SIGHUP. The parent tray process
    # signals us when the user quits the .app or Force Quits the parent;
    # without this handler the child would survive as an orphan and hold
    # files in the .app bundle, blocking Finder from trashing the app.
    import signal as _signal

    def _shutdown(signum, frame):
        try:
            print(f"\n[shim] received signal {signum}, shutting down")
            srv.shutdown()
        except Exception:
            pass

    for _sig in (_signal.SIGTERM, _signal.SIGINT, _signal.SIGHUP):
        try:
            _signal.signal(_sig, _shutdown)
        except (ValueError, OSError, AttributeError):
            pass

    # Parent watchdog: signal handlers handle clean Quit and clean Force Quit
    # (both deliver SIGTERM first), but a SIGKILL on the parent (Activity
    # Monitor's "Force Quit" hammer, kill -9, OOM kill, hard reboot) leaves
    # the shim orphaned. The shim then squats on port 8888 and the next
    # launch sees a stale dead-but-not-replying server. We poll the parent
    # PID every 5s and self-terminate if it's gone. On Unix, getppid() == 1
    # means we've been re-parented to launchd/init -- the original parent
    # is dead. On Windows, we compare against the captured initial parent.
    import os as _os
    import threading as _threading

    _initial_ppid = _os.getppid()

    def _parent_watchdog():
        while True:
            try:
                current_ppid = _os.getppid()
                # Unix: re-parented to PID 1 (launchd/init) means original
                # parent died. Windows: PPID changed from initial value.
                if current_ppid == 1 or (
                    sys.platform == 'win32' and current_ppid != _initial_ppid
                ):
                    print(f"\n[shim] parent process {_initial_ppid} is gone "
                          f"(now reparented to {current_ppid}), shutting down")
                    try:
                        srv.shutdown()
                    except Exception:
                        pass
                    # Hard exit. We can't trust serve_forever to actually
                    # unblock if the parent died mid-request.
                    _os._exit(0)
            except Exception:
                pass
            time.sleep(5.0)

    # Only run the watchdog when there IS an original parent that wasn't
    # already init/launchd. If a user runs the shim directly from a
    # terminal, the parent is the shell, which is fine (watchdog only
    # fires on reparent, not on initial PPID > 1).
    if _initial_ppid > 1:
        _threading.Thread(target=_parent_watchdog, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        try:
            srv.server_close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
