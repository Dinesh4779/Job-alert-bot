import os
import time
import html
import re
import requests
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
USERS = f"{SUPABASE_URL}/rest/v1/users"
SENT = f"{SUPABASE_URL}/rest/v1/sent_jobs"
H = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
DEFAULT_KEYWORDS = ["data analyst", "data analytics", "sql analyst",
                    "business analyst", "software engineer",
                    "software developer", "frontend developer",
                    "backend developer", "full stack developer",
                    "qa engineer"]
DEFAULT_LOCATION = "bangalore"
MAX_PER_ALERT = 10
KEEP_DAYS = 30
ADZUNA_CACHE_URL = f"{SUPABASE_URL}/rest/v1/adzuna_cache"
ADZUNA_CACHE_MINUTES = 60


def get_users():
    r = requests.get(USERS, headers=H, timeout=20, params={
        "active": "eq.true", "select": "chat_id,keywords,location"})
    r.raise_for_status()
    return r.json()


def deactivate(chat_id):
    requests.patch(USERS, headers=H, timeout=20,
                   params={"chat_id": f"eq.{chat_id}"}, json={"active": False})


def already_sent(chat_id, ids):
    if not ids:
        return set()
    r = requests.get(SENT, headers=H, timeout=20, params={
        "chat_id": f"eq.{chat_id}",
        "job_id": "in.(" + ",".join(ids) + ")",
        "select": "job_id"})
    r.raise_for_status()
    return {row["job_id"] for row in r.json()}


