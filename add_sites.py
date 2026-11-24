#!/usr/bin/env python3
"""
add_sites.py - quickly add new websites to MicroNet's sites.json index.

Usage:
    python add_sites.py                      # interactive: paste URLs, empty line to finish
    python add_sites.py URL [URL ...]        # add one or more URLs directly
    python add_sites.py --file urls.txt      # add every URL listed in a text file
    python add_sites.py --no-crawl URL       # add only this URL, skip page discovery
    python add_sites.py --max-pages 50 URL   # raise the discovery cap (default 30)
    python add_sites.py --stats              # show index stats only
    python add_sites.py --tag PRODUCT URL    # force a tag instead of auto-detecting

Speed features:
    - All URLs are fetched in parallel (16 workers).
    - Already-indexed URLs/domains are skipped without a network request.
    - The index is written back atomically (temp file + os.replace).
    - Stdlib only: no pip installs needed.

The generated entries match the existing sites.json schema:
    domain, url, title, tag, description, pts, extras{og_image, og_type, keywords, canonical, json_ld}
"""

import json
import os
import re
import ssl
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin
from urllib.request import Request, urlopen

SITES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites.json")
MAX_WORKERS = 16
FETCH_TIMEOUT = 10
MAX_CRAWL_PAGES = 30
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) MicroNetIndexer/1.0")

META_RE = re.compile(r"<meta\s+[^>]*?>", re.IGNORECASE | re.DOTALL)
ATTR_RE = re.compile(r'(\w+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s"\'>]+))')
ASSET_RE = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|bmp|css|js|mjs|json|xml|rss|atom|txt|pdf|zip|rar|7z|gz|"
    r"mp3|mp4|avi|mov|wmv|flv|wav|ogg|doc|docx|xls|xlsx|ppt|pptx|csv|woff2?|ttf|eot)([?#]|$)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch(url):
    """Download a URL and return (status, text). Never raises."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # keep indexing quick; many small sites have bad certs
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urlopen(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
            raw = resp.read(2_000_000)  # cap at 2 MB so huge pages don't slow us down
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, raw.decode(charset, errors="replace")
    except Exception as e:
        return 0, f"__ERROR__ {type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _attrs(tag):
    return {k.lower(): unescape(v[2] if v[2] is not None else v[0])
            for k, v in (m.group(1, 2) for m in ATTR_RE.finditer(tag))}


class PageParser(HTMLParser):
    """Single-pass HTML parser: grabs title, meta tags and json-ld presence."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.og_image = ""
        self.og_type = ""
        self.keywords = ""
        self.canonical = ""
        self.json_ld = False
        self.json_ld_types = ""
        self.links = []
        self._in_title = False
        self._in_jsonld = False
        self._jsonld_buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        t = tag.lower()
        if t == "title":
            self._in_title = True
        elif t == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            val = a.get("content", "")
            if key == "og:title" and not self.title:
                self.title = val
            elif key in ("description", "og:description") and not self.description:
                self.description = val
            elif key == "og:image":
                self.og_image = val
            elif key == "og:type":
                self.og_type = val
            elif key == "keywords":
                self.keywords = val
        elif t == "link" and a.get("rel", "").lower() == "canonical":
            self.canonical = a.get("href", "")
        elif t == "a" and a.get("href"):
            self.links.append(a["href"])
        elif t == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title += data
        if self._in_jsonld:
            self._jsonld_buf.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title":
            self._in_title = False
        elif t == "script" and self._in_jsonld:
            blob = "".join(self._jsonld_buf)
            if blob.strip():
                self.json_ld = True
                self.json_ld_types = extract_jsonld_types(blob)
            self._in_jsonld = False
            self._jsonld_buf = []

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "meta":
            self.handle_starttag(tag, attrs)

    def unknown_decl(self, data):  # raw CDATA inside script tags lands here
        if self._in_jsonld:
            self._jsonld_buf.append(data)

    def handle_comment(self, data):
        if self._in_jsonld:
            self._jsonld_buf.append(data)


def extract_jsonld_types(blob):
    """Pull every @type value out of JSON-LD blocks (handles @graph and lists)."""
    types = []
    try:
        data = json.loads(blob)
    except Exception:
        return ""

    def walk(node):
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                types.append(t)
            elif isinstance(t, list):
                types.extend(str(x) for x in t)
            graph = node.get("@graph")
            if graph:
                walk(graph)
            for v in node.values():
                if isinstance(v, (dict, list)) and v is not graph:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return " ".join(types).lower()


