import os
import html
import requests
import time
import re
from datetime import datetime, timedelta, timezone
from flask import Flask, request

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB = f"{SUPABASE_URL}/rest/v1/users"
DB_HEADERS = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
DEFAULT_KEYWORDS = ["data analyst", "data analytics", "sql analyst",
                    "business analyst", "software engineer",
                    "software developer", "frontend developer",
                    "backend developer", "full stack developer",
                    "qa engineer"]
DEFAULT_LOCATION = "bangalore"
CACHE_SECONDS = 600
_cache = {}

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
        "chat_id": f"eq.{chat_id}", "select": "keywords,active,location"})
    rows = r.json() if r.ok else []
    return rows[0] if rows else None

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


def fetch_all_jobs(location):
    jobs = []
    r = requests.get("https://remoteok.com/api",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    cutoff = datetime.now(timezone.utc) - timedelta(days=21)
    recent = []
    for j in r.json()[1:]:
        posted = j.get("date") or ""
        try:
            posted_dt = datetime.fromisoformat(posted.replace("Z", "+00:00"))
        except ValueError:
            continue
        if posted_dt < cutoff:
            continue
        title = j.get("position") or ""
        desc = re.sub(r"<[^>]+>", " ", j.get("description") or "")
        recent.append({
            "title": title.lower(),
            "posted": posted_dt,
            "bucket": experience_bucket(f"{title} {desc}"),
            "source": "remoteok",
            "text": f"<b>{html.escape(title)}</b>\n"
                    f"{html.escape(j.get('company') or '')}\n{j.get('url', '')}"})
    recent.sort(key=lambda j: j["posted"], reverse=True)
    jobs += recent

    if ADZUNA_APP_ID and ADZUNA_APP_KEY:
        for kw in ["data analyst", "business analyst", "software engineer",
                       "frontend developer", "qa engineer"]:
            try:
                r = requests.get(
                    "https://api.adzuna.com/v1/api/jobs/in/search/1",
                    params={"app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
                            "results_per_page": 15, "what": kw,
                            "where": location, "content-type": "application/json"},
                    timeout=20)
                r.raise_for_status()
            except requests.RequestException:
                continue
            for j in r.json().get("results", []):
                title = j.get("title") or ""
                company = (j.get("company") or {}).get("display_name") or ""
                desc = j.get("description") or ""
                jobs.append({
                    "title": title.lower(),
                    "bucket": experience_bucket(f"{title} {desc}"),
                    "source": "adzuna",
                    "text": f"<b>{html.escape(title)}</b>\n"
                            f"{html.escape(company)}\n{j.get('redirect_url', '')}"})
    return jobs


def get_cached_jobs(location):
    now = time.time()
    entry = _cache.get(location)
    if entry is None or now - entry["time"] > CACHE_SECONDS:
        entry = {"jobs": fetch_all_jobs(location), "time": now}
        _cache[location] = entry
    return entry["jobs"]


def fetch_jobs(keywords, locations):
    seen = set()
    per_loc = []
    for loc in locations:
        loc_matched = []
        for j in get_cached_jobs(loc):
            if j["text"] in seen:
                continue
            if any(k in j["title"] for k in keywords):
                seen.add(j["text"])
                loc_matched.append(j)
        per_loc.append(loc_matched)

    matched, i = [], 0
    while len(matched) < 10 and any(i < len(lst) for lst in per_loc):
        for lst in per_loc:
            if i < len(lst) and len(matched) < 10:
                matched.append(lst[i])
        i += 1

    all_jobs = [j for lst in per_loc for j in lst]
    sections = []
    for b in BUCKET_ORDER:
        items = [j for j in matched if j["bucket"] == b]
        if items:
            sections.append(f"<b>— {b} —</b>\n\n"
                             + "\n\n".join(j["text"] for j in items))
    if any(j.get("source") == "adzuna" for j in matched):
        sections.append('<a href="https://www.adzuna.in">Jobs by Adzuna</a>')
    return sections, all_jobs


HELP = ("<b>Commands</b>\n\n"
        "🔎 /jobs — get matching jobs now\n\n"
        "🏷 /roles — view or set your job roles\n"
        "   e.g. /roles data analyst, sql\n"
        "   /roles clear — reset to defaults\n\n"
        "📍 /location — view or set your job locations (up to 3)\n"
        "   e.g. /location bangalore, mumbai\n"
        "   /location clear — reset to default\n\n"
        "💬 /suggest your idea — send feedback to the creator\n\n"
        "⏸ /stop — pause alerts\n"
        "▶️ /start — resume alerts")


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
    elif cmd in ("/roles", "/keywords"):
        if not arg:
            user = db_get(chat_id)
            kws = (user or {}).get("keywords") or DEFAULT_KEYWORDS
            send(chat_id, "Your job roles: " + html.escape(", ".join(kws)))
        elif arg.lower() == "clear":
            db_upsert(chat_id, keywords=[])
            send(chat_id, "Job roles cleared. Using defaults.")
        else:
            kws = [k.strip().lower()[:40] for k in arg.split(",")
                   if len(k.strip()) >= 3][:10]
            if not kws:
                send(chat_id, "Each job role needs at least 3 letters. Try again, e.g.\n/roles data analyst, sql")
            else:
                db_upsert(chat_id, keywords=kws)
                send(chat_id, "Saved: " + html.escape(", ".join(kws)))
    elif cmd == "/location":
        if not arg:
            user = db_get(chat_id)
            locs = (user or {}).get("location") or DEFAULT_LOCATION
            send(chat_id, f"Your locations: {html.escape(locs)}\nChange with /location bangalore, mumbai (up to 3)")
        elif arg.lower() == "clear":
            db_upsert(chat_id, location=DEFAULT_LOCATION)
            send(chat_id, f"Location reset to default: {DEFAULT_LOCATION}")
        else:
            locs = [l.strip().lower()[:40] for l in arg.split(",") if l.strip()][:3]
            if not locs:
                send(chat_id, "Add at least one location, e.g.\n/location bangalore, mumbai")
            else:
                db_upsert(chat_id, location=", ".join(locs))
                send(chat_id, "Locations set to: " + html.escape(", ".join(locs)))
    elif cmd == "/suggest":
        if not arg:
            send(chat_id, "Add your suggestion after the command, e.g.\n/suggest add LinkedIn jobs")
        else:
            if OWNER_CHAT_ID:
                send(OWNER_CHAT_ID, f"Suggestion from {chat_id}:\n{arg[:500]}")
            send(chat_id, "Thanks! Your suggestion was sent.")
    elif cmd == "/jobs":
        user = db_get(chat_id)
        kws = (user or {}).get("keywords") or DEFAULT_KEYWORDS
        locs = [l.strip() for l in ((user or {}).get("location") or DEFAULT_LOCATION).split(",") if l.strip()]
        sections, jobs = fetch_jobs(kws, locs)
        if sections:
            send(chat_id, "Matching jobs:\n\n" + "\n\n".join(sections))
        elif not jobs:
            send(chat_id, "Couldn't fetch jobs right now, the job sites may be temporarily unavailable. Try again in a few minutes.")
        else:
            send(chat_id, "No matching jobs right now. Try /roles with other titles.")
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
            if OWNER_CHAT_ID:
                try:
                    send(OWNER_CHAT_ID, f"Bot error: {type(e).__name__} in handle()")
                except Exception:
                    pass
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