def mark_sent(chat_id, ids):
    rows = [{"chat_id": chat_id, "job_id": i} for i in ids]
    h = {**H, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    requests.post(SENT, headers=h, timeout=20, json=rows)


def prune():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    requests.delete(SENT, headers=H, timeout=20,
                    params={"sent_at": f"lt.{cutoff}"})

EXP_RANGE = re.compile(r'(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*\+?\s*(?:yrs?|years?)', re.I)
EXP_PLUS = re.compile(r'(\d{1,2})\s*\+\s*(?:yrs?|years?)', re.I)
EXP_MIN = re.compile(r'(?:minimum|min\.?|at least)\s*(\d{1,2})\s*(?:yrs?|years?)', re.I)
EXP_SINGLE = re.compile(r'(\d{1,2})\s*(?:yrs?|years?)', re.I)
BUCKET_ORDER = ["0-2 yrs experience", "2-4 yrs experience",
                "4-6 yrs experience", "6+ yrs experience",
                "Experience not specified"]


def experience_bucket(text):
    if not text:
        return BUCKET_ORDER[-1]
    m = EXP_RANGE.search(text) or EXP_PLUS.search(text) or \
        EXP_MIN.search(text) or EXP_SINGLE.search(text)
    if not m:
        return BUCKET_ORDER[-1]
    lo = int(m.group(1))
    if lo <= 2:
        return BUCKET_ORDER[0]
    if lo <= 4:
        return BUCKET_ORDER[1]
    if lo <= 6:
        return BUCKET_ORDER[2]
    return BUCKET_ORDER[3]


def fetch_remoteok():
    r = requests.get("https://remoteok.com/api",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    cutoff = datetime.now(timezone.utc) - timedelta(days=21)
    jobs = []
    for j in r.json()[1:]:
        jid = str(j.get("id") or "")
        if not jid.isalnum():
            continue
        posted = j.get("date") or ""
        try:
            posted_dt = datetime.fromisoformat(posted.replace("Z", "+00:00"))
        except ValueError:
            continue
        if posted_dt < cutoff:
            continue
        title = j.get("position") or ""
        desc = re.sub(r"<[^>]+>", " ", j.get("description") or "")
        jobs.append({
            "id": f"ro_{jid}",
            "title": title.lower(),
            "posted": posted_dt,
            "bucket": experience_bucket(f"{title} {desc}"),
            "text": f"<b>{html.escape(title)}</b>\n"
                    f"{html.escape(j.get('company') or '')}\n{j.get('url', '')}"})
    jobs.sort(key=lambda j: j["posted"], reverse=True)
    return jobs


def get_adzuna_cached(location):
    now = datetime.now(timezone.utc)
    r = requests.get(ADZUNA_CACHE_URL, headers=H, timeout=15, params={
        "location": f"eq.{location}", "select": "jobs,fetched_at"})
    if r.ok and r.json():
        row = r.json()[0]
        fetched_at = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
        if now - fetched_at < timedelta(minutes=ADZUNA_CACHE_MINUTES):
            return row["jobs"]
    jobs = fetch_adzuna(location)
    payload = {"location": location, "jobs": jobs,
               "fetched_at": now.isoformat()}
    h = {**H, "Prefer": "resolution=merge-duplicates,return=minimal"}
    try:
        requests.post(ADZUNA_CACHE_URL, headers=h, json=payload, timeout=15)
    except requests.RequestException:
        pass
    return jobs


def fetch_adzuna(location):
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        return []
    jobs, seen_ids = [], set()
    for query in ["data analyst", "business analyst", "software engineer",
                   "frontend developer", "qa engineer"]:
        try:
            r = requests.get(
                "https://api.adzuna.com/v1/api/jobs/in/search/1",
                params={"app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
                        "results_per_page": 20, "what": query,
                        "where": location, "content-type": "application/json"},
                timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            print("adzuna error:", type(e).__name__)
            continue
        for j in r.json().get("results", []):
            jid = str(j.get("id") or "")
            if not jid.isalnum() or jid in seen_ids:
                continue
            seen_ids.add(jid)
            title = j.get("title") or ""
            company = (j.get("company") or {}).get("display_name") or ""
            desc = j.get("description") or ""
            jobs.append({
                "id": f"az_{location}_{jid}",
                "title": title.lower(),
                "bucket": experience_bucket(f"{title} {desc}"),
                "text": f"<b>{html.escape(title)}</b>\n"
                        f"{html.escape(company)}\n{j.get('redirect_url', '')}"})
    return jobs


def send(chat_id, text):
    r = requests.post(f"{TG}/sendMessage", timeout=20, json={
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True})
    if r.status_code == 403:
        deactivate(chat_id)
    return r.ok


def main():
    users = get_users()
    base_jobs = fetch_remoteok()
    adzuna_cache = {}
    delivered = 0
    for u in users:
        cid = u["chat_id"]
        kws = u.get("keywords") or DEFAULT_KEYWORDS
        locs = [l.strip() for l in (u.get("location") or DEFAULT_LOCATION).split(",") if l.strip()]
        for loc in locs:
            if loc not in adzuna_cache:
                adzuna_cache[loc] = get_adzuna_cached(loc)
        seen_text = set()
        per_loc = [base_jobs]
        for loc in locs:
            per_loc.append(adzuna_cache[loc])
        jobs, i = [], 0
        while any(i < len(lst) for lst in per_loc):
            for lst in per_loc:
                if i < len(lst):
                    j = lst[i]
                    if j["text"] not in seen_text:
                        seen_text.add(j["text"])
                        jobs.append(j)
            i += 1
        if not jobs:
            continue
        all_ids = [j["id"] for j in jobs]
        seen = already_sent(cid, all_ids)
        matched = [j for j in jobs if j["id"] not in seen
                   and any(k in j["title"] for k in kws)]
        per_source, fresh, counts = 5, [], {}
        for j in matched:
            src = j["id"].split("_", 1)[0]
            if counts.get(src, 0) < per_source:
                fresh.append(j)
                counts[src] = counts.get(src, 0) + 1
            if len(fresh) >= MAX_PER_ALERT:
                break
        if not fresh:
            continue
        sections = []
        for b in BUCKET_ORDER:
            items = [j for j in fresh if j["bucket"] == b]
            if items:
                sections.append(f"<b>— {b} —</b>\n\n"
                                 + "\n\n".join(j["text"] for j in items))
        text = f"Fresh matching jobs ({len(fresh)}):\n\n" + "\n\n".join(sections)
        if any(j["id"].startswith("az_") for j in fresh):
            text += '\n\n<a href="https://www.adzuna.in">Jobs by Adzuna</a>'
        if send(cid, text):
            mark_sent(cid, [j["id"] for j in fresh])
            delivered += 1
        time.sleep(0.1)
    prune()
    print(f"users={len(users)} alerts_sent={delivered}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("fatal error:", type(e).__name__, str(e))
        try:
            requests.post(f"{TG}/sendMessage", timeout=15, json={
                "chat_id": next(iter(get_users()), {}).get("chat_id", ""),
                "text": f"Job alert run failed: {type(e).__name__}"})
        except Exception:
            pass
        raise
