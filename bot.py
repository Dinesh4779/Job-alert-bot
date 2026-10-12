import os
import html
import requests
import time
import re
import traceback
import hashlib
from datetime import datetime, timedelta, timezone
from flask import Flask, request

def parse_dt(s):
    try:
        dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")
JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY")
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
LOCATION_ALIASES = {
    "banglore": "bangalore", "bengaluru": "bangalore", "blr": "bangalore",
    "bombay": "mumbai",
    "new delhi": "delhi", "ncr": "delhi",
    "gurugram": "gurgaon",
    "hyd": "hyderabad",
    "madras": "chennai",
    "calcutta": "kolkata",
}


BASE_ROLES = ["data analyst", "business analyst", "software engineer",
              "software developer", "frontend developer", "backend developer",
              "full stack developer", "qa engineer"]
SEARCH_TERMS = BASE_ROLES + [f"{r} fresher" for r in BASE_ROLES[:4]]


def normalize_location(loc):
    key = loc.strip().lower()
    return LOCATION_ALIASES.get(key, key)


EXP_CODES = {"0-2": "0-2 yrs experience", "2-4": "2-4 yrs experience",
             "4-6": "4-6 yrs experience", "6+": "6+ yrs experience"}
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

EXP_RANGE = re.compile(
    r'(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*\+?\s*(?:yrs?|years?)'
    r'(?:\s+of)?\s*(?:relevant\s+)?experience', re.I)
EXP_PLUS = re.compile(
    r'(\d{1,2})\s*\+\s*(?:yrs?|years?)(?:\s+of)?\s*(?:relevant\s+)?experience', re.I)
EXP_MIN = re.compile(
    r'(?:minimum|min\.?|at least)\s*(\d{1,2})\s*(?:yrs?|years?)'
    r'(?:\s+of)?\s*(?:relevant\s+)?experience', re.I)
EXP_SINGLE_RANGE = re.compile(
    r'experience\s*(?:of|:)?\s*(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*'
    r'(?:yrs?|years?)', re.I)
EXP_SINGLE = re.compile(
    r'experience\s*(?:of|:)?\s*(\d{1,2})\s*(?:-|\+)?\s*(?:yrs?|years?)', re.I)
CURRENCY_NEAR = re.compile(r'(₹|rs\.?|inr|lpa|lakh|lacs?|ctc|salary)', re.I)
BUCKET_ORDER = ["0-2 yrs experience", "2-4 yrs experience",
                "4-6 yrs experience", "6+ yrs experience",
                "Experience not specified"]


def _bucket_for(lo):
    if lo < 2:
        return BUCKET_ORDER[0]
    if lo < 4:
        return BUCKET_ORDER[1]
    if lo < 6:
        return BUCKET_ORDER[2]
    return BUCKET_ORDER[3]


LEVEL_HINTS = [
    (re.compile(r'\b(senior|sr|lead|principal|staff|manager|head|director)\b', re.I), 3),
    (re.compile(r'\b(intern|internship|fresher|entry[- ]level|graduate|trainee|junior|jr)\b', re.I), 0),
    (re.compile(r'\b(associate|mid[- ]level|midweight|intermediate)\b', re.I), 1),
]


EXP_YEARS = re.compile(
    r'(\d{1,2})\s*(?:yrs?|years?)(?:\s+of)?\s*(?:relevant\s+|work\s+|professional\s+)?experience',
    re.I)


def experience_bucket(text):
    if not text:
        return BUCKET_ORDER[-1]
    window = text[:5000]
    for pattern in (EXP_RANGE, EXP_PLUS, EXP_MIN, EXP_SINGLE_RANGE, EXP_SINGLE):
        for m in pattern.finditer(window):
            start, end = m.span()
            context = window[max(0, start - 20):end + 20]
            if CURRENCY_NEAR.search(context):
                continue
            return _bucket_for(int(m.group(1)))
    for m in EXP_YEARS.finditer(window):
        context = window[max(0, m.start() - 20):m.end() + 20]
        if CURRENCY_NEAR.search(context):
            continue
        return _bucket_for(int(m.group(1)))
    for pattern, idx in LEVEL_HINTS:
        if pattern.search(window):
            return BUCKET_ORDER[idx]
    return BUCKET_ORDER[-1]


def fetch_jooble():
    if not JOOBLE_API_KEY:
        return []
    jobs = []
    for kw in SEARCH_TERMS:
        try:
            r = requests.post(
                f"https://jooble.org/api/{JOOBLE_API_KEY}",
                json={"keywords": kw, "location": "India"},
                timeout=20)
            r.raise_for_status()
        except requests.RequestException:
            continue
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        for j in r.json().get("jobs", []):
            updated = j.get("updated") or ""
            try:
                updated_dt = datetime.fromisoformat(updated[:19]).replace(tzinfo=timezone.utc)
                if updated_dt < cutoff:
                    continue
            except ValueError:
                pass
            title = j.get("title") or ""
            company = j.get("company") or ""
            snippet = j.get("snippet") or ""
            jobs.append({
                "title": title.lower(),
                "bucket": experience_bucket(f"{title} {snippet}"),
                "source": "jooble",
                "text": f"<b>{html.escape(title)}</b>\n"
                        f"{html.escape(company)}\n{j.get('link', '')}"})
    return jobs


