import os
import time
import threading
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI

VERSION = "CEO-BOT-V2-FAST"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    timeout=90.0,
    max_retries=1,
)

EXECUTOR = ThreadPoolExecutor(max_workers=4)
LOCK = threading.Lock()
CACHE = {}
RUNNING = set()
LAST_SECTION = {}

CACHE_TTL = {
    "report": 1800,
    "alerts": 600,
    "canada": 1800,
    "iran": 900,
    "construction": 1800,
    "prices": 600,
    "global": 1800,
    "opportunities": 3600,
}

BUTTON_TO_SECTION = {
    "🧠 گزارش کامل CEO": "report",
    "🚨 هشدارهای مهم": "alerts",
    "🇨🇦 کانادا": "canada",
    "🇮🇷 ایران و دارایی‌ها": "iran",
    "🏗 مهندسی و ساخت‌وساز": "construction",
    "📊 قیمت‌ها و بازارها": "prices",
    "🌎 اقتصاد جهانی": "global",
    "📈 فرصت‌های تجاری": "opportunities",
}

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🧠 گزارش کامل CEO"}, {"text": "🚨 هشدارهای مهم"}],
        [{"text": "📊 قیمت‌ها و بازارها"}],
        [{"text": "🇨🇦 کانادا"}, {"text": "🇮🇷 ایران و دارایی‌ها"}],
        [{"text": "🏗 مهندسی و ساخت‌وساز"}],
        [{"text": "🌎 اقتصاد جهانی"}, {"text": "📈 فرصت‌های تجاری"}],
        [{"text": "🔄 بروزرسانی زنده"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "یک گزینه را انتخاب کن",
}

SYSTEM_PROMPT = """
You are the external intelligence analyst for the CEO of a Canadian engineering consulting company.
Write in Persian. Be concise, numerical, neutral and decision-oriented.
Use current web information and prioritize government, central bank, regulator, statistics, professional engineering and reputable financial/industry sources.
Never invent a price or statistic. Distinguish Iran free-market rates from official rates. Cross-check Iran figures when possible.
Do not dump raw URLs in the body. Name sources briefly at the end.
Keep each section compact enough to read on a phone.
For each important item, state: what happened, why it matters, and what to watch next.
"""


def build_prompt(section: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    prompts = {
        "report": f"""Today is {today}. Prepare a compact CEO intelligence brief covering only material developments from the last 24 hours and 7 days. Structure: 🚨 Alerts (max 5), 🇨🇦 Canada, 🏗 Engineering & Construction, 🇮🇷 Iran & Assets, 🌎 Global, 📈 Opportunities, ⚠️ Risks, 🎯 CEO actions (3-5). Keep it under about 900 Persian words.""",
        "alerts": f"""Today is {today}. Return only the 5-7 most important CEO alerts from the last 24 hours and 7 days affecting Canada, Quebec, BC, Iran, currencies, gold, oil, interest rates, engineering, construction, sanctions or asset values. Ignore routine news. Keep it very concise.""",
        "canada": f"""Today is {today}. Give a concise Canada/Quebec/BC CEO brief: Bank of Canada, rates, inflation, CAD/USD, housing, construction, permits, infrastructure spending and business conditions. End with 3 CEO implications.""",
        "iran": f"""Today is {today}. Give a concise Iran asset brief: USD/IRR free market, CAD/IRR if reliable, gold, coins, inflation, monetary policy, real-estate signals, sanctions and geopolitical risk. Clearly separate confirmed data from estimates. End with 3 asset-owner implications.""",
        "construction": f"""Today is {today}. Give a concise Canadian engineering/construction market brief: structural/civil/geotechnical demand, consulting fees, labour, steel, rebar, concrete, lumber, asphalt, excavation, drilling, surveying, regulations, tenders and technology. End with pricing/business implications.""",
        "prices": f"""Today is {today}. Create a phone-friendly price dashboard with the latest reliable values/trends for CAD/USD, international gold, USD/IRR free market, Iranian gold/coins if reliable, Bank of Canada rate, oil, and major construction-material trends. Use arrows and percentage changes where available. Use N/A instead of guessing.""",
        "global": f"""Today is {today}. Give a concise global macro brief only for developments that materially affect Canada, Iran, gold, oil, currencies, rates, engineering demand or construction. Focus on central banks, inflation, oil, gold, geopolitics, sanctions and trade. End with 3 practical implications.""",
        "opportunities": f"""Today is {today}. Find actionable business opportunities for a Canadian small-to-medium engineering consulting firm offering structural, civil, geotechnical, inspection and rehabilitation services. Prioritize Quebec, BC and major Canadian markets. Rank 5-8 opportunities by attractiveness and give one next action for each.""",
    }
    return prompts[section]


def generate_report(section: str) -> str:
    print(f"[{VERSION}] web job start: {section}", flush=True)
    response = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search", "search_context_size": "low"}],
        max_output_tokens=1200,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(section)},
        ],
    )
    text = response.output_text.strip()
    print(f"[{VERSION}] web job done: {section}", flush=True)
    return text


