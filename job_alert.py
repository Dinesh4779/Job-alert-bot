import os
import time
import html
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB = f"{SUPABASE_URL}/rest/v1/users"
DB_HEADERS = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
DEFAULT_KEYWORDS = ["data analyst", "data analytics", "sql analyst",
                    "software engineer", "software developer"]


def get_users():
    r = requests.get(DB, headers=DB_HEADERS, timeout=20, params={
        "active": "eq.true", "select": "chat_id,keywords"})
    r.raise_for_status()
    return r.json()


def deactivate(chat_id):
    requests.patch(DB, headers=DB_HEADERS, timeout=20,
                   params={"chat_id": f"eq.{chat_id}"}, json={"active": False})


def fetch_jobs():
    r = requests.get("https://remoteok.com/api",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    jobs = []
    for j in r.json()[1:]:
        title = j.get("position") or ""
        jobs.append((title.lower(),
                     f"<b>{html.escape(title)}</b>\n"
                     f"{html.escape(j.get('company') or '')}\n{j.get('url', '')}"))
    return jobs


def send(chat_id, text):
    r = requests.post(f"{TG}/sendMessage", timeout=20, json={
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True})
    if r.status_code == 403:
        deactivate(chat_id)


def main():
    users = get_users()
    jobs = fetch_jobs()
    sent = 0
    for u in users:
        kws = u.get("keywords") or DEFAULT_KEYWORDS
        matches = [msg for title, msg in jobs if any(k in title for k in kws)]
        if matches:
            send(u["chat_id"], f"Fresh matching jobs ({len(matches)}):\n\n"
                 + "\n\n".join(matches[:10]))
            sent += 1
            time.sleep(0.1)
    print(f"users={len(users)} alerts_sent={sent}")


if __name__ == "__main__":
    main()
