import os
import time
import requests
from datetime import datetime
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SYSTEM_PROMPT = """
You are the External Intelligence Analyst for the CEO of an engineering consulting company in Canada.

Your responsibility is to research current external information and produce concise Persian-language executive intelligence.

Priority areas:
1. Canada / Quebec / British Columbia: Bank of Canada, interest rates, inflation, CAD/USD, economy, housing, construction activity, permits, infrastructure investment.
2. Engineering & Construction: structural, civil and geotechnical engineering; consulting fees; labour and salary trends; steel, rebar, concrete, cement, lumber, asphalt, aggregates, excavation, drilling, surveying, lab testing, equipment rental; BIM, Revit, LiDAR, drones, AI; regulations; tenders and opportunities.
3. Iran & Assets: USD/IRR free-market rate, EUR/IRR and CAD/IRR when reliable, Iranian gold and coins, international gold, inflation, monetary policy, liquidity, interest rates, Tehran real estate, transaction volume, taxation, capital controls, sanctions and geopolitical risk.
4. Global: only developments that materially affect Canada, Iran, gold, oil, currencies, construction, engineering, interest rates or the CEO's assets.

Research rules:
- Use current web information.
- Prioritize governments, central banks, regulators, statistical agencies, professional organizations, established financial publications and reputable industry sources.
- Cross-check important Iran figures when possible.
- Never invent a price or statistic.
- If reliable information is unavailable, say: داده قابل اتکای کافی پیدا نشد.
- Distinguish facts from analysis.
- Do not sensationalize politics.
- Keep raw URLs out of the main body whenever possible. Mention source names briefly instead of dumping long links.

Style:
- Persian
- concise, numerical, neutral and decision-oriented
- use current values, previous values and percentage changes when meaningful
- explain what happened, why it matters, what may happen next and what the CEO should watch
"""

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
        [{"text": "🇨🇦 کانادا"}, {"text": "🇮🇷 ایران و دارایی‌ها"}],
        [{"text": "🏗 مهندسی و ساخت‌وساز"}],
        [{"text": "📊 قیمت‌ها و بازارها"}],
        [{"text": "🌎 اقتصاد جهانی"}, {"text": "📈 فرصت‌های تجاری"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def build_prompt(section: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    if section == "report":
        return f"""
Today is {today}. Search the web and prepare a current CEO External Intelligence Brief in Persian.
Focus on the last 24 hours and last 7 days.

Use this exact structure:
🧠 CEO EXTERNAL INTELLIGENCE BRIEF
Date: {today}

🚨 CEO ALERTS
Maximum 5 important developments.

🇨🇦 CANADA

🏗 ENGINEERING & CONSTRUCTION

🇮🇷 IRAN & ASSETS

🌎 GLOBAL SIGNALS

📈 OPPORTUNITIES

⚠️ RISKS

🎯 CEO TAKEAWAYS
Finish with 3 to 7 concrete CEO-level observations or actions.
"""

    prompts = {
        "alerts": f"""Today is {today}. Search the web and produce only the most important CEO alerts in Persian from the last 24 hours and last 7 days. Maximum 7 items. Prioritize material developments affecting Canada, Quebec, BC, Iran, currencies, gold, oil, interest rates, engineering, construction, sanctions, regulations or major asset values. For each alert include: what happened, why it matters, likely impact, and what to watch next. Ignore routine news.""",
        "iran": f"""Today is {today}. Search the web and give me a concise Iran asset intelligence brief in Persian. Focus on USD/IRR free-market rate, gold, coins, inflation, monetary policy, property/real-estate signals, sanctions and geopolitical developments that can materially affect asset values. Separate confirmed data from estimates and explain what matters for an asset owner.""",
        "canada": f"""Today is {today}. Search the web and give me a concise Canada/Quebec/BC CEO intelligence brief in Persian. Focus on Bank of Canada, rates, inflation, CAD/USD, housing, construction, permits, infrastructure spending and business conditions. End with CEO implications.""",
        "construction": f"""Today is {today}. Search the web and give me a concise Canadian engineering and construction market brief in Persian. Focus on structural/civil/geotechnical demand, engineering fees, labour costs, steel, rebar, concrete, lumber, asphalt, excavation, drilling, surveying, regulations, tenders and technologies. End with pricing and business implications.""",
        "prices": f"""Today is {today}. Search the web and prepare a concise price-and-market dashboard in Persian for a Canadian engineering CEO. Include the latest reliable values/trends available for CAD/USD, international gold, USD/IRR free market, Iranian gold/coins when reliable, Bank of Canada rate, oil, and major construction material price trends. Use N/A rather than inventing data. Show direction arrows and percentage changes when available.""",
        "global": f"""Today is {today}. Search the web and prepare a concise global macroeconomic intelligence brief in Persian. Only include developments that can materially affect Canada, Iran, gold, oil, currencies, interest rates, construction, engineering demand or the CEO's assets. Focus on central banks, inflation, recession risk, oil, gold, geopolitics, sanctions and major trade developments. End with practical CEO implications.""",
        "opportunities": f"""Today is {today}. Search the web for actionable business opportunities relevant to a Canadian small-to-medium engineering consulting firm providing structural, civil, geotechnical, inspection and rehabilitation services. Prioritize Quebec, British Columbia and major Canadian markets. Look for infrastructure programs, tenders, municipal spending, rehabilitation needs, regulatory changes creating demand, technology opportunities, partnerships and underserved niches. Return a concise Persian list ranked by attractiveness, with why it matters and the next action to investigate.""",
    }
    return prompts[section]


def generate_report(section: str) -> str:
    response = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search"}],
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(section)},
        ],
    )
    return response.output_text