def auto_tag(meta, url):
    """Guess the index tag from structured data, keywords and title signals."""
    structured = f"{meta['json_ld_types']} {meta['og_type']}".lower()
    human = f"{meta['title']} {meta['keywords']} {meta['description']}".lower()
    path = urlsplit(url).path.lower()
    homepage = path in ("", "/")

    if re.search(r"\b(product|productpage|item)\b", structured) or meta["og_type"] == "product" \
            or re.search(r"\b(buy|price|cart|checkout|shop|store|order)\b", human):
        return "PRODUCT"
    if re.search(r"\b(game|videogame)\b", structured) or re.search(r"\b(game|gaming)\b", human):
        return "GAME"
    if re.search(r"\b(book|books)\b", structured) or re.search(r"\b(book|ebook|novel)\b", human):
        return "BOOK"
    if re.search(r"\b(person|profilepage)\b", structured) or re.search(r"\b(profile|portfolio|resume|cv)\b", human):
        return "PROFILE"
    if re.search(r"\bwebsite\b", structured) and homepage:
        return "WEBSITE"
    if re.search(r"\b(article|blogposting|newsarticle|techarticle)\b", structured) or meta["og_type"] == "article" \
            or re.search(r"\b(article|blog|news|post)\b", human) or re.search(r"/(blog|news|article|post)s?/", path):
        return "ARTICLE"
    if re.search(r"\b(dataset|imageobject|videoobject|map|softwareapplication)\b", structured):
        return "OBJECT"
    if homepage:
        return "WEBSITE"
    return "SITE"


def parse_page(html, base_url):
    p = PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    title = (p.title or "").strip()
    description = (p.description or "").strip()
    canonical = p.canonical.strip()
    if canonical:
        canonical = urljoin(base_url, canonical)
    if p.og_image and not p.og_image.startswith(("http://", "https://")):
        p.og_image = urljoin(base_url, p.og_image)
    return {
        "title": unescape(title),
        "description": unescape(description),
        "og_image": p.og_image,
        "og_type": p.og_type,
        "keywords": p.keywords,
        "canonical": canonical,
        "json_ld": p.json_ld,
        "json_ld_types": p.json_ld_types,
        "links": p.links,
    }


# --------------------------------------------------------------------------- #
# Entry building
# --------------------------------------------------------------------------- #

def normalize_url(raw):
    """Add a scheme if missing and return the cleaned URL."""
    raw = raw.strip()
    if not raw:
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if not parts.netloc:
        return None
    return raw


def url_to_entry(url, meta, tag_override=None):
    """Build a sites.json-shaped entry from a URL and parsed metadata."""
    parts = urlsplit(url)
    domain = (parts.netloc + parts.path).rstrip("/").lower()
    if domain != parts.netloc.lower() and not parts.path:
        domain = parts.netloc.lower()

    # Score roughly like existing entries (30-45 pts), mirroring real SEO value.
    pts = 30
    if meta["title"]:
        pts += 2
    if meta["description"]:
        pts += 2
    if meta["keywords"]:
        pts += 2
    if meta["og_image"]:
        pts += 2
    if meta["canonical"]:
        pts += 2
    if url.startswith("https://"):
        pts += 2
    if meta["json_ld"]:
        pts += 3
    pts = min(pts, 45)

    extras = {}
    if meta["og_image"]:
        extras["og_image"] = meta["og_image"]
    if meta["og_type"]:
        extras["og_type"] = meta["og_type"]
    if meta["keywords"]:
        extras["keywords"] = meta["keywords"]
    if meta["canonical"]:
        extras["canonical"] = meta["canonical"]
    if meta["json_ld"]:
        extras["json_ld"] = True

    return {
        "domain": domain,
        "url": url,
        "title": meta["title"] or domain,
        "tag": tag_override or auto_tag(meta, url),
        "description": meta["description"],
        "pts": pts,
        "extras": extras,
    }


# --------------------------------------------------------------------------- #
# Index handling
# --------------------------------------------------------------------------- #

def load_index(path=SITES_FILE):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("sites.json must be a JSON array")
    return data


