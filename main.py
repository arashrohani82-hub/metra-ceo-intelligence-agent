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

VERSION = "CEO-BOT-V8-VERIFIED"

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
        [{"text": "📊 داشبورد تأییدشده"}],
        [{"text": "💰 ارز و طلا"}, {"text": "✈️ بلیط‌ها"}],
        [{"text": "🚨 هشدارها"}, {"text": "🔄 بررسی مجدد"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "یک گزینه را انتخاب کن",
}

BASE_INSTRUCTIONS = """
You are a verification-grade data collector. Output ONLY valid JSON, no markdown.
Accuracy is more important than completeness. Never guess or infer a market price from a headline.
For every numeric item, use a current page that visibly contains that number. Return null when uncertain.
Iran FX must be FREE-MARKET TOMAN, not official/central-bank rate, and must have a visible source.
Flights must be CURRENT round-trip economy fares for one adult from Montreal YUL with visible total price and exact dates.
Never call a one-way fare, monthly teaser, 'from' price without dates, or stale snippet a verified round-trip fare.
"""


def now_label():
    return datetime.now().strftime("%d %b %Y • %H:%M")


def parse_json_text(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            return json.loads(text[s:e + 1])
        raise


def openai_json(prompt: str, max_tokens=2200) -> dict:
    payload = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_tokens,
        "instructions": BASE_INSTRUCTIONS,
        "input": prompt,
    }
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
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
        raise RuntimeError("empty OpenAI response")
    return parse_json_text(text)


def market_prompt(role: str) -> str:
    return f"""
Role: {role}. Time: {datetime.now().isoformat(timespec='minutes')}.
Collect these values independently. Prefer primary/major sources; for Iran use reputable free-market sources such as TGJU/Bonbast/DolarChand only when the displayed unit is unambiguous.
Return exactly:
{{
 "gold_usd_oz": number|null,
 "gold_change_pct": number|null,
 "usd_cad": number|null,
 "usd_cad_change_pct": number|null,
 "usd_irr_toman": number|null,
 "usd_irr_change_pct": number|null,
 "iran_gold18_toman_g": number|null,
 "iran_gold18_change_pct": number|null,
 "emami_coin_toman": number|null,
 "emami_coin_change_pct": number|null,
 "sources": {{
   "gold": string|null,
   "usd_cad": string|null,
   "usd_irr": string|null,
   "iran_gold18": string|null,
   "emami_coin": string|null
 }}
}}
Do not convert rial to toman unless the source unit is explicit. Do not use search snippets whose date/currentness is unclear.
"""


def flight_prompt(route: str, role: str) -> str:
    if route == "iran":
        dest = "Tehran IKA"
        horizon = "next 90 days, stay 7-30 nights"
    else:
        dest = "Vancouver YVR"
        horizon = "next 60 days, stay 3-7 nights"
    return f"""
Role: {role}. Time: {datetime.now().isoformat(timespec='minutes')}.
Find one currently visible CHEAPEST round-trip economy itinerary for 1 adult from Montreal YUL to {dest}, {horizon}.
Return exactly:
{{"price_cad": number|null, "outbound": "YYYY-MM-DD"|null, "return": "YYYY-MM-DD"|null, "airline": string|null, "stops": string|null, "source_url": string|null, "source_name": string|null}}
The total CAD price and both dates must be visible for the same itinerary. Reject teaser/from prices and one-way prices. If not verifiable, return null fields.
"""


def as_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def agree(a, b, rel_tol):
    a, b = as_float(a), as_float(b)
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    if abs(a - b) / ((a + b) / 2.0) > rel_tol:
        return None
    return (a + b) / 2.0


def pct_agree(a, b, abs_tol=0.35):
    a, b = as_float(a), as_float(b)
    if a is None or b is None:
        return None
    return (a + b) / 2.0 if abs(a - b) <= abs_tol else None


def guard_jump(name, new_value, old_value):
    if new_value is None or old_value is None:
        return new_value
    limits = {
        "gold_usd_oz": 0.03,
        "usd_cad": 0.015,
        "usd_irr_toman": 0.05,
        "iran_gold18_toman_g": 0.06,
        "emami_coin_toman": 0.06,
    }
    lim = limits.get(name)
    if not lim:
        return new_value
    try:
        if abs(float(new_value) - float(old_value)) / float(old_value) > lim:
            return old_value
    except Exception:
        pass
    return new_value


def reconcile_markets(a: dict, b: dict, old: dict | None) -> dict:
    out = {}
    out["gold_usd_oz"] = agree(a.get("gold_usd_oz"), b.get("gold_usd_oz"), 0.01)
    out["gold_change_pct"] = pct_agree(a.get("gold_change_pct"), b.get("gold_change_pct"), 0.5)
    out["usd_cad"] = agree(a.get("usd_cad"), b.get("usd_cad"), 0.004)
    out["usd_cad_change_pct"] = pct_agree(a.get("usd_cad_change_pct"), b.get("usd_cad_change_pct"), 0.25)
    out["usd_irr_toman"] = agree(a.get("usd_irr_toman"), b.get("usd_irr_toman"), 0.02)
    out["usd_irr_change_pct"] = pct_agree(a.get("usd_irr_change_pct"), b.get("usd_irr_change_pct"), 0.6)
    out["iran_gold18_toman_g"] = agree(a.get("iran_gold18_toman_g"), b.get("iran_gold18_toman_g"), 0.02)
    out["iran_gold18_change_pct"] = pct_agree(a.get("iran_gold18_change_pct"), b.get("iran_gold18_change_pct"), 0.6)
    out["emami_coin_toman"] = agree(a.get("emami_coin_toman"), b.get("emami_coin_toman"), 0.025)
    out["emami_coin_change_pct"] = pct_agree(a.get("emami_coin_change_pct"), b.get("emami_coin_change_pct"), 0.8)

    old = old or {}
    for k in ["gold_usd_oz", "usd_cad", "usd_irr_toman", "iran_gold18_toman_g", "emami_coin_toman"]:
        out[k] = guard_jump(k, out.get(k), old.get(k))

    if out.get("usd_cad"):
        out["cad_usd"] = 1.0 / out["usd_cad"]
    else:
        out["cad_usd"] = None
    if out.get("usd_irr_toman") and out.get("usd_cad"):
        out["cad_irr_toman"] = out["usd_irr_toman"] / out["usd_cad"]
    else:
        out["cad_irr_toman"] = None
    out["cad_irr_change_pct"] = None

    g, fx, g18 = out.get("gold_usd_oz"), out.get("usd_irr_toman"), out.get("iran_gold18_toman_g")
    if g and fx and g18:
        theoretical = (g / 31.1034768) * 0.75 * fx
        ratio = g18 / theoretical if theoretical else 0
        if ratio < 0.75 or ratio > 1.35:
            out["iran_gold18_toman_g"] = old.get("iran_gold18_toman_g")
            out["iran_gold18_change_pct"] = None

    srcs = []
    for obj in (a.get("sources") or {}, b.get("sources") or {}):
        for v in obj.values():
            if v and v not in srcs:
                srcs.append(v)
    out["sources"] = srcs[:8]
    return out


def verify_flight(route: str) -> dict:
    p = openai_json(flight_prompt(route, "primary search"), 1500)
    v = openai_json(flight_prompt(route, "independent verification search; try a different source/provider"), 1500)
    required = ["price_cad", "outbound", "return", "source_url"]
    if any(not p.get(k) for k in required) or any(not v.get(k) for k in required):
        return {}
    if p.get("outbound") != v.get("outbound") or p.get("return") != v.get("return"):
        return {}
    price = agree(p.get("price_cad"), v.get("price_cad"), 0.10)
    if price is None:
        return {}
    return {
        "price_cad": round(price),
        "outbound": p.get("outbound"),
        "return": p.get("return"),
        "airline": p.get("airline") or v.get("airline"),
        "stops": p.get("stops") or v.get("stops"),
        "source_url": p.get("source_url"),
        "source_name": p.get("source_name") or v.get("source_name"),
    }


def build_verified_snapshot(old=None) -> dict:
    a = openai_json(market_prompt("primary market collector"), 2200)
    b = openai_json(market_prompt("independent verifier; use different sources where possible"), 2200)
    market = reconcile_markets(a, b, old)
    market["iran_flight"] = verify_flight("iran")
    market["vancouver_flight"] = verify_flight("vancouver")
    alerts = []
    if old:
        for key, label, threshold in [
            ("gold_usd_oz", "طلای جهانی", 0.02),
            ("usd_cad", "USD/CAD", 0.01),
            ("usd_irr_toman", "دلار آزاد ایران", 0.03),
            ("iran_gold18_toman_g", "طلای ۱۸ عیار", 0.03),
        ]:
            n, o = market.get(key), old.get(key)
            if n and o:
                move = (n - o) / o
                if abs(move) >= threshold:
                    alerts.append(f"{label}: {move:+.1%}")
    market["alerts"] = alerts[:2]
    market["verified_at"] = now_label()
    return market


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
    try:
        return get_display(arabic_reshaper.reshape(str(text)))
    except Exception:
        return str(text)


def fmt_num(value, decimals=0):
    if value is None:
        return "—"
    try:
        n = float(value)
        return f"{n:,.{decimals}f}" if decimals else f"{n:,.0f}"
    except Exception:
        return "—"


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
        if v > 0: return (34, 214, 110)
        if v < 0: return (255, 72, 72)
    except Exception:
        pass
    return (160, 170, 180)


def rounded(draw, box, radius=24, fill=(12, 22, 31), outline=(45, 65, 78), width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def center_text(draw, box, text, fnt, fill):
    x1, y1, x2, y2 = box
    b = draw.textbbox((0, 0), text, font=fnt)
    w, h = b[2] - b[0], b[3] - b[1]
    draw.text(((x1 + x2 - w) / 2, (y1 + y2 - h) / 2), text, font=fnt, fill=fill)


def card_metric(draw, box, title, value, subtitle="", change=None, accent=(46, 204, 113)):
    rounded(draw, box, fill=(10, 19, 27), outline=accent, width=2)
    x1, y1, x2, y2 = box
    draw.text((x1 + 22, y1 + 18), rtl(title), font=font(25, True), fill=(236, 241, 245))
    draw.text((x1 + 22, y1 + 66), value, font=font(39, True), fill=(245, 248, 250))
    if subtitle:
        draw.text((x1 + 22, y1 + 118), rtl(subtitle), font=font(20), fill=(165, 178, 190))
    if change is not None:
        draw.text((x1 + 22, y2 - 40), fmt_pct(change), font=font(20, True), fill=color_for_change(change))


def flight_card(draw, box, title, flight, accent):
    rounded(draw, box, fill=(9, 20, 30), outline=accent, width=2)
    x1, y1, x2, y2 = box
    draw.text((x1 + 22, y1 + 18), rtl(title), font=font(26, True), fill=(238, 243, 247))
    price = flight.get("price_cad") if isinstance(flight, dict) else None
    draw.text((x1 + 22, y1 + 70), f"C${fmt_num(price)}" if price is not None else "—", font=font(44, True), fill=accent)
    out, ret = (flight or {}).get("outbound") or "—", (flight or {}).get("return") or "—"
    draw.text((x1 + 22, y1 + 132), f"{out}  →  {ret}", font=font(21), fill=(220, 226, 231))
    airline, stops = (flight or {}).get("airline") or "—", (flight or {}).get("stops") or "—"
    draw.text((x1 + 22, y1 + 171), f"{airline}  •  {stops}", font=font(19), fill=(164, 178, 190))


def render_dashboard(snapshot: dict) -> bytes:
    W, H = 1080, 1500
    img = Image.new("RGB", (W, H), (4, 10, 16))
    d = ImageDraw.Draw(img)
    d.text((48, 34), "METRA", font=font(44, True), fill=(242, 245, 247))
    d.text((48, 82), "VERIFIED DASHBOARD", font=font(22, True), fill=(46, 204, 113))
    d.text((515, 48), snapshot.get("verified_at") or now_label(), font=font(22), fill=(190, 201, 210))
    rounded(d, (855, 34, 1035, 88), radius=18, fill=(8, 42, 28), outline=(46, 204, 113), width=2)
    center_text(d, (855, 34, 1035, 88), "✓ VERIFIED", font(20, True), (46, 204, 113))
    card_metric(d, (40,145,360,420), "طلای جهانی", f"${fmt_num(snapshot.get('gold_usd_oz'))}", "هر اونس", snapshot.get("gold_change_pct"), (232,170,32))
    card_metric(d, (380,145,700,420), "USD / CAD", fmt_num(snapshot.get("usd_cad"),4), "", snapshot.get("usd_cad_change_pct"), (34,214,110))
    card_metric(d, (720,145,1040,420), "CAD / USD", fmt_num(snapshot.get("cad_usd"),4), "", None, (54,130,220))
    card_metric(d, (40,445,280,700), "USD / IRR", fmt_num(snapshot.get("usd_irr_toman")), "تومان", snapshot.get("usd_irr_change_pct"), (54,130,220))
    card_metric(d, (300,445,540,700), "CAD / IRR", fmt_num(snapshot.get("cad_irr_toman")), "تومان • محاسبه‌ای", None, (54,130,220))
    card_metric(d, (560,445,800,700), "طلای 18 عیار", fmt_num(snapshot.get("iran_gold18_toman_g")), "تومان / گرم", snapshot.get("iran_gold18_change_pct"), (34,214,110))
    card_metric(d, (820,445,1040,700), "سکه امامی", fmt_num(snapshot.get("emami_coin_toman")), "تومان", snapshot.get("emami_coin_change_pct"), (34,214,110))
    d.text((46,740), rtl("ارزان‌ترین بلیت تأییدشده"), font=font(30, True), fill=(242,245,247))
    flight_card(d, (40,800,520,1085), "مونترال ⇄ تهران", snapshot.get("iran_flight") or {}, (56,139,253))
    flight_card(d, (560,800,1040,1085), "مونترال ⇄ ونکوور", snapshot.get("vancouver_flight") or {}, (34,214,110))
    rounded(d, (40,1120,1040,1310), fill=(8,18,25), outline=(44,60,72), width=2)
    d.text((64,1143), rtl("هشدارهای مهم"), font=font(28, True), fill=(242,245,247))
    alerts = snapshot.get("alerts") or []
    if not alerts:
        d.text((64,1202), rtl("مورد مهمی وجود ندارد."), font=font(24), fill=(34,214,110))
    else:
        y = 1195
        for a in alerts[:2]:
            d.text((64,y), rtl("• " + str(a)), font=font(23), fill=(235,238,242))
            y += 42
    d.text((48,1350), rtl("اعداد فقط پس از تطبیق دو منبع نمایش داده می‌شوند؛ موارد نامطمئن با — پنهان می‌شوند."), font=font(18), fill=(135,148,158))
    d.text((48,1392), rtl("CAD/IRR از USD/IRR ÷ USD/CAD محاسبه می‌شود."), font=font(18), fill=(135,148,158))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def inline_buttons(snapshot):
    rows = []
    iurl = (snapshot.get("iran_flight") or {}).get("source_url")
    vurl = (snapshot.get("vancouver_flight") or {}).get("source_url")
    if iurl and vurl:
        rows.append([{"text":"✈️ تهران","url":iurl},{"text":"✈️ ونکوور","url":vurl}])
    elif iurl:
        rows.append([{"text":"✈️ تهران","url":iurl}])
    elif vurl:
        rows.append([{"text":"✈️ ونکوور","url":vurl}])
    return rows


def send_dashboard(chat_id):
    with LOCK:
        snap = SNAPSHOT
    if not snap:
        telegram_send_message("⏳ داده تأییدشده هنوز آماده نیست. در حال بررسی دو منبع مستقل...", chat_id, True)
        trigger_refresh(chat_id)
        return
    telegram_send_photo(render_dashboard(snap), "✅ فقط داده‌های تأییدشده نمایش داده شده‌اند.", chat_id, inline_buttons(snap))


def refresh_worker(notify_chat=None):
    global SNAPSHOT, SNAPSHOT_TIME, REFRESHING
    try:
        with LOCK:
            old = dict(SNAPSHOT) if SNAPSHOT else None
        new = build_verified_snapshot(old)
        with LOCK:
            SNAPSHOT, SNAPSHOT_TIME = new, time.time()
        if notify_chat:
            send_dashboard(notify_chat)
    except Exception as exc:
        print(f"[{VERSION}] refresh error: {type(exc).__name__}: {exc}", flush=True)
        if notify_chat:
            telegram_send_message("⚠️ بررسی دو منبع کامل نشد؛ داده قبلی حفظ شد.", notify_chat, True)
    finally:
        with LOCK:
            REFRESHING = False


def trigger_refresh(notify_chat=None):
    global REFRESHING
    with LOCK:
        if REFRESHING:
            return False
        REFRESHING = True
    EXECUTOR.submit(refresh_worker, notify_chat)
    return True


def handle_message(msg):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id or chat_id != TELEGRAM_CHAT_ID:
        return
    raw = (msg.get("text") or "").strip()
    low = raw.lower()
    if low in {"/start","/help","hello","hi"}:
        telegram_send_message("✅ Metra Verified Dashboard\nعدد مشکوک نمایش داده نمی‌شود؛ صحت از کامل‌بودن مهم‌تر است.", chat_id, True)
    elif raw == "📊 داشبورد تأییدشده" or low in {"/dashboard","📊 داشبورد فوری"}:
        send_dashboard(chat_id)
    elif raw == "🔄 بررسی مجدد" or low in {"/refresh","🔄 بروزرسانی"}:
        if trigger_refresh(chat_id):
            telegram_send_message("🔎 تطبیق دو منبع مستقل شروع شد. نتیجه فقط پس از تأیید ارسال می‌شود.", chat_id, True)
        else:
            telegram_send_message("⏳ بررسی از قبل در حال انجام است.", chat_id, True)
    elif raw in {"💰 ارز و طلا","✈️ بلیط‌ها","🚨 هشدارها"}:
        send_dashboard(chat_id)
    else:
        telegram_send_message("یکی از دکمه‌ها را انتخاب کن.", chat_id, True)


def scheduler_loop():
    while True:
        try:
            with LOCK:
                age = time.time() - SNAPSHOT_TIME if SNAPSHOT_TIME else 10**9
            if age > TTL:
                trigger_refresh()
        except Exception as exc:
            print(f"[{VERSION}] scheduler error: {exc}", flush=True)
        time.sleep(60)


def polling_loop():
    offset = None
    print(f"[{VERSION}] polling started", flush=True)
    while True:
        try:
            data = get_updates(offset)
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                if u.get("message"):
                    handle_message(u["message"])
        except Exception as exc:
            print(f"[{VERSION}] telegram loop error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(2)


def startup():
    print(f"[{VERSION}] START", flush=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    trigger_refresh()
    try:
        telegram_send_message("✅ V8 فعال شد — از این پس فقط داده‌های دو-منبعی و سازگار نمایش داده می‌شوند.", TELEGRAM_CHAT_ID, True)
    except Exception as exc:
        print(f"[{VERSION}] startup telegram error: {exc}", flush=True)
    polling_loop()


if __name__ == "__main__":
    startup()