def fetch_jobicy():
    jobs = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    try:
        r = requests.get("https://jobicy.com/api/v2/remote-jobs",
                         params={"count": 50}, timeout=20)
        r.raise_for_status()
        data = r.json().get("jobs", [])
    except (requests.RequestException, ValueError):
        return []
    for j in data:
        posted = j.get("pubDate") or ""
        try:
            posted_dt = datetime.fromisoformat(posted.replace("Z", "+00:00"))
            if posted_dt < cutoff:
                continue
        except ValueError:
            pass
        title = j.get("jobTitle") or ""
        company = j.get("companyName") or ""
        excerpt = j.get("jobExcerpt") or ""
        jobs.append({
            "id": f"jc_{hash(j.get('url', ''))}",
            "title": title.lower(),
            "bucket": experience_bucket(f"{title} {excerpt} {j.get('jobLevel', '')}"),
            "source": "jobicy",
            "text": f"<b>{html.escape(title)}</b>\n"
                    f"{html.escape(company)}\n{j.get('url', '')}"})
    return jobs


def fetch_remotive():
    jobs = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    try:
        r = requests.get("https://remotive.com/api/remote-jobs",
                         params={"limit": 50}, timeout=20)
        r.raise_for_status()
        data = r.json().get("jobs", [])
    except (requests.RequestException, ValueError):
        return []
    for j in data:
        posted = j.get("publication_date") or ""
        try:
            posted_dt = datetime.fromisoformat(posted.replace("Z", "+00:00"))
            if posted_dt.tzinfo is None:
                posted_dt = posted_dt.replace(tzinfo=timezone.utc)
            if posted_dt < cutoff:
                continue
        except ValueError:
            pass
        title = j.get("title") or ""
        company = j.get("company_name") or ""
        desc = re.sub(r"<[^>]+>", " ", j.get("description") or "")
        jobs.append({
            "id": hashlib.md5(j.get('url', '').encode()).hexdigest()[:12],
            "title": title.lower(),
            "bucket": experience_bucket(f"{title} {desc}"),
            "source": "remotive",
            "text": f"<b>{html.escape(title)}</b>\n"
                    f"{html.escape(company)}\n{j.get('url', '')}"})
    return jobs


def fetch_all_jobs(location):
    jobs = []
    r = requests.get("https://remoteok.com/api",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
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
        for kw in SEARCH_TERMS:
            try:
                r = requests.get(
                    "https://api.adzuna.com/v1/api/jobs/in/search/1",
                    params={"app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
                            "results_per_page": 15, "what": kw,
                            "where": location, "max_days_old": 14,
                            "content-type": "application/json"},
                    timeout=20)
                r.raise_for_status()
            except requests.RequestException as e:
                print("adzuna error:", kw, type(e).__name__, getattr(e.response, "status_code", ""))
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

def get_jooble_cached():
    now = time.time()
    entry = _cache.get("_jooble")
    if entry is None or now - entry["time"] > CACHE_SECONDS:
        entry = {"jobs": fetch_jooble(), "time": now}
        _cache["_jooble"] = entry
    return entry["jobs"]


def get_jobicy_cached():
    now = time.time()
    entry = _cache.get("_jobicy")
    if entry is None or now - entry["time"] > CACHE_SECONDS:
        entry = {"jobs": fetch_jobicy(), "time": now}
        _cache["_jobicy"] = entry
    return entry["jobs"]


def get_remotive_cached():
    now = time.time()
    entry = _cache.get("_remotive")
    if entry is None or now - entry["time"] > CACHE_SECONDS:
        entry = {"jobs": fetch_remotive(), "time": now}
        _cache["_remotive"] = entry
    return entry["jobs"]


_scraped_cache = {}


def fetch_scraped_jobs(location_key):
    base = os.environ.get("SUPABASE_URL")
    api_key = os.environ.get("SUPABASE_KEY")
    if not base or not api_key:
        return []
    try:
        r = requests.get(
            f"{base}/rest/v1/scraped_jobs",
            headers={"apikey": api_key, "Authorization": f"Bearer {api_key}"},
            params={"search_location": f"eq.{location_key}",
                    "order": "exp_min.asc.nullslast,posted.desc.nullslast",
                    "limit": "150"},
            timeout=15)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError):
        return []
    jobs = []
    for row in rows:
        title = row.get("title") or ""
        company = row.get("company") or ""
        url = row.get("url") or ""
        exp_min = row.get("exp_min")
        if exp_min is None:
            bucket = experience_bucket(title)
        else:
            bucket = _bucket_for(int(exp_min))
        match_title = title.lower()
        role = row.get("role") or ""
        if role and any(w in match_title for w in role.split() if len(w) > 2):
            match_title += " " + role
        jobs.append({
            "id": row["id"],
            "title": match_title,
            "bucket": bucket,
            "source": "scraped",
            "text": f"<b>{html.escape(title)}</b>\n{html.escape(company)}\n{url}",
        })
    return jobs


