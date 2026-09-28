import os
import html
import requests
from flask import Flask, request

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB = f"{SUPABASE_URL}/rest/v1/users"
DB_HEADERS = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
DEFAULT_KEYWORDS = ["data analyst", "data analytics", "sql analyst",
                    "software engineer", "software developer"]

app = Flask(__name__)


def send(chat_id, text):
    requests.post(f"{TG}/sendMessage", json={
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True}, timeout=15)


def db_upsert(chat_id, **fields):
    row = {"chat_id": chat_id, **fields}
    headers = {**DB_HEADERS,
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    requests.post(DB, headers=headers, json=row, timeout=15)


def db_get(chat_id):
    r = requests.get(DB, headers=DB_HEADERS, timeout=15, params={
        "chat_id": f"eq.{chat_id}", "select": "keywords,active"})
    rows = r.json() if r.ok else []
    return rows[0] if rows else None


def fetch_jobs(keywords):
    r = requests.get("https://remoteok.com/api",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    found = []
    for j in r.json()[1:]:
        title = (j.get("position") or "").lower()
        if any(k in title for k in keywords):
            found.append(
                f"<b>{html.escape(j.get('position', ''))}</b>\n"
                f"{html.escape(j.get('company', ''))}\n{j.get('url', '')}")
    return found


HELP = ("Commands:\n"
        "/jobs - get matching jobs now\n"
        "/keywords data analyst, sql - set the job titles you want\n"
        "/keywords - see your current keywords\n"
        "/keywords clear - go back to defaults\n"
        "/stop - pause alerts\n"
        "/start - resume alerts")


def handle(chat_id, text):
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if cmd == "/start":
        db_upsert(chat_id, active=True)
        send(chat_id, "Welcome! I find fresh job listings for you.\n\n" + HELP)
    elif cmd == "/help":
        send(chat_id, HELP)
    elif cmd == "/stop":
        db_upsert(chat_id, active=False)
        send(chat_id, "Alerts paused. Send /start to resume.")
    elif cmd == "/keywords":
        if not arg:
            user = db_get(chat_id)
            kws = (user or {}).get("keywords") or DEFAULT_KEYWORDS
            send(chat_id, "Your keywords: " + html.escape(", ".join(kws)))
        elif arg.lower() == "clear":
            db_upsert(chat_id, keywords=[])
            send(chat_id, "Keywords cleared. Using defaults.")
        else:
            kws = [k.strip().lower()[:40] for k in arg.split(",") if k.strip()][:10]
            db_upsert(chat_id, keywords=kws)
            send(chat_id, "Saved: " + html.escape(", ".join(kws)))
    elif cmd == "/jobs":
        user = db_get(chat_id)
        kws = (user or {}).get("keywords") or DEFAULT_KEYWORDS
        jobs = fetch_jobs(kws)
        if jobs:
            send(chat_id, f"Matching jobs ({len(jobs)}):\n\n" + "\n\n".join(jobs[:10]))
        else:
            send(chat_id, "No matching jobs right now. Try /keywords with other titles.")
    else:
        send(chat_id, "I didn't get that.\n\n" + HELP)


@app.get("/")
def home():
    return "Bot is alive"


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "forbidden", 403
    msg = (request.get_json(silent=True) or {}).get("message") or {}
    chat = msg.get("chat") or {}
    if msg.get("text") and chat.get("type") == "private":
        try:
            handle(chat["id"], msg["text"])
        except Exception as e:
            print("error:", type(e).__name__)
    return "ok"


def set_webhook():
    if PUBLIC_URL:
        try:
            requests.post(f"{TG}/setWebhook", json={
                "url": f"{PUBLIC_URL}/webhook",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": ["message"]}, timeout=15)
        except Exception as e:
            print("webhook error:", type(e).__name__)


set_webhook()
