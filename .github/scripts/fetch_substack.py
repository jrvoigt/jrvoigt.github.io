"""Snapshot the Substack RSS feed into substack.json for jrvoigt.github.io.

The Research tab used to read the feed straight from the browser, which needed
a third-party CORS proxy because Substack sends no cross-origin headers. Those
proxies kept going down and stalling the page, so this script fetches the feed
on a schedule instead and commits the result next to index.html. The page then
loads its own file from its own domain: no CORS, no proxies, no third parties.

Substack answers GitHub's runner IPs with 403, so a direct fetch is only the
first of several sources tried here; the rest read the feed through services
that Substack does answer. Whatever the source, the output is identical.

Writes substack.json in the repository root. No secrets or environment
variables needed — the feed is public. If every source fails the script exits
non-zero and leaves the existing snapshot untouched, so the site keeps showing
the last good data rather than an empty list.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

FEED_URL = "https://jensrvoigt.substack.com/feed"
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "substack.json")
MAX_ITEMS = 5
TIMEOUT = 30

# Substack's bot protection is friendlier to something that looks like a browser
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    "Accept-Language": "en-GB,en;q=0.9",
}

TAG_RE = re.compile(r"<[^>]*>")
WS_RE = re.compile(r"\s+")


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read().decode("utf-8", "replace")


def clean(text):
    """Strip markup and collapse whitespace, the way the page used to."""
    return WS_RE.sub(" ", TAG_RE.sub(" ", html.unescape(text or ""))).strip()


def iso_date(value):
    """Normalise any of the formats our sources emit to ISO 8601 UTC.

    The page just hands this to new Date(), and ISO is the one format every
    browser parses the same way.
    """
    value = (value or "").strip()
    if not value:
        return ""
    for parse in (
        parsedate_to_datetime,                       # RFC 822: "Wed, 08 Jul 2026 07:19:48 GMT"
        datetime.fromisoformat,                      # "2026-07-08T07:19:48+00:00"
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),  # rss2json's format
    ):
        try:
            dt = parse(value)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def items_from_xml(xml_text):
    if "<item" not in xml_text:
        raise ValueError("response was not an RSS feed")
    root = ET.fromstring(xml_text)

    def text_of(node, tag):
        el = node.find(tag)
        return el.text if el is not None and el.text else ""

    out = []
    for node in list(root.iterfind("./channel/item"))[:MAX_ITEMS]:
        link = text_of(node, "link").strip()
        if not link:
            continue
        out.append(
            {
                "title": clean(text_of(node, "title")) or "Untitled",
                "link": link,
                "pubDate": iso_date(text_of(node, "pubDate")),
                "summary": clean(text_of(node, "description")),
            }
        )
    return out


def items_from_rss2json(body):
    data = json.loads(body)
    if data.get("status") != "ok":
        raise ValueError("rss2json reported status=%r" % data.get("status"))
    out = []
    for entry in (data.get("items") or [])[:MAX_ITEMS]:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        out.append(
            {
                "title": clean(entry.get("title")) or "Untitled",
                "link": link,
                "pubDate": iso_date(entry.get("pubDate")),
                "summary": clean(entry.get("description")),
            }
        )
    return out


def source_direct():
    return items_from_xml(get(FEED_URL))


def source_rss2json():
    url = "https://api.rss2json.com/v1/api.json?rss_url=" + urllib.parse.quote(FEED_URL, safe="")
    return items_from_rss2json(get(url, {"User-Agent": BROWSER_HEADERS["User-Agent"]}))


def source_codetabs():
    url = "https://api.codetabs.com/v1/proxy?quest=" + urllib.parse.quote(FEED_URL, safe="")
    return items_from_xml(get(url))


def source_allorigins():
    url = "https://api.allorigins.win/raw?url=" + urllib.parse.quote(FEED_URL, safe="")
    return items_from_xml(get(url))


SOURCES = [
    ("substack direct", source_direct),
    ("rss2json", source_rss2json),
    ("codetabs proxy", source_codetabs),
    ("allorigins proxy", source_allorigins),
]


def collect():
    problems = []
    for name, fn in SOURCES:
        for attempt in range(2):
            try:
                items = fn()
                if not items:
                    raise ValueError("no usable items")
                print(f"source '{name}' succeeded with {len(items)} items")
                return items
            except Exception as exc:  # any source may fail in its own way
                problems.append(f"{name}: {exc}")
                if attempt == 0:
                    time.sleep(3)
    raise SystemExit(
        "every source failed, leaving the existing snapshot untouched:\n  "
        + "\n  ".join(problems)
    )


def existing_items(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("items")
    except (OSError, ValueError):
        return None


def main():
    items = collect()
    out = os.path.abspath(OUT_PATH)

    # Rewriting on every run would bump "fetched" and produce a daily commit
    # even when nothing was published, so leave the file alone unless the
    # posts themselves changed.
    if existing_items(out) == items:
        print("no new posts — snapshot already up to date")
        return

    payload = {
        "source": FEED_URL,
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }

    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"wrote {out} with {len(items)} items; newest: {items[0]['title']}")


if __name__ == "__main__":
    sys.exit(main())
