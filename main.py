import os
import time
import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

VERSION = "CEO-BOT-V6-DASHBOARD"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

EXECUTOR = ThreadPoolExecutor(max_workers=3)
LOCK = threading.Lock()
CACHE = {}
RUNNING = set()

MARKETS_TTL = 15 * 60
FLIGHTS_TTL = 6 * 60 * 60
ALERTS_TTL = 30 * 60

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📌 داشبورد فوری"}],
        [{"text": "💰 ارز و طلا"}, {"text": "✈️ بلیط‌ها"}],
        [{"text": "🚨 فقط هشدارها"}],
        [{"text": "🔄 بروزرسانی"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "یک گزینه را انتخاب کن",
}

SYSTEM_PROMPT = """
You are a personal market-and-travel data analyst.
Write in Persian and optimize for a phone screen.
Be extremely concise, numerical and practical.
Never write an essay. Never dump raw URLs.
Never invent a number. If a current reliable value cannot be verified, write: N/A.
For Iran exchange rates, use free-market rates, not official rates, and label تومان and ریال clearly.
For flights, report only fares you can actually verify from current web results; otherwise N/A.
At the end list only 2-5 source names and a freshness timestamp.
"""


def now_label():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def build_prompt(kind: str) -> str:
    now = now_label()
    if kind == "markets":
        return f"""
Current time: {now}.
Find and return ONLY this compact dashboard, in this exact order:

💰 بازار — {now}
🥇 Gold spot: $X/oz | 24h: ±X%
🇺🇸🇨🇦 USD/CAD: X | 24h: ±X%
🇨🇦🇺🇸 CAD/USD: X
🇺🇸🇮🇷 USD/IRR free market: X تومان | X ریال | 24h: ±X%
🇨🇦🇮🇷 CAD/IRR free market: X تومان | X ریال
🇮🇷🥇 طلای 18 عیار ایران: X تومان/گرم | 24h: ±X%

📍 فقط اگر قابل اتکا بود: سکه امامی: X تومان

Rules:
- Use latest available values.
- Cross-check Iran free-market FX/gold with at least two credible market sources when possible.
- No economic commentary unless a move is unusually large; then add one short line beginning with ⚠️.
- Keep under 14 lines total.
"""
    if kind == "flights":
        return f"""
Current time: {now}.
Search current round-trip economy fares for one adult departing Montreal (YUL).
Use flexible dates in the next 90 days, trip length roughly 7-21 nights.
Return ONLY:

✈️ ارزان‌ترین بلیط‌های پیدا‌شده — {now}
🇮🇷 YUL ↔ Tehran (IKA): C$X | outbound date → return date | airline | stops
🇨🇦 YUL ↔ Vancouver (YVR): C$X | outbound date → return date | airline | nonstop/stops

If Tehran is not the cheapest practical Iran gateway, you may add one extra line for another major Iran airport only when meaningfully cheaper.
Do not give estimated fares. If a fare is not currently verifiable, put N/A.
Keep under 8 lines total.
"""
    if kind == "alerts":
        return f"""
Current time: {now}.
Give ONLY high-signal alerts affecting these items: global gold, USD/CAD, Iran free-market USD/IRR, Iran gold, Montreal-Iran airfare, Montreal-Vancouver airfare.
Maximum 4 alerts. Ignore routine news.
Each alert must be one short line and explain the practical implication.
If nothing material changed, write: ✅ فعلاً هشدار مهمی نیست.
"""
    raise ValueError(kind)


def http_json(url: str, method: str = "GET", payload=None, headers=None, timeout: int = 30):
    data = None
    request_headers = {"User-Agent": "Metra-CEO-Bot/6.0"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_output_text(response: dict) -> str:
    pieces = []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    pieces.append(text.strip())
    if pieces:
        return "\n".join(pieces)
    top = response.get("output_text")
    return top.strip() if isinstance(top, str) else ""


def call_openai(kind: str) -> str:
    payload = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "reasoning": {"effort": "low"},
        "max_output_tokens": 1600,
        "instructions": SYSTEM_PROMPT,
        "input": build_prompt(kind),
    }
    response = http_json(
        "https://api.openai.com/v1/responses",
        method="POST",
        payload=payload,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        timeout=100,
    )
    text = extract_output_text(response)
    if not text:
        raise RuntimeError("empty OpenAI response")
    return text


def telegram_call(method: str, payload=None, timeout: int = 30):
    return http_json(f"{TELEGRAM_API}/{method}", method="POST", payload=payload or {}, timeout=timeout)


def send_telegram(message: str, chat_id: str = TELEGRAM_CHAT_ID, menu: bool = False):
    chunks = [message[i:i + 3800] for i in range(0, len(message), 3800)] or [""]
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if menu and i == len(chunks) - 1:
            payload["reply_markup"] = MAIN_KEYBOARD
        telegram_call("sendMessage", payload, timeout=20)


def get_updates(offset=None):
    params = {"timeout": 25}
    if offset is not None:
        params["offset"] = offset
    return http_json(f"{TELEGRAM_API}/getUpdates?{urllib.parse.urlencode(params)}", timeout=35)


def ttl_for(kind: str) -> int:
    return {"markets": MARKETS_TTL, "flights": FLIGHTS_TTL, "alerts": ALERTS_TTL}[kind]


def cache_get(kind: str):
    with LOCK:
        item = CACHE.get(kind)
    if not item:
        return None, False
    return item, (time.time() - item["time"] <= ttl_for(kind))


def cache_put(kind: str, text: str):
    with LOCK:
        CACHE[kind] = {"text": text, "time": time.time()}


def refresh_job(kind: str, notify_chat=None):
    try:
        print(f"[{VERSION}] refresh start {kind}", flush=True)
        text = call_openai(kind)
        cache_put(kind, text)
        print(f"[{VERSION}] refresh done {kind}", flush=True)
        if notify_chat:
            send_telegram(text, notify_chat, menu=True)
    except Exception as exc:
        print(f"[{VERSION}] refresh error {kind}: {type(exc).__name__}: {exc}", flush=True)
        if notify_chat:
            send_telegram("⚠️ بروزرسانی کامل نشد؛ آخرین داده ذخیره‌شده همچنان قابل استفاده است.", notify_chat, menu=True)
    finally:
        with LOCK:
            RUNNING.discard(kind)


def start_refresh(kind: str, notify_chat=None) -> bool:
    with LOCK:
        if kind in RUNNING:
            return False
        RUNNING.add(kind)
    EXECUTOR.submit(refresh_job, kind, notify_chat)
    return True


def render_cached(kind: str, chat_id: str):
    item, fresh = cache_get(kind)
    if item:
        age = int((time.time() - item["time"]) / 60)
        suffix = "" if fresh else f"\n\n⏱ داده {age} دقیقه قبل است؛ بروزرسانی در پس‌زمینه شروع شد."
        send_telegram(item["text"] + suffix, chat_id, menu=True)
        if not fresh:
            start_refresh(kind)
        return
    start_refresh(kind, notify_chat=chat_id)
    send_telegram("⏳ اولین داده در حال آماده‌شدن است. نتیجه خودکار ارسال می‌شود؛ منو همچنان آزاد است.", chat_id, menu=True)


def render_dashboard(chat_id: str):
    m, _ = cache_get("markets")
    f, _ = cache_get("flights")
    a, _ = cache_get("alerts")
    if not any([m, f, a]):
        for kind in ("markets", "flights", "alerts"):
            start_refresh(kind)
        send_telegram("⏳ داشبورد اولیه در حال آماده‌شدن است. چند لحظه بعد دوباره «📌 داشبورد فوری» را بزن.", chat_id, menu=True)
        return
    parts = [x["text"] for x in (m, f, a) if x]
    send_telegram("\n\n".join(parts), chat_id, menu=True)
    for kind in ("markets", "flights", "alerts"):
        item, fresh = cache_get(kind)
        if not item or not fresh:
            start_refresh(kind)


def handle_message(message: dict):
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id:
        return
    if chat_id != TELEGRAM_CHAT_ID:
        send_telegram("این ربات خصوصی است.", chat_id)
        return

    raw = (message.get("text") or "").strip()
    low = raw.lower()

    if low in {"/start", "/help", "hello", "hi"}:
        send_telegram(
            "⚡ داشبورد شخصی بازار و سفر\n\nفقط قیمت‌ها، بلیت‌ها و هشدارهای مهم. گزارش طولانی حذف شده است.",
            chat_id,
            menu=True,
        )
    elif raw == "📌 داشبورد فوری" or low == "/dashboard":
        render_dashboard(chat_id)
    elif raw == "💰 ارز و طلا" or low == "/markets":
        render_cached("markets", chat_id)
    elif raw == "✈️ بلیط‌ها" or low == "/flights":
        render_cached("flights", chat_id)
    elif raw == "🚨 فقط هشدارها" or low == "/alerts":
        render_cached("alerts", chat_id)
    elif raw == "🔄 بروزرسانی" or low == "/refresh":
        started = 0
        for kind in ("markets", "flights", "alerts"):
            started += 1 if start_refresh(kind) else 0
        send_telegram(f"🔄 بروزرسانی در پس‌زمینه شروع شد ({started} بخش). منو قفل نمی‌شود.", chat_id, menu=True)
    else:
        send_telegram("یکی از دکمه‌های منو را انتخاب کن.", chat_id, menu=True)


def scheduler_loop():
    while True:
        try:
            for kind in ("markets", "flights", "alerts"):
                item, fresh = cache_get(kind)
                if not item or not fresh:
                    start_refresh(kind)
        except Exception as exc:
            print(f"[{VERSION}] scheduler error: {exc}", flush=True)
        time.sleep(60)


def polling_loop():
    print(f"[{VERSION}] polling started", flush=True)
    offset = None
    while True:
        try:
            data = get_updates(offset)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if msg:
                    handle_message(msg)
        except Exception as exc:
            print(f"[{VERSION}] telegram loop error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(2)


def startup():
    print(f"[{VERSION}] START", flush=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    for kind in ("markets", "flights", "alerts"):
        start_refresh(kind)
    try:
        send_telegram("✅ نسخه V6 فعال شد — داشبورد سریع و پالایش‌شده آماده است.", TELEGRAM_CHAT_ID, menu=True)
    except Exception as exc:
        print(f"[{VERSION}] startup telegram error: {type(exc).__name__}: {exc}", flush=True)
    polling_loop()


if __name__ == "__main__":
    startup()
