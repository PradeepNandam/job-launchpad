#!/usr/bin/env python3
"""
DevOps / Azure DevOps / Boomi daily job alert.

Pulls FRESH openings (posted in the configured window) from legitimate,
publicly documented job-board APIs — not scraping, not automated
applications, just reading public data the same way a browser would.
Sources used:
  - Greenhouse public job board API   (boards-api.greenhouse.io)
  - Lever public job board API        (api.lever.co)
  - RemoteOK public API               (remoteok.com/api)
  - We Work Remotely RSS feed         (weworkremotely.com)

Add company slugs you care about to GREENHOUSE_BOARDS / LEVER_BOARDS below.
LinkedIn/Naukri/Indeed are NOT included here on purpose — they don't offer
a public API and scraping them violates their Terms of Service. Keep using
their own native "email me daily" alert feature alongside this script for
full coverage (see the README notes at the bottom of this file).

Run manually:
    python job_alert.py

Environment variables required (set as GitHub Secrets when automated):
    EMAIL_ADDRESS       - the Gmail address to send FROM
    EMAIL_APP_PASSWORD  - a Gmail App Password (not your normal password)
    TO_EMAIL            - where the digest should be sent (can be same as EMAIL_ADDRESS)
"""

import os
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from xml.etree import ElementTree

import requests

# ---------------------------------------------------------------------------
# CONFIG — edit this section to match your search
# ---------------------------------------------------------------------------

KEYWORDS = [
    "azure devops", "devops engineer", "devops consultant",
    "boomi", "dell boomi",
]

# Only show postings from the last N days (set to 1 for "new since yesterday")
FRESHNESS_DAYS = 1

# Rough experience-range filter applied to the free-text description, in years.
# Set to None to disable and see everything that matches the keywords.
MIN_EXPERIENCE = 3
MAX_EXPERIENCE = 6

# Company slugs on Greenhouse's public board API.
# Find a company's slug from their careers URL:
#   https://job-boards.greenhouse.io/<slug>
GREENHOUSE_BOARDS = [
    "orioninnovationnaukri",
]

# Company slugs on Lever's public API.
# Find a company's slug from their careers URL:
#   https://jobs.lever.co/<slug>
LEVER_BOARDS = [
    # "example-company",
]

INCLUDE_REMOTEOK = True
INCLUDE_WWR = True

TIMEOUT = 15  # seconds per request


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def matches_keywords(text):
    text = (text or "").lower()
    return any(k in text for k in KEYWORDS)


def matches_experience(text):
    if MIN_EXPERIENCE is None:
        return True
    text = (text or "").lower()
    years = [int(n) for n in re.findall(r"(\d+)\s*\+?\s*(?:years|yrs)", text)]
    if not years:
        return True  # don't discard postings that just don't state a number
    return any(MIN_EXPERIENCE <= y <= MAX_EXPERIENCE + 2 for y in years)


def within_freshness(posted_dt):
    if posted_dt is None:
        return True  # keep it if we can't tell — better a false positive than missed
    cutoff = datetime.now(timezone.utc) - timedelta(days=FRESHNESS_DAYS)
    return posted_dt >= cutoff


# ---------------------------------------------------------------------------
# source fetchers — each returns a list of dicts:
#   {title, company, location, url, posted (datetime or None), source}
# ---------------------------------------------------------------------------

def fetch_greenhouse(slug):
    results = []
    try:
        r = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        for job in r.json().get("jobs", []):
            title = job.get("title", "")
            content = job.get("content", "")
            if not (matches_keywords(title) or matches_keywords(content)):
                continue
            if not matches_experience(content):
                continue
            updated = job.get("updated_at")
            posted_dt = None
            if updated:
                try:
                    posted_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                except ValueError:
                    pass
            if not within_freshness(posted_dt):
                continue
            loc = (job.get("location") or {}).get("name", "")
            results.append({
                "title": title, "company": slug, "location": loc,
                "url": job.get("absolute_url", ""), "posted": posted_dt,
                "source": f"Greenhouse ({slug})",
            })
    except requests.RequestException as e:
        print(f"[warn] Greenhouse fetch failed for {slug}: {e}")
    return results


def fetch_lever(slug):
    results = []
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=TIMEOUT)
        r.raise_for_status()
        for job in r.json():
            title = job.get("text", "")
            desc = job.get("descriptionPlain", "") or job.get("description", "")
            if not (matches_keywords(title) or matches_keywords(desc)):
                continue
            if not matches_experience(desc):
                continue
            created_ms = job.get("createdAt")
            posted_dt = (
                datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                if created_ms else None
            )
            if not within_freshness(posted_dt):
                continue
            loc = (job.get("categories") or {}).get("location", "")
            results.append({
                "title": title, "company": slug, "location": loc,
                "url": job.get("hostedUrl", ""), "posted": posted_dt,
                "source": f"Lever ({slug})",
            })
    except requests.RequestException as e:
        print(f"[warn] Lever fetch failed for {slug}: {e}")
    return results


