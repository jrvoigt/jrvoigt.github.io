"""Snapshot the Substack RSS feed into substack.json for jrvoigt.github.io.

The Research tab used to read the feed straight from the browser, which needed
a third-party CORS proxy because Substack sends no cross-origin headers. Those
proxies kept going down and stalling the page, so this script fetches the feed
on a schedule instead and commits the result next to index.html. The page then
loads its own file from its own domain: no CORS, no proxies, no third parties.

Writes substack.json in the repository root. No secrets or environment
variables needed — the feed is public.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

FEED_URL = "https://jensrvoigt.substack.com/feed"
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "substack.json")
MAX_ITEMS = 5

TAG_RE = re.compile(r"<[^>]*>")
WS_RE = re.compile(r"\s+")


def fetch_feed(url, attempts=3):
    """GET the feed with retries — Substack occasionally rate-limits."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "jrvoigt.github.io feed snapshot (+https://jrvoigt.github.io)",
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
    )
    last = None
    for n in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if n < attempts - 1:
                time.sleep(3 * (n + 1))
    raise SystemExit(f"could not fetch {url}: {last}")


def clean(text):
    """Strip markup and collapse whitespace, the way the page used to."""
    return WS_RE.sub(" ", TAG_RE.sub(" ", html.unescape(text or ""))).strip()


def text_of(item, tag):
    el = item.find(tag)
    return el.text if el is not None and el.text else ""


def main():
    raw = fetch_feed(FEED_URL)
    root = ET.fromstring(raw)

    items = []
    for item in list(root.iterfind("./channel/item"))[:MAX_ITEMS]:
        link = text_of(item, "link").strip()
        if not link:
            continue
        items.append(
            {
                "title": clean(text_of(item, "title")) or "Untitled",
                "link": link,
                # Kept as the raw RFC-822 string so the page formats the date
                # itself and both loading paths render identically.
                "pubDate": text_of(item, "pubDate").strip(),
                "summary": clean(text_of(item, "description")),
            }
        )

    if not items:
        raise SystemExit("feed parsed but contained no usable items — refusing to overwrite")

    payload = {
        "source": FEED_URL,
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }

    out = os.path.abspath(OUT_PATH)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"wrote {out} with {len(items)} items; newest: {items[0]['title']}")


if __name__ == "__main__":
    sys.exit(main())
