"""Weekly visitor-statistics email for jrvoigt.github.io.

Pulls the last 7 days of stats from the GoatCounter API, compares them with
the 7 days before that, and emails an HTML summary via Gmail SMTP.

Required environment variables (set as GitHub Actions secrets):
  GOATCOUNTER_SITE   e.g. "jrvoigt" (the xxx in xxx.goatcounter.com)
  GOATCOUNTER_TOKEN  API token from GoatCounter settings -> API
  GMAIL_USER         Gmail address used to send the report
  GMAIL_APP_PASSWORD Gmail app password (not the normal account password)
  REPORT_TO          Recipient address
"""

import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SITE = os.environ["GOATCOUNTER_SITE"]
TOKEN = os.environ["GOATCOUNTER_TOKEN"]
BASE = f"https://{SITE}.goatcounter.com/api/v0"

TAB_NAMES = {
    "/": "Direct page load",
    "/home": "Home",
    "/framework": "Framework",
    "/research": "Research",
    "/about": "About",
    "/media": "Media",
    "/contact": "Contact",
}


def api_get(path, **params):
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"GoatCounter API error {e.code} for {url}: {e.read().decode(errors='replace')}", file=sys.stderr)
        raise


def get_total(start, end):
    """Returns (total_visits, busiest_day, busiest_day_count) for the range.

    'total' includes event clicks (downloads etc.); subtract them so the
    headline number is genuine site visits only.
    """
    data = api_get("/stats/total", start=start.isoformat(), end=end.isoformat())
    total = data.get("total", 0) - data.get("total_events", 0)
    busiest_day, busiest_count = None, 0
    for day_stat in data.get("stats", []):
        day_total = day_stat.get("daily")
        if day_total is None:
            day_total = sum(day_stat.get("hourly", []))
        if day_total > busiest_count:
            busiest_count = day_total
            busiest_day = day_stat.get("day")
    return total, busiest_day, busiest_count


def get_pages(start, end):
    """Returns (pages, papers, media_articles) — regular pageviews vs. events.

    Events are tracked by the site with prefixed paths: paper/<name> for PDF
    downloads, media/<title> and article/<title> for outbound clicks.
    """
    data = api_get("/stats/hits", start=start.isoformat(), end=end.isoformat(), limit=100)
    pages, papers, media_articles = [], [], []
    for h in data.get("hits", []):
        path = h.get("path", "")
        if h.get("event"):
            if path.startswith("paper/"):
                papers.append((path[len("paper/"):], h.get("count", 0)))
            elif path.startswith("media/"):
                media_articles.append(("Media: " + path[len("media/"):], h.get("count", 0)))
            elif path.startswith("article/"):
                media_articles.append(("Article: " + path[len("article/"):], h.get("count", 0)))
            else:
                media_articles.append((path, h.get("count", 0)))
        else:
            pages.append(h)
    papers.sort(key=lambda x: -x[1])
    media_articles.sort(key=lambda x: -x[1])
    return pages[:10], papers[:10], media_articles[:10]


def get_breakdown(page, start, end):
    """page: 'toprefs', 'browsers', 'systems', 'locations', 'languages'."""
    try:
        data = api_get(f"/stats/{page}", start=start.isoformat(), end=end.isoformat(), limit=10)
        return data.get("stats", [])
    except Exception as e:
        print(f"Skipping breakdown '{page}': {e}", file=sys.stderr)
        return []  # a missing breakdown should not sink the whole report


def pct_change(current, previous):
    if previous == 0:
        return "new" if current > 0 else "&ndash;"
    change = (current - previous) / previous * 100
    arrow = "&#9650;" if change >= 0 else "&#9660;"
    color = "#2e7d32" if change >= 0 else "#c62828"
    return f'<span style="color:{color}">{arrow} {abs(change):.0f}%</span>'


def rows_html(items):
    if not items:
        return '<tr><td colspan="2" style="padding:6px 10px;color:#888">No data yet</td></tr>'
    out = []
    for label, count in items:
        out.append(
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee">{label}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">{count}</td></tr>'
        )
    return "".join(out)


def section(title, header, rows, count_label="Visits"):
    return f"""
    <h3 style="font-family:Georgia,serif;margin:24px 0 8px">{title}</h3>
    <table style="border-collapse:collapse;width:100%;font-size:14px">
      <tr>
        <th style="text-align:left;padding:6px 10px;background:#f0ece4">{header}</th>
        <th style="text-align:right;padding:6px 10px;background:#f0ece4">{count_label}</th>
      </tr>
      {rows}
    </table>"""