SCRAPED_CITY_ALIAS = {"pune": "mumbai", "noida": "delhi", "gurgaon": "delhi"}


def get_scraped_cached(location):
    now = time.time()
    key = (location or "").strip().lower()
    entry = _scraped_cache.get(key)
    if entry is None or now - entry["time"] > 600:
        entry = {"jobs": fetch_scraped_jobs(key), "time": now}
        _scraped_cache[key] = entry
    return entry["jobs"]


def get_cached_jobs(location):
    now = time.time()
    entry = _cache.get(location)
    if entry is None or now - entry["time"] > CACHE_SECONDS:
        jobs = list(fetch_all_jobs(location) or []) + list(get_jooble_cached() or []) + list(get_remotive_cached() or []) + list(get_scraped_cached(location) or []) 
        entry = {"jobs": jobs, "time": now}
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

    by_source = {}
    for lst in per_loc:
        for j in lst:
            by_source.setdefault(j.get("source", "remoteok"), []).append(j)

    matched, i = [], 0
    sources = list(by_source.keys())
    while len(matched) < 10 and any(i < len(by_source[s]) for s in sources):
        for s in sources:
            if i < len(by_source[s]) and len(matched) < 10:
                matched.append(by_source[s][i])
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
        "☕ /support — support the bot (optional)\n"
        "⏸ /stop — pause alerts\n"
        "▶️ /start — resume alerts")


def _sb_headers(extra=None):
    key = os.environ.get("SUPABASE_KEY", "")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if extra:
        headers.update(extra)
    return headers


def is_owner(chat_id):
    owner = os.environ.get("OWNER_CHAT_ID")
    return bool(owner) and str(chat_id) == str(owner)


def build_stats():
    base = os.environ.get("SUPABASE_URL", "")
    lines = ["Bot stats"]

    try:
        r = requests.get(
            f"{base}/rest/v1/users",
            headers=_sb_headers({"Prefer": "count=exact", "Range": "0-0"}),
            params={"select": "*"}, timeout=15)
        r.raise_for_status()
        total = r.headers.get("Content-Range", "").split("/")[-1]
        lines.append(f"Users: {total}")
    except (requests.RequestException, ValueError):
        lines.append("Users: could not read")

    try:
        r = requests.get(
            f"{base}/rest/v1/users", headers=_sb_headers(),
            params={"select": "location", "limit": "5000"}, timeout=15)
        r.raise_for_status()
        cities = {}
        for row in r.json():
            for city in (row.get("location") or "bangalore").split(","):
                city = city.strip().lower()
                if city:
                    cities[city] = cities.get(city, 0) + 1
        top = sorted(cities.items(), key=lambda x: -x[1])[:6]
        lines.append("User cities: " + ", ".join(f"{c} {n}" for c, n in top))
    except (requests.RequestException, ValueError):
        lines.append("User cities: could not read")

    try:
        r = requests.get(
            f"{base}/rest/v1/scraped_jobs", headers=_sb_headers(),
            params={"select": "search_location,fetched_at", "limit": "5000"},
            timeout=15)
        r.raise_for_status()
        per_city, newest = {}, ""
        for row in r.json():
            city = row.get("search_location") or "?"
            per_city[city] = per_city.get(city, 0) + 1
            newest = max(newest, row.get("fetched_at") or "")
        lines.append("Saved scraped jobs: " + ", ".join(
            f"{c} {n}" for c, n in sorted(per_city.items())))
        if newest:
            lines.append("Newest scraped job saved: "
                         + newest[:16].replace("T", " ") + " UTC")
        else:
            lines.append("Newest scraped job saved: none yet")
    except (requests.RequestException, ValueError):
        lines.append("Scraped jobs: could not read")

    return "\n".join(lines)


SUPPORT_TEXT = (
    "☕ <b>Support the bot</b>\n\n"
    "This bot is free and I build it in my spare time. If it helped you, "
    "you can chip in. It's completely optional.\n\n"
    "UPI: <code>dineshkodavati@ptyes</code>\n\n"
    "Thank you! You can also send ideas with /suggest."
)


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
    elif cmd == "/support":
        send(chat_id, SUPPORT_TEXT)
    elif cmd == "/stats":
        if is_owner(chat_id):
            send(chat_id, html.escape(build_stats()))
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
        locs = [normalize_location(l) for l in ((user or {}).get("location") or DEFAULT_LOCATION).split(",") if l.strip()]
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
            traceback.print_exc()
            if OWNER_CHAT_ID:
                try:
                    send(OWNER_CHAT_ID, f"Bot error: {type(e).__name__}: {html.escape(str(e))[:300]}")
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