def fetch_remoteok():
    results = []
    try:
        r = requests.get("https://remoteok.com/api", timeout=TIMEOUT,
                          headers={"User-Agent": "Mozilla/5.0 (job-alert-script)"})
        r.raise_for_status()
        for job in r.json():
            if not isinstance(job, dict) or "position" not in job:
                continue  # first element is metadata, skip it
            title = job.get("position", "")
            tags = " ".join(job.get("tags", []))
            desc = job.get("description", "")
            haystack = f"{title} {tags} {desc}"
            if not matches_keywords(haystack):
                continue
            if not matches_experience(desc):
                continue
            posted_dt = None
            if job.get("date"):
                try:
                    posted_dt = datetime.fromisoformat(job["date"].replace("Z", "+00:00"))
                except ValueError:
                    pass
            if not within_freshness(posted_dt):
                continue
            results.append({
                "title": title, "company": job.get("company", ""),
                "location": job.get("location", "Remote"),
                "url": job.get("url", ""), "posted": posted_dt,
                "source": "RemoteOK",
            })
    except requests.RequestException as e:
        print(f"[warn] RemoteOK fetch failed: {e}")
    return results


def fetch_wwr():
    results = []
    if not INCLUDE_WWR:
        return results
    try:
        r = requests.get(
            "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        root = ElementTree.fromstring(r.content)
        for item in root.iter("item"):
            title = (item.findtext("title") or "")
            link = (item.findtext("link") or "")
            desc = (item.findtext("description") or "")
            if not (matches_keywords(title) or matches_keywords(desc)):
                continue
            pub_date_raw = item.findtext("pubDate")
            posted_dt = None
            if pub_date_raw:
                try:
                    from email.utils import parsedate_to_datetime
                    posted_dt = parsedate_to_datetime(pub_date_raw)
                except (TypeError, ValueError):
                    pass
            if not within_freshness(posted_dt):
                continue
            results.append({
                "title": title, "company": "", "location": "Remote",
                "url": link, "posted": posted_dt, "source": "We Work Remotely",
            })
    except (requests.RequestException, ElementTree.ParseError) as e:
        print(f"[warn] We Work Remotely fetch failed: {e}")
    return results


# ---------------------------------------------------------------------------
# email
# ---------------------------------------------------------------------------

def build_email_html(jobs):
    if not jobs:
        return "<p>No fresh matching postings today. Nothing to send, but the script ran successfully.</p>"
    rows = []
    for j in jobs:
        posted_str = j["posted"].strftime("%Y-%m-%d %H:%M UTC") if j["posted"] else "date unknown"
        rows.append(f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee;">
            <a href="{j['url']}"><strong>{j['title']}</strong></a><br>
            <span style="color:#666;font-size:13px;">{j['company']} · {j['location']}</span><br>
            <span style="color:#999;font-size:12px;">{j['source']} · {posted_str}</span>
          </td>
        </tr>""")
    return f"""
    <h2>Your DevOps / Boomi job digest — {datetime.now().strftime('%Y-%m-%d')}</h2>
    <p>{len(jobs)} fresh posting(s) matched your keywords in the last {FRESHNESS_DAYS} day(s).</p>
    <table style="border-collapse:collapse;width:100%;max-width:640px;">{''.join(rows)}</table>
    <p style="color:#999;font-size:12px;margin-top:20px;">
      This only covers sources with public APIs (Greenhouse, Lever, RemoteOK, We Work Remotely).
      Keep your native LinkedIn/Naukri daily alerts turned on too — this script can't cover
      those legally, since they don't offer a public API.
    </p>
    """


def send_email(html_body):
    from_addr = os.environ["EMAIL_ADDRESS"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]
    to_addr = os.environ.get("TO_EMAIL", from_addr)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"DevOps/Boomi job digest — {datetime.now().strftime('%d %b %Y')}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(from_addr, app_password)
        server.sendmail(from_addr, to_addr, msg.as_string())


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    all_jobs = []
    for slug in GREENHOUSE_BOARDS:
        all_jobs.extend(fetch_greenhouse(slug))
    for slug in LEVER_BOARDS:
        all_jobs.extend(fetch_lever(slug))
    if INCLUDE_REMOTEOK:
        all_jobs.extend(fetch_remoteok())
    all_jobs.extend(fetch_wwr())

    # de-dupe by URL, sort newest first
    seen = set()
    unique_jobs = []
    for j in all_jobs:
        if j["url"] in seen:
            continue
        seen.add(j["url"])
        unique_jobs.append(j)
    unique_jobs.sort(key=lambda j: j["posted"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    print(f"Found {len(unique_jobs)} fresh matching job(s).")
    html = build_email_html(unique_jobs)
    send_email(html)
    print("Email sent.")


if __name__ == "__main__":
    main()
