"""Fetch fresher-friendly jobs with JobSpy (LinkedIn, Indeed, Naukri) and save
them to the Supabase table `scraped_jobs`."""
import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from jobspy import scrape_jobs

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")

# ---------------- settings you can change ----------------
ROLES = ["data analyst", "business analyst", "software developer",
         "software engineer", "qa engineer", "frontend developer",
         "backend developer", "full stack developer"]
# bot location key -> city name to search (keys must match your bot's city names)
LOCATIONS = {"bangalore": "Bengaluru", "hyderabad": "Hyderabad",
             "mumbai": "Mumbai"}
SITES = ["linkedin", "indeed", "naukri"]
RESULTS_PER_SEARCH = 30
MAX_AGE_HOURS = 336      # 14 days
MAX_EXP_YEARS = 1        # drop jobs that clearly ask for more than this
PAUSE_SECONDS = 6        # wait between searches (helps avoid blocks)
# ---------------------------------------------------------

SENIOR = re.compile(
    r"\b(senior|sr|lead|principal|staff|manager|head|director|architect)\b",
    re.I)
FRESHER = re.compile(
    r"\b(fresher|freshers|intern|internship|trainee|graduate|entry[- ]level|"
    r"junior|jr)\b", re.I)
EXP_IN_TEXT = re.compile(
    r"(\d{1,2})\s*(?:\+|-|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)"
    r"[^.\n]{0,40}exp(?:erience)?", re.I)
FIRST_NUMBER = re.compile(r"(\d{1,2})")
TITLE_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|-|to)\s*\d{0,2}\s*(?:years?|yrs?)", re.I)
# LinkedIn's own seniority label -> years we treat it as
LEVEL_TO_YEARS = {"internship": 0, "entry level": 0, "mid-senior level": 5,
                  "director": 8, "executive": 10}


def clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def experience_min(row, title):
    """Return the minimum years asked, or None when unknown."""
    naukri = clean(row.get("experience_range"))
    match = FIRST_NUMBER.search(naukri)
    if match:
        return int(match.group(1))
    level = clean(row.get("job_level")).lower()
    if level in LEVEL_TO_YEARS:
        return LEVEL_TO_YEARS[level]
    match = TITLE_YEARS.search(title)
    if match:
        return int(match.group(1))
    if FRESHER.search(title):
        return 0
    match = EXP_IN_TEXT.search(clean(row.get("description"))[:5000])
    if match:
        return int(match.group(1))
    return None


def posted_iso(value):
    stamp = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(stamp) else stamp.isoformat()


def supabase_headers(extra=None):
    headers = {"apikey": SUPABASE_KEY,
               "Authorization": f"Bearer {SUPABASE_KEY}",
               "Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    return headers


def notify(text):
    """Send a Telegram message to the owner (never includes secrets)."""
    if not BOT_TOKEN or not OWNER_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": OWNER_CHAT_ID, "text": text},
                      timeout=15)
    except requests.RequestException:
        pass


def save(rows):
    for start in range(0, len(rows), 100):
        response = requests.post(
            f"{SUPABASE_URL}/rest/v1/scraped_jobs?on_conflict=id",
            headers=supabase_headers({"Prefer": "resolution=merge-duplicates"}),
            json=rows[start:start + 100], timeout=30)
        response.raise_for_status()


def cleanup():
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=MAX_AGE_HOURS)).isoformat()
    for column in ("posted", "fetched_at"):
        try:
            requests.delete(f"{SUPABASE_URL}/rest/v1/scraped_jobs",
                            headers=supabase_headers(),
                            params={column: f"lt.{cutoff}"}, timeout=30)
        except requests.RequestException as e:
            print("cleanup error:", column, type(e).__name__)


def main():
    rows = {}
    seen_pairs = set()
    stats = {"searches": 0, "failed": 0, "fetched": 0, "senior": 0,
             "too_much_exp": 0, "dup": 0, "kept": 0}
    per_site = {site: 0 for site in SITES}

    for key, city in LOCATIONS.items():
        for role in ROLES:
            for site in SITES:
                stats["searches"] += 1
                try:
                    df = scrape_jobs(
                        site_name=[site], search_term=role, location=city,
                        results_wanted=RESULTS_PER_SEARCH,
                        hours_old=MAX_AGE_HOURS, country_indeed="India",
                        fetch_description=(site == "linkedin"),
                        verbose=0)
                except Exception as e:
                    stats["failed"] += 1
                    print(f"FAILED {site} / {role}: {type(e).__name__}: "
                          f"{str(e)[:150]}")
                    time.sleep(PAUSE_SECONDS)
                    continue

                for _, row in df.iterrows():
                    stats["fetched"] += 1
                    title = clean(row.get("title"))
                    url = clean(row.get("job_url"))
                    if not title or not url:
                        continue
                    if SENIOR.search(title):
                        stats["senior"] += 1
                        continue
                    exp_min = experience_min(row, title)
                    if exp_min is not None and exp_min > MAX_EXP_YEARS:
                        stats["too_much_exp"] += 1
                        continue
                    company = clean(row.get("company"))
                    pair = (title.lower(), company.lower(), key)
                    if pair in seen_pairs:
                        stats["dup"] += 1
                        continue
                    seen_pairs.add(pair)
                    job_id = "js_" + hashlib.md5(f"{url}|{key}".encode()).hexdigest()[:16]
                    rows[job_id] = {
                        "id": job_id, "title": title, "company": company,
                        "url": url, "exp_min": exp_min,
                        "posted": posted_iso(row.get("date_posted")),
                        "search_location": key, "source": site,
                        "role": role,
                    }
                    per_site[site] += 1
                    stats["kept"] += 1
                time.sleep(PAUSE_SECONDS)

    if rows:
        save(list(rows.values()))
        cleanup()
    print("summary:", stats)
    print("kept per site:", per_site)
    if not rows or stats["failed"] * 2 >= stats["searches"]:
        print("WARNING: many searches failed or nothing was saved.")
        notify(f"JobSpy run problem: {stats['failed']} of {stats['searches']} "
               f"searches failed, {stats['kept']} jobs saved.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify(f"JobSpy run crashed: {type(e).__name__}")
        raise
