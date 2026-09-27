import os
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

KEYWORDS = ["data analyst", "data analytics", "sql analyst", "software engineer", "software developer"]

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})

def fetch_remoteok_jobs():
    resp = requests.get("https://remoteok.com/api", headers={"User-Agent": "Mozilla/5.0"})
    jobs = resp.json()[1:]  # first item is metadata
    matches = []
    for job in jobs:
        title = job.get("position", "").lower()
        if any(k in title for k in KEYWORDS):
            matches.append(f"<b>{job.get('position')}</b>\n{job.get('company')}\nhttps://remoteok.com{job.get('url')}")
    return matches

def main():
    jobs = fetch_remoteok_jobs()
    if not jobs:
        send_message("No new matching jobs this run.")
        return
    message = f"Fresh matching jobs ({len(jobs)}):\n\n" + "\n\n".join(jobs[:10])
    send_message(message)

if __name__ == "__main__":
    main()