def send_telegram(message: str, chat_id: str = TELEGRAM_CHAT_ID, with_menu: bool = False):
    max_length = 3900
    chunks = [message[i:i + max_length] for i in range(0, len(message), max_length)] or [""]

    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if with_menu and i == len(chunks) - 1:
            payload["reply_markup"] = MAIN_KEYBOARD

        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()


def welcome_text() -> str:
    return (
        "🧠 Metra CEO Intelligence\n\n"
        "یکی از گزینه‌های زیر را انتخاب کن.\n"
        "گزارش‌ها با جست‌وجوی وب و اطلاعات به‌روز تهیه می‌شوند."
    )


def handle_message(message: dict):
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    original_text = (message.get("text") or "").strip()
    text = original_text.lower()

    if chat_id != TELEGRAM_CHAT_ID:
        if chat_id:
            send_telegram("این ربات خصوصی است.", chat_id)
        return

    if text in {"/start", "/help", "hello", "hi"}:
        send_telegram(welcome_text(), chat_id, with_menu=True)
        return

    command_map = {
        "/report": "report",
        "/alerts": "alerts",
        "/iran": "iran",
        "/canada": "canada",
        "/construction": "construction",
        "/prices": "prices",
        "/global": "global",
        "/opportunities": "opportunities",
    }

    section = BUTTON_TO_SECTION.get(original_text) or command_map.get(text)

    if not section:
        send_telegram("یکی از دکمه‌های منو را انتخاب کن.", chat_id, with_menu=True)
        return

    send_telegram("⏳ در حال بررسی منابع به‌روز و تهیه گزارش...", chat_id)

    try:
        report = generate_report(section)
        send_telegram(report, chat_id, with_menu=True)
    except Exception as exc:
        print(f"OpenAI/report error: {type(exc).__name__}: {exc}", flush=True)
        send_telegram("⚠️ در تهیه گزارش خطایی رخ داد. چند دقیقه دیگر دوباره امتحان کن.", chat_id, with_menu=True)


def poll_telegram():
    offset = None
    print("CEO Intelligence Telegram bot is running with Persian button menu...", flush=True)

    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params=params,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                print(f"Telegram API error: {data}", flush=True)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    handle_message(message)

        except requests.RequestException as exc:
            print(f"Telegram polling error: {exc}", flush=True)
            time.sleep(5)
        except Exception as exc:
            print(f"Unexpected error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    poll_telegram()
