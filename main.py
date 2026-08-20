import os
import time
import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

VERSION = "CEO-BOT-V5-MARKETS-TRAVEL"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
HOME_AIRPORT = os.environ.get("HOME_AIRPORT", "YUL")

EXECUTOR = ThreadPoolExecutor(max_workers=4)
LOCK = threading.Lock()
CACHE = {}
RUNNING = set()
LAST_SECTION = {}

CACHE_TTL = {
    "dashboard": 900,
    "markets": 600,
    "iran": 600,
    "flight_iran": 7200,
    "flight_vancouver": 3600,
    "alerts": 900,
}

BUTTON_TO_SECTION = {
    "📌 داشبورد من": "dashboard",
    "💰 ارز و طلا": "markets",
    "🇮🇷 بازار ایران": "iran",
    "✈️ ارزان‌ترین ایران": "flight_iran",
    "✈️ ارزان‌ترین ونکوور": "flight_vancouver",
    "🚨 هشدارهای مهم": "alerts",
}

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📌 داشبورد من"}],
        [{"text": "💰 ارز و طلا"}, {"text": "🇮🇷 بازار ایران"}],
        [{"text": "✈️ ارزان‌ترین ایران"}, {"text": "✈️ ارزان‌ترین ونکوور"}],
        [{"text": "🚨 هشدارهای مهم"}, {"text": "🔄 بروزرسانی زنده"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "یک گزینه را انتخاب کن",
}

SYSTEM_PROMPT = """
You are a private market-and-travel intelligence assistant.
Write in Persian, optimized for a phone screen. Be concise, numerical, practical and heavily filtered.
The user does NOT want a broad news report. Only return information directly useful for money, assets, exchange rates, gold, travel prices, or a major risk/opportunity.

DATA RULES
- Use current web information.
- Never invent a price, rate, fare, date, percentage or source.
- For time-sensitive numbers, state the observation time/date when available.
- Prefer primary or well-established sources.
- For USD/CAD prefer Bank of Canada or established FX sources.
- For international gold prefer reputable market data sources.
- For Iran free-market FX and gold, cross-check when possible using reputable Iranian market sources such as TGJU and Bonbast; clearly distinguish free-market from official rates.
- Iran everyday prices should show TOMAN first and RIAL second when useful.
- If CAD/IRR is derived from USD/IRR and USD/CAD rather than directly quoted, label it clearly as «محاسبه‌ای» and show the formula conceptually.
- For flights, never call something «cheapest» unless a current searchable fare with route/dates is actually visible. If dynamic fare data is unavailable, say «قیمت قابل تأیید پیدا نشد» rather than guessing.
- For flights include total round-trip price in CAD, origin/destination airports, outbound/return dates, airline(s), stops, and where the fare was found. Mention baggage only if verified.
- Do not fill space with GDP, routine politics, housing statistics, construction permits, or generic macro commentary unless there is a material direct effect on the tracked items.
- Avoid raw URL dumps. Give short source names at the end.

STYLE
- Use short lines and compact bullets.
- Put the number first.
- Use ▲ ▼ → only when direction is supported by data.
- Maximum 1-3 short sentences of interpretation after the numbers.
"""


def build_prompt(section: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    prompts = {
        "dashboard": f"""
Current local timestamp: {now}.
Prepare ONE compact personal dashboard with this exact order:

📌 داشبورد بازار و سفر

🥇 طلای جهانی
- XAU/USD spot per troy ounce
- 24h or latest session change if reliably available

🇺🇸🇨🇦 دلار آمریکا / کانادا
- 1 USD = ? CAD
- 1 CAD = ? USD
- latest daily change if available

🇺🇸🇮🇷 دلار آمریکا / ایران
- free-market 1 USD = ? toman
- also show rial equivalent
- daily change if reliable

🇨🇦🇮🇷 دلار کانادا / ایران
- 1 CAD = ? toman and rial
- use a direct reliable quote if available; otherwise derive from USD/IRR and USD/CAD and label «محاسبه‌ای»

🥇🇮🇷 طلای ایران
- 18K gold price per gram in toman and rial
- international-gold linkage or daily change only if reliable
- optionally one major coin price if reliable

✈️ {HOME_AIRPORT} ↔ Iran
- Find the cheapest CURRENTLY VISIBLE round-trip economy fare for 1 adult, flexible travel in the next 90 days, preferably Tehran IKA; allow other practical Iran airports only if clearly cheaper.
- Search stays roughly 14-45 days.
- Give CAD total, exact dates, airline(s), stops, and source.

✈️ {HOME_AIRPORT} ↔ Vancouver YVR
- Find the cheapest CURRENTLY VISIBLE round-trip economy fare for 1 adult in the next 60 days, flexible dates, stay roughly 3-7 days.
- Give CAD total, exact dates, airline(s), stops, and source.

🚨 فقط اگر مهم است
- Maximum 3 items that materially affect the values above.

Do not add unrelated macro sections. Keep the entire dashboard compact.
""",

        "markets": f"""
Current timestamp: {now}.
Return only a compact live market board:
1) International gold XAU/USD spot + latest change.
2) USD/CAD and inverse CAD/USD + latest change.
3) USD/IRR free market in TOMAN first and RIAL second + latest reliable change.
4) CAD/IRR in TOMAN and RIAL; direct quote if reliable, otherwise derived and explicitly marked «محاسبه‌ای».
5) Iran 18K gold per gram in TOMAN and RIAL + latest reliable change.
6) One short note: what moved most and why, only if evidence is clear.
For every number, prefer current value and name the source. No generic news.
""",

        "iran": f"""
Current timestamp: {now}.
Return only a compact Iran asset board for an asset owner:
- USD free-market rate: toman + rial, current value and daily move.
- CAD/IRR: toman + rial; label derived values.
- 18K gold per gram: toman + rial, current value and daily move.
- Emami coin only if a current reliable quote exists.
- Maximum 3 material Iran-specific alerts affecting FX/gold/asset values: sanctions, capital controls, monetary decisions, or severe geopolitical risk.
Ignore routine political news and broad commentary.
""",

        "flight_iran": f"""
Current timestamp: {now}.
Search for the cheapest CURRENTLY VISIBLE round-trip ECONOMY airfare for 1 adult from {HOME_AIRPORT} (Montreal) to Iran within the next 90 days.
Primary destination: Tehran IKA. You may include another practical Iranian international airport only if the visible total fare is clearly cheaper.
Use flexible dates and target a stay of roughly 14-45 days.
Return up to 3 best verified options ranked by total CAD price.
For each show: total round-trip CAD price, outbound date, return date, exact airports, airline(s), number of stops each way, and source/platform.
Do not quote a fare if the amount/dates are not actually visible in a current source. If no verifiable live fare is available, state «قیمت قابل تأیید پیدا نشد» and do not estimate.
Keep it concise.
""",

        "flight_vancouver": f"""
Current timestamp: {now}.
Search for the cheapest CURRENTLY VISIBLE round-trip ECONOMY airfare for 1 adult from {HOME_AIRPORT} (Montreal) to Vancouver YVR within the next 60 days.
Use flexible dates and target a stay of roughly 3-7 days.
Return up to 3 best verified options ranked by total CAD price.
For each show: total round-trip CAD price, outbound date, return date, airline(s), nonstop or stops, and source/platform.
Do not quote a fare if the amount/dates are not actually visible in a current source. If no verifiable live fare is available, state «قیمت قابل تأیید پیدا نشد» and do not estimate.
Keep it concise.
""",

        "alerts": f"""
Current timestamp: {now}.
Return a maximum of 5 alerts ONLY if they materially affect one of these tracked items:
- international gold
- USD/CAD
- USD/IRR free market
- CAD/IRR
- Iran gold
- airfare from {HOME_AIRPORT} to Iran
- airfare from {HOME_AIRPORT} to Vancouver
Include only unusual moves, major policy/geopolitical changes, or unusually attractive travel-price opportunities. Ignore routine news. If nothing material exists, say «هشدار مهمی در حال حاضر دیده نشد.»
""",
    }
    return prompts[section]


def http_json(url: str, method: str = "GET", payload=None, headers=None, timeout: int = 30):
    data = None
    request_headers = {"User-Agent": "Metra-CEO-Bot/5.0"}
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
    if isinstance(top, str) and top.strip():
        return top.strip()

    def walk(obj):
        found = []
        if isinstance(obj, dict):
            if obj.get("type") == "output_text" and isinstance(obj.get("text"), str):
                found.append(obj["text"])
            for value in obj.values():
                found.extend(walk(value))
        elif isinstance(obj, list):
            for value in obj:
                found.extend(walk(value))
        return found

    nested = [x.strip() for x in walk(response) if isinstance(x, str) and x.strip()]
    return "\n".join(dict.fromkeys(nested))


def call_openai(section: str, max_tokens: int) -> dict:
    payload = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_tokens,
        "instructions": SYSTEM_PROMPT,
        "input": build_prompt(section),
    }
    return http_json(
        "https://api.openai.com/v1/responses",
        method="POST",
        payload=payload,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        timeout=110,
    )


def generate_report(section: str) -> str:
    print(f"[{VERSION}] web job start: {section}", flush=True)
    token_budget = 2600 if section == "dashboard" else 1800
    response = call_openai(section, token_budget)
    text = extract_output_text(response)

    if not text:
        print(
            f"[{VERSION}] empty output: section={section} status={response.get('status')} details={response.get('incomplete_details')}",
            flush=True,
        )
        response = call_openai(section, token_budget + 1600)
        text = extract_output_text(response)

    if not text:
        raise RuntimeError("OpenAI returned no visible text after retry")

    print(f"[{VERSION}] web job done: {section} chars={len(text)}", flush=True)
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


def cache_read(section: str):
    with LOCK:
        item = CACHE.get(section)
        if not item:
            return None, False
        return item, time.time() - item["time"] <= CACHE_TTL[section]


def cache_write(section: str, text: str):
    with LOCK:
        CACHE[section] = {"text": text, "time": time.time()}


def time_label(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def background_job(section: str, chat_id, notify: bool):
    try:
        text = generate_report(section)
        cache_write(section, text)
        if notify and chat_id:
            send_telegram(f"✅ بروزرسانی {time_label(time.time())}\n\n{text}", chat_id, menu=True)
    except Exception as exc:
        print(f"[{VERSION}] job error {section}: {type(exc).__name__}: {exc}", flush=True)
        if notify and chat_id:
            send_telegram("⚠️ بروزرسانی این بخش کامل نشد. چند دقیقه دیگر دوباره امتحان کن.", chat_id, menu=True)
    finally:
        with LOCK:
            RUNNING.discard(section)


def start_job(section: str, chat_id=None, notify: bool = False) -> bool:
    with LOCK:
        if section in RUNNING:
            return False
        RUNNING.add(section)
    EXECUTOR.submit(background_job, section, chat_id, notify)
    return True


def serve(section: str, chat_id: str):
    LAST_SECTION[chat_id] = section
    item, fresh = cache_read(section)
    if item and fresh:
        send_telegram(f"⚡ بروزرسانی {time_label(item['time'])}\n\n{item['text']}", chat_id, menu=True)
        return
    if item:
        send_telegram(
            f"⚡ آخرین نسخه موجود ({time_label(item['time'])})\nنسخه تازه هم‌زمان در پس‌زمینه در حال آماده‌شدن است.\n\n{item['text']}",
            chat_id,
            menu=True,
        )
        start_job(section, chat_id, notify=True)
        return
    if start_job(section, chat_id, notify=True):
        send_telegram("⚡ درخواست ثبت شد؛ نتیجه پس از آماده‌شدن خودکار می‌آید و منو آزاد است.", chat_id, menu=True)
    else:
        send_telegram("⏳ همین بخش الان در حال بروزرسانی است.", chat_id, menu=True)


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
            "⚡ داشبورد شخصی بازار و سفر آماده است.\n\nفقط قیمت‌ها، دارایی‌های ایران، بلیت‌های ارزان و هشدارهای مهم نمایش داده می‌شوند.",
            chat_id,
            menu=True,
        )
        return

    if raw == "🔄 بروزرسانی زنده":
        section = LAST_SECTION.get(chat_id)
        if not section:
            send_telegram("اول یکی از بخش‌ها را انتخاب کن.", chat_id, menu=True)
        elif start_job(section, chat_id, notify=True):
            send_telegram("🔄 بروزرسانی زنده شروع شد. منو همچنان آزاد است.", chat_id, menu=True)
        else:
            send_telegram("⏳ همین بخش الان در حال بروزرسانی است.", chat_id, menu=True)
        return

    commands = {
        "/dashboard": "dashboard",
        "/markets": "markets",
        "/iran": "iran",
        "/iranflight": "flight_iran",
        "/vancouver": "flight_vancouver",
        "/alerts": "alerts",
    }
    section = BUTTON_TO_SECTION.get(raw) or commands.get(low)
    if section:
        serve(section, chat_id)
    else:
        send_telegram("یکی از دکمه‌های منو را انتخاب کن.", chat_id, menu=True)


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
    try:
        send_telegram(
            "✅ Metra Market & Travel Intelligence V5 فعال شد.\nداشبورد پالایش‌شده آماده است.",
            TELEGRAM_CHAT_ID,
            menu=True,
        )
    except Exception as exc:
        print(f"[{VERSION}] startup telegram error: {type(exc).__name__}: {exc}", flush=True)

    # Warm the highest-value market dashboard only; flight searches run on demand.
    start_job("markets", notify=False)
    polling_loop()


if __name__ == "__main__":
    startup()