def fmt_day(day_str):
    if not day_str:
        return "&ndash;"
    try:
        return datetime.strptime(day_str[:10], "%Y-%m-%d").strftime("%A %d %b")
    except ValueError:
        return day_str


def main():
    today = date.today()
    week_end = today - timedelta(days=1)          # yesterday
    week_start = week_end - timedelta(days=6)     # 7 full days
    prev_end = week_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

    total, busiest_day, busiest_count = get_total(week_start, week_end)
    prev_total, _, _ = get_total(prev_start, prev_end)

    pages, papers, media_articles = get_pages(week_start, week_end)
    refs = get_breakdown("toprefs", week_start, week_end)
    browsers = get_breakdown("browsers", week_start, week_end)
    locations = get_breakdown("locations", week_start, week_end)

    period = f"{week_start.strftime('%d %b')} &ndash; {week_end.strftime('%d %b %Y')}"

    page_rows = rows_html([
        (TAB_NAMES.get(p.get("path", ""), p.get("path", "")), p.get("count", 0))
        for p in pages])
    paper_rows = rows_html(papers)
    media_rows = rows_html(media_articles)
    ref_rows = rows_html([
        (r.get("name") or "Direct / unknown", r.get("count", 0)) for r in refs])
    browser_rows = rows_html([
        (b.get("name") or "(unknown)", b.get("count", 0)) for b in browsers])
    location_rows = rows_html([
        (l.get("name") or "(unknown)", l.get("count", 0)) for l in locations])

    html = f"""
<div style="max-width:640px;margin:0 auto;font-family:Georgia,'Times New Roman',serif;color:#1a1a1a">
  <div style="background:#F7F5F0;border:1px solid #D8D3C8;border-radius:6px;padding:28px 32px">
    <p style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#8B7355;margin:0 0 4px">
      Weekly Website Report</p>
    <h1 style="font-size:26px;margin:0 0 2px">jrvoigt.github.io</h1>
    <p style="color:#888;font-size:14px;margin:0 0 24px">{period}</p>

    <table style="width:100%;border-collapse:collapse;margin-bottom:8px">
      <tr>
        <td style="padding:14px;background:#fff;border:1px solid #E0DBD2;border-radius:4px">
          <div style="font-size:12px;color:#8B7355;text-transform:uppercase;letter-spacing:1px">Total visits</div>
          <div style="font-size:30px;font-weight:bold">{total}</div>
          <div style="font-size:13px">{pct_change(total, prev_total)} vs. previous week ({prev_total})</div>
        </td>
        <td style="width:12px"></td>
        <td style="padding:14px;background:#fff;border:1px solid #E0DBD2;border-radius:4px">
          <div style="font-size:12px;color:#8B7355;text-transform:uppercase;letter-spacing:1px">Busiest day</div>
          <div style="font-size:20px;font-weight:bold;padding:5px 0">{fmt_day(busiest_day)}</div>
          <div style="font-size:13px">{busiest_count} visits</div>
        </td>
      </tr>
    </table>

    {section("Most-viewed sections", "Section", page_rows)}
    {section("Paper downloads", "Paper", paper_rows, "Downloads")}
    {section("Articles &amp; media clicked", "Item", media_rows, "Clicks")}
    {section("Where visitors came from", "Source", ref_rows)}
    {section("Visitor locations", "Country", location_rows)}
    {section("Browsers", "Browser", browser_rows)}

    <p style="font-size:12px;color:#999;margin-top:28px">
      Full dashboard: <a href="https://{SITE}.goatcounter.com" style="color:#8B7355">{SITE}.goatcounter.com</a><br>
      Generated automatically by GitHub Actions.</p>
  </div>
</div>"""

    downloads = sum(count for _, count in papers)
    plain_period = f"{week_start.strftime('%d %b')} - {week_end.strftime('%d %b %Y')}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Website report: {total} visits, {downloads} paper downloads ({plain_period})"
    msg["From"] = os.environ["GMAIL_USER"]
    msg["To"] = os.environ["REPORT_TO"]
    msg.attach(MIMEText(
        f"Weekly report {plain_period}: {total} visits, {downloads} paper downloads "
        f"(previous week: {prev_total} visits).", "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)

    print(f"Report sent: {total} visits for {week_start} - {week_end}")


if __name__ == "__main__":
    main()
