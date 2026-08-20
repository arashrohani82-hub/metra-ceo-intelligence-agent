import os
import time
import json
import threading
import io
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display

VERSION = "CEO-BOT-V7-GRAPHIC"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

EXECUTOR = ThreadPoolExecutor(max_workers=2)
LOCK = threading.Lock()
SNAPSHOT = None
SNAPSHOT_TIME = 0
REFRESHING = False
TTL = 30 * 60

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 داشبورد فوری"}],
        [{"text": "💰 ارز و طلا"}, {"text": "✈️ بلیط‌ها"}],
        [{"text": "🚨 هشدارها"}, {"text": "🔄 بروزرسانی"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "یک گزینه را انتخاب کن",
}

SYSTEM_PROMPT = """
You are a private market-and-travel data collector for a graphical CEO dashboard.
Your output will be parsed by software, so return ONLY valid JSON and no markdown.
Use current web information. Never invent a number. Use null when a value cannot be verified.
Iran FX must be free-market, with TOMAN as the main display unit.
For flights, only use currently visible round-trip economy fares for one adult from Montreal YUL.
Prefer Tehran IKA for Iran. For Vancouver use YVR.
Keep alerts extremely short and only material.
"""


def now_label():
    return datetime.now().strftime("%d %b %Y • %H:%M")


def snapshot_prompt():
    return f"""
Current local time: {datetime.now().isoformat(timespec='minutes')}.
Search current web data and return exactly one JSON object with this schema:
{{
  "gold_usd_oz": number|null,
  "gold_change_pct": number|null,
  "usd_cad": number|null,
  "usd_cad_change_pct": number|null,
  "cad_usd": number|null,
  "usd_irr_toman": number|null,
  "usd_irr_change_pct": number|null,
  "cad_irr_toman": number|null,
  "cad_irr_change_pct": number|null,
  "iran_gold18_toman_g": number|null,
  "iran_gold18_change_pct": number|null,
  "emami_coin_toman": number|null,
  "emami_coin_change_pct": number|null,
  "iran_flight": {{
    "price_cad": number|null,
    "outbound": "YYYY-MM-DD"|null,
    "return": "YYYY-MM-DD"|null,
    "airline": string|null,
    "stops": string|null,
    "source_url": string|null
  }},
  "vancouver_flight": {{
    "price_cad": number|null,
    "outbound": "YYYY-MM-DD"|null,
    "return": "YYYY-MM-DD"|null,
    "airline": string|null,
    "stops": string|null,
    "source_url": string|null
  }},
  "alerts": [string],
  "sources": [string]
}}
Rules:
- Gold = current international spot XAU/USD per troy ounce.
- USD/CAD = 1 USD in CAD; CAD/USD = inverse.
- Iran FX = current free-market TOMAN values. If CAD/IRR is derived from USD/IRR and USD/CAD, that is acceptable.
- Iran 18K gold = toman per gram.
- Flight Iran: cheapest currently visible YUL↔IKA round trip within next 90 days, roughly 7-30 nights.
- Flight Vancouver: cheapest currently visible YUL↔YVR round trip within next 60 days, roughly 3-7 nights.
- alerts: maximum 2, each under 55 Persian characters. Empty list if nothing material.
- sources: names only, maximum 5. No commentary.
"""


def parse_json_text(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def call_openai_snapshot() -> dict:
    payload = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "reasoning": {"effort": "low"},
        "max_output_tokens": 2200,
        "instructions": SYSTEM_PROMPT,
        "input": snapshot_prompt(),
    }
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=110,
    )
    r.raise_for_status()
    data = r.json()
    parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if isinstance(c.get("text"), str):
                    parts.append(c["text"])
    text = "\n".join(parts).strip() or str(data.get("output_text", "")).strip()
    if not text:
        raise RuntimeError("OpenAI returned empty snapshot")
    return parse_json_text(text)


def telegram_send_message(text: str, chat_id=TELEGRAM_CHAT_ID, menu=True):
    payload = {"chat_id": chat_id, "text": text}
    if menu:
        payload["reply_markup"] = json.dumps(MAIN_KEYBOARD, ensure_ascii=False)
    r = requests.post(f"{TELEGRAM_API}/sendMessage", data=payload, timeout=25)
    r.raise_for_status()