def send_telegram(message: str, chat_id: str = TELEGRAM_CHAT_ID, menu: bool = False):
    chunks = [message[i:i + 3800] for i in range(0, len(message), 3800)] or [""]
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if menu and i == len(chunks) - 1:
            payload["reply_markup"] = MAIN_KEYBOARD
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)
        r.raise_for_status()


def cache_read(section: str):
    with LOCK:
        item = CACHE.get(section)
        if not item:
            return None, False
        fresh = time.time() - item["time"] <= CACHE_TTL[section]
        return item, fresh


def cache_write(section: str, text: str):
    with LOCK:
        CACHE[section] = {"text": text, "time": time.time()}


def time_label(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def background_job(section: str, chat_id: str | None, notify: bool):
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


def start_job(section: str, chat_id: str | None = None, notify: bool = False) -> bool:
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
            f"⚡ آخرین نسخه موجود ({time_label(item['time'])})\n"
            "نسخه تازه هم‌زمان در پس‌زمینه در حال آماده‌شدن است.\n\n"
            + item["text"],
            chat_id,
            menu=True,
        )
        start_job(section, chat_id, notify=True)
        return

    if start_job(section, chat_id, notify=True):
        send_telegram(
            "⚡ درخواست ثبت شد. لازم نیست منتظر بمانی؛ منو همچنان فعال است.\n"
            "نتیجه پس از آماده‌شدن خودکار ارسال می‌شود.",
            chat_id,
            menu=True,
        )
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
            "⚡ Metra CEO Intelligence — نسخه سریع\n\n"
            "منو فوری کار می‌کند و گزارش‌ها در پس‌زمینه بروزرسانی می‌شوند.\n"
            "یکی از دکمه‌ها را انتخاب کن.",
            chat_id,
            menu=True,
        )
        return

    if raw == "🔄 بروزرسانی زنده":
        section = LAST_SECTION.get(chat_id)
        if not section:
            send_telegram("اول یکی از بخش‌ها را انتخاب کن.", chat_id, menu=True)
            return
        if start_job(section, chat_id, notify=True):
            send_telegram("🔄 بروزرسانی زنده شروع شد. منو همچنان آزاد است.", chat_id, menu=True)
        else:
            send_telegram("⏳ همین بخش الان در حال بروزرسانی است.", chat_id, menu=True)
        return

    commands = {
        "/report": "report", "/alerts": "alerts", "/canada": "canada",
        "/iran": "iran", "/construction": "construction", "/prices": "prices",
        "/global": "global", "/opportunities": "opportunities",
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
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
            r.raise_for_status()
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if msg:
                    handle_message(msg)
        except requests.RequestException as exc:
            print(f"[{VERSION}] telegram network error: {exc}", flush=True)
            time.sleep(2)
        except Exception as exc:
            print(f"[{VERSION}] loop error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(2)


def startup():
    print(f"[{VERSION}] START", flush=True)
    try:
        send_telegram(
            "✅ Metra CEO Intelligence V2 فعال شد.\n"
            "منوی سریع آماده است؛ نیازی به تایپ دستور نیست.",
            TELEGRAM_CHAT_ID,
            menu=True,
        )
    except Exception as exc:
        print(f"[{VERSION}] startup telegram error: {exc}", flush=True)

    # Pre-warm the most frequently used sections without blocking Telegram.
    for section in ("prices", "alerts", "report"):
        start_job(section, notify=False)

    polling_loop()


if __name__ == "__main__":
    startup()
