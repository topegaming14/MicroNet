#!/usr/bin/env python3
"""
add_sites.py - quickly add new websites to MicroNet's sites.json index.

Usage:
    python add_sites.py                      # interactive: paste URLs, empty line to finish
    python add_sites.py URL [URL ...]        # add one or more URLs directly
    python add_sites.py --file urls.txt      # add every URL listed in a text file
    python add_sites.py --stats              # show index stats only

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
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) MicroNetIndexer/1.0")

META_RE = re.compile(r"<meta\s+[^>]*?>", re.IGNORECASE | re.DOTALL)
ATTR_RE = re.compile(r'(\w+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s"\'>]+))')


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
        elif t == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title += data

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title":
            self._in_title = False
        elif t == "script" and self._in_jsonld:
            blob = "".join(self._jsonld_buf)
            if blob.strip():
                self.json_ld = True
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


def url_to_entry(url, meta):
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
        "tag": "SITE",
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


def add_urls(urls, entries, interactive=False):
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

    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch, u): u for u in todo}
        done = 0
        for fut in as_completed(futures):
            u = futures[fut]
            status, text = fut.result()
            done += 1
            if text.startswith("__ERROR__") or not text:
                results[u] = None
                print(f"  [{done}/{len(todo)}] FAIL  {u}  ({text.removeprefix('__ERROR__ ')})")
            else:
                results[u] = parse_page(text, u)
                print(f"  [{done}/{len(todo)}] OK    {u}")

    added = failed = 0
    for u in todo:
        meta = results.get(u)
        if meta is None:
            failed += 1
            continue
        entry = url_to_entry(u, meta)
        entries.append(entry)
        added += 1
        print(f"  + added  {entry['title'][:60]}  ({entry['pts']} pts)")

    if added and interactive:
        # In interactive mode let the user tag entries before they are saved.
        tags = {}
        for i, e in enumerate(entries[len(entries) - added:], start=1):
            t = input(f"  tag for '{e['title'][:40]}' [SITE]: ").strip().upper()
            if t:
                e["tag"] = t
                tags[i] = t
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
    return [a for a in argv if not a.startswith("-")]


def main():
    argv = sys.argv[1:]
    interactive = not argv or "-i" in argv

    if not os.path.exists(SITES_FILE):
        print(f"sites.json not found at {SITES_FILE}", file=sys.stderr)
        sys.exit(1)

    print("Loading index…")
    entries = load_index()
    show_stats(entries)

    if "--stats" in argv:
        return

    if interactive:
        print("\nPaste website URLs one per line. Empty line (or 'done') to save.\n")
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
    added, failed = add_urls(urls, entries, interactive=interactive)

    if added:
        save_index(entries)
        print(f"\nSaved. {added} added, {failed} failed, index now has {len(entries)} entries.")
    else:
        print(f"\nNothing added ({failed} failed). sites.json left untouched.")


if __name__ == "__main__":
    main()
