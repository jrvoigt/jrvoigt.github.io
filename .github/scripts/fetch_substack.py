"""Snapshot the Substack RSS feed into substack.json for jrvoigt.github.io.

The Research tab used to read the feed straight from the browser, which needed
a third-party CORS proxy because Substack sends no cross-origin headers. Those
proxies kept going down and stalling the page, so this script fetches the feed
on a schedule instead and commits the result next to index.html. The page then
loads its own file from its own domain: no CORS, no proxies, no third parties.

Substack blocks datacenter IP ranges, so a direct fetch always returns 403 from
a GitHub runner and only ever succeeds when this is run from a normal machine.
The real work is done by the JSON converters that follow it, which read the feed
from their own servers. Whatever the source, the output here is identical.

Failure policy: a converter having a bad morning is harmless, because the site
keeps serving the snapshot already committed. So a failed refresh exits 0 with a
warning rather than failing the build and sending mail. It only exits non-zero
when there is no usable snapshot at all, or when no source has succeeded for
STALE_AFTER_DAYS - which means the pipeline is genuinely broken, not just flaky.
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
TIMEOUT = 25

# Refresh "checked" at least this often even when nothing was published, so the
# field stays a usable freshness signal. Costs about one commit a week.
TOUCH_AFTER_DAYS = 7
# No source has worked for this long: treat it as broken and fail the run.
STALE_AFTER_DAYS = 14

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
    # JSON Feed sends "2026-08-25T17:53:47.000Z", which fromisoformat rejects
    # on older Pythons unless the Z is spelled out as an offset.
    for raw in (value, value.replace("Z", "+00:00")):
        for parse in (
            parsedate_to_datetime,                                  # RFC 822
            datetime.fromisoformat,                                 # ISO 8601
            lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),    # rss2json
        ):
            try:
                dt = parse(raw)
            except (TypeError, ValueError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def entry(title, link, date, summary):
    link = (link or "").strip()
    if not link:
        return None
    return {
        "title": clean(title) or "Untitled",
        "link": link,
        "pubDate": iso_date(date),
        "summary": clean(summary),
    }


def items_from_xml(xml_text):
    if "<item" not in xml_text:
        raise ValueError("response was not an RSS feed")
    root = ET.fromstring(xml_text)

    def text_of(node, tag):
        el = node.find(tag)
        return el.text if el is not None and el.text else ""

    out = []
    for node in list(root.iterfind("./channel/item"))[:MAX_ITEMS]:
        e = entry(
            text_of(node, "title"),
            text_of(node, "link"),
            text_of(node, "pubDate"),
            text_of(node, "description"),
        )
        if e:
            out.append(e)
    return out


def source_direct():
    """Only ever works off a GitHub runner; kept so local runs skip the converters."""
    return items_from_xml(get(FEED_URL))


def source_rss2json():
    url = "https://api.rss2json.com/v1/api.json?rss_url=" + urllib.parse.quote(FEED_URL, safe="")
    data = json.loads(get(url))
    if data.get("status") != "ok":
        raise ValueError("rss2json reported status=%r" % data.get("status"))
    out = []
    for it in (data.get("items") or [])[:MAX_ITEMS]:
        e = entry(it.get("title"), it.get("link"), it.get("pubDate"), it.get("description"))
        if e:
            out.append(e)
    return out


def source_feed2json():
    url = "https://feed2json.org/convert?url=" + urllib.parse.quote(FEED_URL, safe="")
    data = json.loads(get(url))
    out = []
    for it in (data.get("items") or [])[:MAX_ITEMS]:
        e = entry(
            it.get("title"),
            it.get("url"),
            it.get("date_published"),
            it.get("summary") or it.get("content_html"),
        )
        if e:
            out.append(e)
    return out


SOURCES = [
    ("substack direct", source_direct),
    ("rss2json", source_rss2json),
    ("feed2json", source_feed2json),
]


def collect():
    """Return (items, None) on success, or (None, problems) if every source failed."""
    problems = []
    for name, fn in SOURCES:
        for attempt in range(2):
            try:
                items = fn()
                if not items:
                    raise ValueError("no usable items")
                print("source '%s' succeeded with %d items" % (name, len(items)))
                return items, None
            except Exception as exc:  # each source fails in its own way
                problems.append("%s: %s" % (name, exc))
                if attempt == 0:
                    time.sleep(3)
    return None, problems


def load_existing(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("items"), list) and data["items"]:
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return None


def age_in_days(stamp, now):
    if not stamp:
        return None
    try:
        dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - dt).total_seconds() / 86400.0


def save(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main():
    out = os.path.abspath(OUT_PATH)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = load_existing(out)
    items, problems = collect()

    if items is None:
        report = "could not refresh the snapshot:\n  " + "\n  ".join(problems)

        if existing is None:
            print("ERROR: " + report, file=sys.stderr)
            print("No usable snapshot exists, so there is nothing to fall back to.", file=sys.stderr)
            return 1

        # A converter having a bad morning is routine; weeks of silence is not.
        age = age_in_days(existing.get("checked"), now)
        if age is not None and age >= STALE_AFTER_DAYS:
            print("ERROR: " + report, file=sys.stderr)
            print(
                "No source has worked for %.0f days - the feed pipeline looks broken, "
                "not just flaky." % age,
                file=sys.stderr,
            )
            return 1

        since = ("%.1f days ago" % age) if age is not None else "unknown"
        print("WARNING: " + report)
        print("Keeping the existing snapshot (last successful refresh: %s)." % since)
        print("The site is unaffected - it serves the committed snapshot.")
        return 0

    if existing is None or existing.get("items") != items:
        save(out, {"source": FEED_URL, "fetched": stamp, "checked": stamp, "items": items})
        print("wrote %d items; newest: %s" % (len(items), items[0]["title"]))
        return 0

    # Nothing new. Refresh "checked" occasionally so it stays a meaningful
    # freshness signal, but not every run, or the timestamp alone would produce
    # a commit every single day.
    age = age_in_days(existing.get("checked"), now)
    if age is None or age >= TOUCH_AFTER_DAYS:
        payload = dict(existing)
        payload.setdefault("source", FEED_URL)
        payload["checked"] = stamp
        save(out, payload)
        print("no new posts - refreshed the 'checked' timestamp")
        return 0

    print("no new posts - snapshot already up to date (checked %.1f days ago)" % age)
    return 0


if __name__ == "__main__":
    sys.exit(main())