def save_index(entries, path=SITES_FILE):
    """Atomic write so an interrupted run never corrupts the index."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def discover_links(meta, base_url, seen_urls, seen_domains):
    """Extract indexable same-domain pages linked from a fetched page."""
    base_host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    found = set()
    for href in meta.get("links", ()):
        if href.startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        link = urljoin(base_url, href.split("#")[0])
        if not link:
            continue
        parts = urlsplit(link)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        if parts.netloc.lower().removeprefix("www.") != base_host:
            continue  # only pages belonging to the same site
        link = normalize_url(link)
        if not link or ASSET_RE.search(link):
            continue
        key = link.rstrip("/").lower()
        if key in seen_urls or parts.netloc.lower() in seen_domains:
            continue
        found.add(key)
    # Top-level pages first (example.com/about before example.com/a/b/c)
    return sorted(found, key=lambda u: (urlsplit(u).path.count("/"), len(u)))


def fetch_batch(urls):
    """Fetch and parse a list of URLs in parallel. Returns {url: meta|None}."""
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch, u): u for u in urls}
        done = 0
        for fut in as_completed(futures):
            u = futures[fut]
            status, text = fut.result()
            done += 1
            if text.startswith("__ERROR__") or not text:
                results[u] = None
                print(f"  [{done}/{len(urls)}] FAIL  {u}  ({text.removeprefix('__ERROR__ ')})")
            else:
                results[u] = parse_page(text, u)
                print(f"  [{done}/{len(urls)}] OK    {u}")
    return results


def add_urls(urls, entries, tag_override=None, crawl=True, max_pages=MAX_CRAWL_PAGES):
    """Fetch new URLs in parallel and append entries. Returns (added, failed)."""
    # Existing lookup maps: skip repeats without hitting the network.
    seen_urls = {e.get("url", "").rstrip("/").lower() for e in entries}
    seen_domains = {e.get("domain", "").lower() for e in entries}
    seen_titles = {e.get("title", "").lower() for e in entries}

    todo = []
    skipped = 0
    for u in urls:
        u = normalize_url(u)
        if not u:
            continue
        key = u.rstrip("/").lower()
        dom = urlsplit(u).netloc.lower()
        if key in seen_urls or key in seen_domains or key in seen_titles:
            skipped += 1
            continue
        todo.append(u)

    if skipped:
        print(f"  -> skipped {skipped} URL(s) already in the index")

    if not todo:
        return 0, 0

    results = fetch_batch(todo)
    added = failed = 0
    for u in todo:
        meta = results.get(u)
        if meta is None:
            failed += 1
            continue
        entry = url_to_entry(u, meta, tag_override=tag_override)
        entries.append(entry)
        added += 1
        print(f"  + added  {entry['title'][:60]}  ({entry['pts']} pts, tag={entry['tag']})")

    # Phase 2: auto-discover other pages on the same site (example.com/[example]).
    if crawl and results:
        candidates = set()
        for u, meta in results.items():
            if meta:
                candidates.update(discover_links(meta, u, seen_urls, seen_domains))
        candidates -= {u.rstrip("/").lower() for u in todo}
        candidates = list(candidates)[:max_pages]
        if candidates:
            print(f"\n  Discovered {len(candidates)} new page(s) on the same site — fetching them too…")
            results2 = fetch_batch(candidates)
            for u in candidates:
                meta = results2.get(u)
                if meta is None:
                    continue
                entry = url_to_entry(u, meta, tag_override=tag_override)
                entries.append(entry)
                added += 1
                print(f"  + added  {entry['title'][:60]}  ({entry['pts']} pts, tag={entry['tag']})")

    return added, failed


def show_stats(entries):
    from collections import Counter
    tags = Counter(e.get("tag") for e in entries)
    print(f"Index: {len(entries)} entries in {SITES_FILE}")
    print("Tags:", ", ".join(f"{k or '?'}={v}" for k, v in tags.most_common()))


# --------------------------------------------------------------------------- #
# CLI / interactive loop
# --------------------------------------------------------------------------- #

def read_urls_from_args(argv):
    if "--file" in argv:
        i = argv.index("--file")
        path = argv[i + 1]
        with open(path, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    skip_next = False
    urls = []
    flags_with_value = {"--file", "--tag", "--max-pages"}
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a in flags_with_value:
            skip_next = True   # its value is an option, not a URL
            continue
        if not a.startswith("-"):
            urls.append(a)
    return urls


def main():
    argv = sys.argv[1:]
    interactive = not argv or "-i" in argv
    tag_override = None
    if "--tag" in argv:
        i = argv.index("--tag")
        if i + 1 < len(argv):
            tag_override = argv[i + 1].strip().upper()

    if not os.path.exists(SITES_FILE):
        print(f"sites.json not found at {SITES_FILE}", file=sys.stderr)
        sys.exit(1)

    print("Loading index…")
    entries = load_index()
    show_stats(entries)

    if "--stats" in argv:
        return

    if interactive:
        print("\nPaste website URLs one per line. Each URL's other same-site pages are discovered automatically.")
        print("Empty line (or 'done') to save.\n")
        urls = []
        while True:
            try:
                line = input("url> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line or line.lower() in ("done", "q", "quit"):
                break
            urls.append(line)
    else:
        urls = read_urls_from_args(argv)

    if not urls:
        print("No URLs entered. Nothing to do.")
        return

    print(f"\nFetching {len(set(urls))} URL(s) with {MAX_WORKERS} workers…")
    crawl = "--no-crawl" not in argv
    max_pages = MAX_CRAWL_PAGES
    if "--max-pages" in argv:
        i = argv.index("--max-pages")
        if i + 1 < len(argv):
            try:
                max_pages = max(0, int(argv[i + 1]))
            except ValueError:
                print(f"Invalid --max-pages value '{argv[i + 1]}', using {MAX_CRAWL_PAGES}.")
    added, failed = add_urls(urls, entries, tag_override=tag_override, crawl=crawl, max_pages=max_pages)

    if added:
        save_index(entries)
        print(f"\nSaved. {added} added, {failed} failed, index now has {len(entries)} entries.")
    else:
        print(f"\nNothing added ({failed} failed). sites.json left untouched.")


if __name__ == "__main__":
    main()
