import os
import time
import html
from datetime import datetime, timedelta, timezone
import requests

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
                    "software engineer", "software developer"]
MAX_PER_ALERT = 10
KEEP_DAYS = 30


def get_users():
    r = requests.get(USERS, headers=H, timeout=20, params={
        "active": "eq.true", "select": "chat_id,keywords"})
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
        jobs.append({
            "id": f"ro_{jid}",
            "title": title.lower(),
            "posted": posted_dt,
            "text": f"<b>{html.escape(title)}</b>\n"
                    f"{html.escape(j.get('company') or '')}\n{j.get('url', '')}"})
    jobs.sort(key=lambda j: j["posted"], reverse=True)
    return jobs


def fetch_adzuna():
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        return []
    jobs, seen_ids = [], set()
    for query in ["data analyst", "software engineer", "software developer"]:
        try:
            r = requests.get(
                "https://api.adzuna.com/v1/api/jobs/in/search/1",
                params={"app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
                        "results_per_page": 20, "what": query,
                        "where": "bangalore", "content-type": "application/json"},
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
            jobs.append({
                "id": f"az_{jid}",
                "title": title.lower(),
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
    jobs = fetch_remoteok() + fetch_adzuna()
    if not jobs:
        print("no jobs fetched")
        return
    all_ids = [j["id"] for j in jobs]
    delivered = 0
    for u in users:
        cid = u["chat_id"]
        kws = u.get("keywords") or DEFAULT_KEYWORDS
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
        text = (f"Fresh matching jobs ({len(fresh)}):\n\n"
                + "\n\n".join(j["text"] for j in fresh))
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
