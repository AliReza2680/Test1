"""Persian RSS news bot for Telegram. Python standard library only."""
import html
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = {x.strip() for x in os.environ.get("CHAT_ID", "").split(",") if x.strip()}
INTERVAL = max(5, int(os.environ.get("CHECK_INTERVAL_MIN", "15"))) * 60
MAX_ITEMS = max(1, int(os.environ.get("MAX_ITEMS_PER_ROUND", "12")))
PORT = int(os.environ.get("PORT", "10000"))
SOURCES = [
    ("بی‌بی‌سی فارسی", "https://feeds.bbci.co.uk/persian/rss.xml"),
    ("دویچه‌وله فارسی", "https://rss.dw.com/xml/rss-per-all"),
    ("یورونیوز فارسی", "https://parsi.euronews.com/rss"),
]
IRAN_WORDS = re.compile("|".join(map(re.escape, [
    "ایران", "تهران", "جمهوری اسلامی", "خامنه‌ای", "خامنه ای", "پزشکیان",
    "سپاه", "مجلس شورای", "عراقچی", "قالیباف", "اصفهان", "مشهد", "شیراز",
    "تبریز", "کرج", "اهواز", "خوزستان", "ریال", "تومان"
])))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("persian-news-bot")
STATE_FILE = os.path.join(os.environ.get("DATA_DIR", "data"), "state.json")
lock = threading.Lock()
subscribers = set(CHAT_IDS)
seen = set()

def load_state():
    global subscribers, seen
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        subscribers |= set(map(str, data.get("subscribers", [])))
        seen = set(data.get("seen", []))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("Could not load state")

def save_state():
    with lock:
        data = {"subscribers": sorted(subscribers), "seen": list(seen)[-5000:]}
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)

def request(url, payload=None, timeout=30):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "PersianNewsBot/1.0",
        "Content-Type": "application/json"
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()

def telegram(method, **kwargs):
    if not TOKEN:
        return {}
    try:
        raw = request(f"https://api.telegram.org/bot{TOKEN}/{method}", kwargs)
        return json.loads(raw.decode())
    except Exception:
        log.exception("Telegram API error: %s", method)
        return {}

def send(chat_id, text):
    return telegram("sendMessage", chat_id=chat_id, text=text,
                    parse_mode="HTML", disable_web_page_preview=True)

def clean(value):
    value = re.sub(r"<[^>]*>", " ", value or "")
    return html.unescape(re.sub(r"\s+", " ", value)).strip()

def fetch_news():
    found = []
    for name, url in SOURCES:
        try:
            root = ET.fromstring(request(url))
            for item in root.iter():
                if item.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
                    continue
                fields = {c.tag.rsplit("}", 1)[-1].lower(): c for c in item}
                title = clean(fields.get("title").text if fields.get("title") is not None else "")
                if not title:
                    continue
                link_el = fields.get("link")
                link = ""
                if link_el is not None:
                    link = (link_el.text or link_el.attrib.get("href", "")).strip()
                if not link:
                    continue
                key = link.split("?")[0].rstrip("/") or title
                section = "iran" if IRAN_WORDS.search(title) else "world"
                date_text = ""
                for k in ("pubdate", "published", "updated"):
                    if fields.get(k) is not None and fields[k].text:
                        date_text = fields[k].text
                        break
                try:
                    date = parsedate_to_datetime(date_text).astimezone(timezone.utc) if date_text else None
                except Exception:
                    date = None
                found.append((date or datetime.min.replace(tzinfo=timezone.utc), key, title, link, name, section))
        except Exception:
            log.exception("RSS source failed: %s", name)
    found.sort(reverse=True)
    unique, keys = [], set()
    for row in found:
        if row[1] not in keys:
            keys.add(row[1])
            unique.append(row)
    return unique

def format_news(items):
    blocks = []
    for section, heading in (("iran", "🇮🇷 <b>اخبار ایران</b>"), ("world", "🌍 <b>اخبار جهان</b>")):
        group = [x for x in items if x[5] == section]
        if not group:
            continue
        lines = [heading]
        for _, _, title, link, source, _ in group:
            lines.append(f"🔹 <b>{html.escape(title)}</b>\n<i>{html.escape(source)}</i> — <a href=\"{html.escape(link, quote=True)}\">ادامه خبر</a>")
        blocks.append("\n\n".join(lines))
    return blocks

def deliver(items, chats=None):
    chats = chats if chats is not None else list(subscribers)
    if not items:
        return
    for chat in chats:
        for message in format_news(items):
            send(chat, message)
            time.sleep(0.35)

def handle(update):
    msg = update.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat or not text.startswith("/"):
        return
    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/start":
        subscribers.add(chat)
        save_state()
        send(chat, "✅ ارسال خودکار اخبار فعال شد.\nدستورات: /news، /iran، /world، /sources، /stop")
    elif cmd == "/stop":
        if chat not in CHAT_IDS:
            subscribers.discard(chat)
            save_state()
        send(chat, "ارسال خودکار متوقف شد.")
    elif cmd in ("/news", "/iran", "/world"):
        send(chat, "در حال دریافت خبرها…")
        items = fetch_news()
        if cmd == "/iran":
            items = [x for x in items if x[5] == "iran"][:10]
        elif cmd == "/world":
            items = [x for x in items if x[5] == "world"][:10]
        else:
            items = items[:MAX_ITEMS]
        if items:
            deliver(items, [chat])
        else:
            send(chat, "فعلاً خبری دریافت نشد.")
    elif cmd == "/sources":
        send(chat, "منابع خبری:\n" + "\n".join("• " + html.escape(x[0]) for x in SOURCES))
    else:
        send(chat, "دستورات: /start، /stop، /news، /iran، /world، /sources")

def polling():
    offset = 0
    while True:
        try:
            result = telegram("getUpdates", offset=offset, timeout=50)
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle(update)
                except Exception:
                    log.exception("Update handler failed")
        except Exception:
            log.exception("Polling failed")
            time.sleep(5)

def news_loop():
    first = True
    while True:
        try:
            items = fetch_news()
            fresh = [x for x in items if x[1] not in seen]
            for item in fresh:
                seen.add(item[1])
            if first:
                first = False
            elif fresh:
                deliver(fresh[:MAX_ITEMS])
            save_state()
        except Exception:
            log.exception("News cycle failed")
        time.sleep(INTERVAL)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            body, status = b"ok", 200
        else:
            body, status = b"not found", 404
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        return

def main():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment.")
    load_state()
    telegram("setMyCommands", commands=[
        {"command": "news", "description": "تازه‌ترین خبرها"},
        {"command": "iran", "description": "اخبار ایران"},
        {"command": "world", "description": "اخبار جهان"},
        {"command": "sources", "description": "منابع خبری"},
        {"command": "start", "description": "فعال‌سازی ارسال خودکار"},
        {"command": "stop", "description": "توقف ارسال خودکار"},
    ])
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=polling, daemon=True).start()
    news_loop()

if __name__ == "__main__":
    main()