def telegram_send_photo(image_bytes: bytes, caption: str = "", chat_id=TELEGRAM_CHAT_ID, inline_keyboard=None):
    data = {"chat_id": chat_id, "caption": caption}
    if inline_keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": inline_keyboard}, ensure_ascii=False)
    files = {"photo": ("dashboard.png", image_bytes, "image/png")}
    r = requests.post(f"{TELEGRAM_API}/sendPhoto", data=data, files=files, timeout=35)
    r.raise_for_status()


def get_updates(offset=None):
    params = {"timeout": 25}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
    r.raise_for_status()
    return r.json()


def font(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def rtl(text):
    if text is None:
        return ""
    s = str(text)
    try:
        return get_display(arabic_reshaper.reshape(s))
    except Exception:
        return s


def fmt_num(value, decimals=0):
    if value is None:
        return "N/A"
    try:
        n = float(value)
        if decimals:
            return f"{n:,.{decimals}f}"
        return f"{n:,.0f}"
    except Exception:
        return str(value)


def fmt_pct(value):
    if value is None:
        return ""
    try:
        v = float(value)
        arrow = "▲" if v > 0 else "▼" if v < 0 else "→"
        return f"{arrow} {v:+.2f}%"
    except Exception:
        return ""


def color_for_change(value):
    try:
        v = float(value)
        if v > 0:
            return (34, 214, 110)
        if v < 0:
            return (255, 72, 72)
    except Exception:
        pass
    return (160, 170, 180)


def rounded(draw, box, radius=24, fill=(12, 22, 31), outline=(45, 65, 78), width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def center_text(draw, box, text, fnt, fill):
    x1, y1, x2, y2 = box
    b = draw.textbbox((0, 0), text, font=fnt)
    w = b[2] - b[0]
    h = b[3] - b[1]
    draw.text(((x1 + x2 - w) / 2, (y1 + y2 - h) / 2), text, font=fnt, fill=fill)


def card_metric(draw, box, title, value, subtitle="", change=None, accent=(46, 204, 113)):
    rounded(draw, box, fill=(10, 19, 27), outline=accent, width=2)
    x1, y1, x2, y2 = box
    draw.text((x1 + 22, y1 + 18), rtl(title), font=font(27, True), fill=(236, 241, 245))
    draw.text((x1 + 22, y1 + 65), value, font=font(43, True), fill=(245, 248, 250))
    if subtitle:
        draw.text((x1 + 22, y1 + 120), rtl(subtitle), font=font(22), fill=(165, 178, 190))
    if change is not None:
        draw.text((x1 + 22, y2 - 42), fmt_pct(change), font=font(22, True), fill=color_for_change(change))


def flight_card(draw, box, title, flight, accent):
    rounded(draw, box, fill=(9, 20, 30), outline=accent, width=2)
    x1, y1, x2, y2 = box
    draw.text((x1 + 22, y1 + 18), rtl(title), font=font(27, True), fill=(238, 243, 247))
    price = flight.get("price_cad") if isinstance(flight, dict) else None
    draw.text((x1 + 22, y1 + 70), f"C${fmt_num(price)}" if price is not None else "N/A", font=font(46, True), fill=accent)
    out = (flight or {}).get("outbound") or "—"
    ret = (flight or {}).get("return") or "—"
    draw.text((x1 + 22, y1 + 132), f"{out}  →  {ret}", font=font(22), fill=(220, 226, 231))
    airline = (flight or {}).get("airline") or "N/A"
    stops = (flight or {}).get("stops") or "N/A"
    draw.text((x1 + 22, y1 + 171), f"{airline}  •  {stops}", font=font(20), fill=(164, 178, 190))


def render_dashboard(snapshot: dict) -> bytes:
    W, H = 1080, 1500
    img = Image.new("RGB", (W, H), (4, 10, 16))
    d = ImageDraw.Draw(img)

    d.text((48, 34), "METRA", font=font(44, True), fill=(242, 245, 247))
    d.text((48, 82), "CEO DASHBOARD", font=font(23, True), fill=(46, 204, 113))
    d.text((525, 48), now_label(), font=font(23), fill=(190, 201, 210))
    rounded(d, (885, 34, 1035, 88), radius=18, fill=(8, 42, 28), outline=(46, 204, 113), width=2)
    center_text(d, (885, 34, 1035, 88), "● LIVE", font(24, True), (46, 204, 113))

    card_metric(d, (40, 145, 360, 420), "🥇 طلای جهانی", f"${fmt_num(snapshot.get('gold_usd_oz'))}", "هر اونس", snapshot.get("gold_change_pct"), (232, 170, 32))
    card_metric(d, (380, 145, 700, 420), "🇺🇸 USD / CAD 🇨🇦", fmt_num(snapshot.get("usd_cad"), 4), "", snapshot.get("usd_cad_change_pct"), (34, 214, 110))
    card_metric(d, (720, 145, 1040, 420), "🇨🇦 CAD / USD 🇺🇸", fmt_num(snapshot.get("cad_usd"), 4), "", None, (54, 130, 220))

    card_metric(d, (40, 445, 280, 700), "🇺🇸 USD / IRR", fmt_num(snapshot.get("usd_irr_toman")), "تومان", snapshot.get("usd_irr_change_pct"), (54, 130, 220))
    card_metric(d, (300, 445, 540, 700), "🇨🇦 CAD / IRR", fmt_num(snapshot.get("cad_irr_toman")), "تومان", snapshot.get("cad_irr_change_pct"), (54, 130, 220))
    card_metric(d, (560, 445, 800, 700), "🥇 طلای 18 عیار", fmt_num(snapshot.get("iran_gold18_toman_g")), "تومان / گرم", snapshot.get("iran_gold18_change_pct"), (34, 214, 110))
    card_metric(d, (820, 445, 1040, 700), "🪙 سکه امامی", fmt_num(snapshot.get("emami_coin_toman")), "تومان", snapshot.get("emami_coin_change_pct"), (34, 214, 110))

    d.text((46, 740), rtl("✈️ ارزان‌ترین بلیت رفت و برگشت"), font=font(31, True), fill=(242, 245, 247))
    flight_card(d, (40, 800, 520, 1085), "🇮🇷 مونترال ⇄ تهران", snapshot.get("iran_flight") or {}, (56, 139, 253))
    flight_card(d, (560, 800, 1040, 1085), "🇨🇦 مونترال ⇄ ونکوور", snapshot.get("vancouver_flight") or {}, (34, 214, 110))

    rounded(d, (40, 1120, 1040, 1310), fill=(8, 18, 25), outline=(44, 60, 72), width=2)
    d.text((64, 1143), rtl("🔔 هشدارهای مهم"), font=font(29, True), fill=(242, 245, 247))
    alerts = snapshot.get("alerts") or []
    if not alerts:
        d.text((64, 1202), rtl("مورد مهمی وجود ندارد."), font=font(25), fill=(34, 214, 110))
    else:
        y = 1195
        for a in alerts[:2]:
            d.text((64, y), rtl("• " + str(a)), font=font(23), fill=(225, 230, 235))
            y += 48

    sources = ", ".join((snapshot.get("sources") or [])[:4])
    d.text((48, 1360), rtl("آخرین بروزرسانی:"), font=font(20), fill=(140, 154, 165))
    d.text((220, 1360), now_label(), font=font(20), fill=(180, 192, 201))
    if sources:
        d.text((48, 1400), "Sources: " + sources[:95], font=font(18), fill=(120, 135, 148))
    d.text((48, 1450), rtl("داده‌ها هر 30 دقیقه بروزرسانی می‌شوند."), font=font(19), fill=(120, 135, 148))

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def build_inline_keyboard(snapshot):
    rows = []
    iran_url = ((snapshot.get("iran_flight") or {}).get("source_url"))
    van_url = ((snapshot.get("vancouver_flight") or {}).get("source_url"))
    row = []
    if iran_url and isinstance(iran_url, str) and iran_url.startswith("http"):
        row.append({"text": "✈️ مشاهده بلیت تهران", "url": iran_url})
    if van_url and isinstance(van_url, str) and van_url.startswith("http"):
        row.append({"text": "✈️ مشاهده بلیت ونکوور", "url": van_url})
    if row:
        rows.append(row)
    return rows


def cache_get():
    with LOCK:
        return SNAPSHOT, SNAPSHOT_TIME


def refresh_snapshot(notify_chat=None):
    global SNAPSHOT, SNAPSHOT_TIME, REFRESHING
    try:
        print(f"[{VERSION}] refresh start", flush=True)
        data = call_openai_snapshot()
        with LOCK:
            SNAPSHOT = data
            SNAPSHOT_TIME = time.time()
        print(f"[{VERSION}] refresh done", flush=True)
        if notify_chat:
            telegram_send_photo(render_dashboard(data), "📊 داشبورد بروزرسانی شد", notify_chat, build_inline_keyboard(data))
    except Exception as exc:
        print(f"[{VERSION}] refresh error: {type(exc).__name__}: {exc}", flush=True)
        if notify_chat:
            telegram_send_message("⚠️ بروزرسانی کامل نشد؛ آخرین داشبورد ذخیره‌شده همچنان قابل استفاده است.", notify_chat)
    finally:
        with LOCK:
            REFRESHING = False


def start_refresh(notify_chat=None):
    global REFRESHING
    with LOCK:
        if REFRESHING:
            return False
        REFRESHING = True
    EXECUTOR.submit(refresh_snapshot, notify_chat)
    return True


def send_dashboard(chat_id):
    snap, ts = cache_get()
    if snap:
        telegram_send_photo(render_dashboard(snap), "", chat_id, build_inline_keyboard(snap))
        if time.time() - ts > TTL:
            start_refresh()
    else:
        start_refresh(chat_id)
        telegram_send_message("⏳ داشبورد اولیه در حال آماده‌شدن است؛ نتیجه خودکار به‌صورت کارت گرافیکی ارسال می‌شود.", chat_id)


def send_compact(kind, chat_id):
    snap, _ = cache_get()
    if not snap:
        start_refresh(chat_id)
        telegram_send_message("⏳ داده اولیه در حال آماده‌شدن است.", chat_id)
        return
    if kind == "markets":
        text = (
            f"🥇 Gold  ${fmt_num(snap.get('gold_usd_oz'))}  {fmt_pct(snap.get('gold_change_pct'))}\n"
            f"🇺🇸 USD/CAD  {fmt_num(snap.get('usd_cad'), 4)}\n"
            f"🇺🇸 USD  {fmt_num(snap.get('usd_irr_toman'))} تومان\n"
            f"🇨🇦 CAD  {fmt_num(snap.get('cad_irr_toman'))} تومان\n"
            f"🥇 18K  {fmt_num(snap.get('iran_gold18_toman_g'))} تومان/گرم"
        )
    elif kind == "flights":
        i = snap.get("iran_flight") or {}
        v = snap.get("vancouver_flight") or {}
        text = (
            f"🇮🇷 تهران  C${fmt_num(i.get('price_cad'))}  | {i.get('outbound') or '—'} → {i.get('return') or '—'}\n"
            f"🇨🇦 ونکوور  C${fmt_num(v.get('price_cad'))}  | {v.get('outbound') or '—'} → {v.get('return') or '—'}"
        )
    else:
        alerts = snap.get("alerts") or []
        text = "✅ فعلاً هشدار مهمی نیست." if not alerts else "🚨 " + "\n🚨 ".join(alerts[:2])
    telegram_send_message(text, chat_id)


def handle_message(message):
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id:
        return
    if chat_id != TELEGRAM_CHAT_ID:
        telegram_send_message("این ربات خصوصی است.", chat_id, menu=False)
        return
    raw = (message.get("text") or "").strip()
    low = raw.lower()
    if low in {"/start", "/help", "hello", "hi"}:
        telegram_send_message("📊 Metra CEO Dashboard\nنسخه گرافیکی آماده است. «داشبورد فوری» را بزن.", chat_id)
    elif raw == "📊 داشبورد فوری" or low == "/dashboard":
        send_dashboard(chat_id)
    elif raw == "💰 ارز و طلا" or low == "/markets":
        send_compact("markets", chat_id)
    elif raw == "✈️ بلیط‌ها" or low == "/flights":
        send_compact("flights", chat_id)
    elif raw == "🚨 هشدارها" or low == "/alerts":
        send_compact("alerts", chat_id)
    elif raw == "🔄 بروزرسانی" or low == "/refresh":
        if start_refresh(chat_id):
            telegram_send_message("🔄 بروزرسانی شروع شد؛ نتیجه جدید خودکار به صورت داشبورد گرافیکی می‌آید.", chat_id)
        else:
            telegram_send_message("⏳ بروزرسانی همین الان در حال اجراست.", chat_id)
    else:
        telegram_send_message("یکی از دکمه‌ها را انتخاب کن.", chat_id)


def scheduler_loop():
    while True:
        try:
            snap, ts = cache_get()
            if not snap or time.time() - ts > TTL:
                start_refresh()
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
                if update.get("message"):
                    handle_message(update["message"])
        except Exception as exc:
            print(f"[{VERSION}] telegram loop error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(2)


def startup():
    print(f"[{VERSION}] START", flush=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    start_refresh()
    try:
        telegram_send_message("✅ V7 فعال شد — داشبورد گرافیکی آماده است.", TELEGRAM_CHAT_ID)
    except Exception as exc:
        print(f"[{VERSION}] startup message error: {exc}", flush=True)
    polling_loop()


if __name__ == "__main__":
    startup()
