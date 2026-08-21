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

VERSION = "CEO-BOT-V9-TRUSTED-PRACTICAL"

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
        [{"text": "📊 داشبورد"}],
        [{"text": "💰 ارز و طلا"}, {"text": "✈️ بلیط‌ها"}],
        [{"text": "🚨 هشدارها"}, {"text": "🔄 بررسی مجدد"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "یک گزینه را انتخاب کن",
}

SYSTEM_PROMPT = """
You are a high-accuracy market and travel data collector. Return ONLY valid JSON.
Do not write prose or markdown.
Accuracy matters more than completeness, but do NOT discard a good primary-source value merely because a second source is unavailable.
For each metric:
- choose one clearly current PRIMARY value from a reputable source;
- optionally provide one SECONDARY value for cross-checking;
- include source names and whether the value was directly visible.
Never guess numbers.
Iran FX must be FREE-MARKET TOMAN, not official rate.
Flights must be current round-trip economy fares for one adult from Montreal YUL, with total CAD price and exact outbound/return dates for the same itinerary.
Reject teaser/from prices without matching dates, monthly estimates, and one-way fares.
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


def openai_json(prompt: str, max_tokens=2400) -> dict:
    payload = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_tokens,
        "instructions": SYSTEM_PROMPT,
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


def market_prompt() -> str:
    return f"""
Time now: {datetime.now().isoformat(timespec='minutes')}.
Collect CURRENT market data and return exactly this JSON shape:
{{
  "gold": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}},
  "usd_cad": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}},
  "usd_irr_toman": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}},
  "iran_gold18_toman_g": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}},
  "emami_coin_toman": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}}
}}
Rules:
- gold = XAU/USD spot per troy ounce.
- USD/CAD = 1 USD in CAD.
- USD/IRR = current FREE-MARKET TOMAN value for 1 USD.
- Iran 18K gold = TOMAN per gram.
- Emami coin = TOMAN.
- Prefer clearly current pages. For Canada FX prefer Bank of Canada or established market providers. For Iran use reputable free-market sources such as TGJU, Bonbast, DolarChand when unit/date are explicit.
- If only one source is solid, set secondary to null rather than dropping the primary.
"""


def flight_prompt(route: str) -> str:
    if route == "iran":
        dest = "Tehran IKA"
        horizon = "within the next 90 days, stay 7-30 nights"
    else:
        dest = "Vancouver YVR"
        horizon = "within the next 60 days, stay 3-7 nights"
    return f"""
Time now: {datetime.now().isoformat(timespec='minutes')}.
Find the cheapest CURRENTLY VISIBLE round-trip economy itinerary for 1 adult from Montreal YUL to {dest}, {horizon}.
Return exactly:
{{
  "primary": {{"price_cad": number|null, "outbound": "YYYY-MM-DD"|null, "return": "YYYY-MM-DD"|null, "airline": string|null, "stops": string|null, "source_url": string|null, "source_name": string|null}},
  "secondary": {{"price_cad": number|null, "outbound": "YYYY-MM-DD"|null, "return": "YYYY-MM-DD"|null, "source_name": string|null}}
}}
Only accept a primary fare when total CAD price and both dates are visible for the same itinerary. If no solid fare exists, use nulls.
"""


def f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def within(a, b, tol):
    a, b = f(a), f(b)
    if a is None or b is None or a <= 0 or b <= 0:
        return False
    return abs(a - b) / ((a + b) / 2) <= tol


def sane(name, value):
    value = f(value)
    if value is None:
        return False
    ranges = {
        "gold_usd_oz": (1000, 10000),
        "usd_cad": (1.0, 2.0),
        "usd_irr_toman": (20000, 1000000),
        "iran_gold18_toman_g": (1000000, 100000000),
        "emami_coin_toman": (10000000, 1000000000),
    }
    lo, hi = ranges[name]
    return lo <= value <= hi


def choose_metric(name, item, old, tolerance):
    p = f((item or {}).get("primary"))
    s = f((item or {}).get("secondary"))
    oldv = f((old or {}).get(name))
    status = "primary"

    if not sane(name, p):
        return (oldv if oldv is not None else None), "old" if oldv is not None else "missing"

    if s is not None and sane(name, s):
        if within(p, s, tolerance):
            p = (p + s) / 2
            status = "verified"
        else:
            status = "warning"

    jump_limits = {
        "gold_usd_oz": 0.04,
        "usd_cad": 0.02,
        "usd_irr_toman": 0.08,
        "iran_gold18_toman_g": 0.08,
        "emami_coin_toman": 0.10,
    }
    if oldv and abs(p - oldv) / oldv > jump_limits[name]:
        return oldv, "old-warning"

    return p, status


def build_market_snapshot(old=None):
    raw = openai_json(market_prompt(), 2500)
    out = {}
    statuses = {}
    specs = [
        ("gold_usd_oz", "gold", 0.012),
        ("usd_cad", "usd_cad", 0.006),
        ("usd_irr_toman", "usd_irr_toman", 0.035),
        ("iran_gold18_toman_g", "iran_gold18_toman_g", 0.035),
        ("emami_coin_toman", "emami_coin_toman", 0.04),
    ]
    for key, rawkey, tol in specs:
        out[key], statuses[key] = choose_metric(key, raw.get(rawkey) or {}, old or {}, tol)
        out[key.replace("_usd_oz", "_change_pct") if key == "gold_usd_oz" else key.replace("_toman_g", "_change_pct") if key == "iran_gold18_toman_g" else key.replace("_toman", "_change_pct") if key in ("usd_irr_toman", "emami_coin_toman") else "usd_cad_change_pct"] = f((raw.get(rawkey) or {}).get("change_pct"))

    if out.get("usd_cad"):
        out["cad_usd"] = 1 / out["usd_cad"]
    else:
        out["cad_usd"] = f((old or {}).get("cad_usd"))

    if out.get("usd_irr_toman") and out.get("usd_cad"):
        out["cad_irr_toman"] = out["usd_irr_toman"] / out["usd_cad"]
    else:
        out["cad_irr_toman"] = f((old or {}).get("cad_irr_toman"))
    out["cad_irr_change_pct"] = None

    out["status"] = statuses
    sources = []
    for rawkey in ("gold", "usd_cad", "usd_irr_toman", "iran_gold18_toman_g", "emami_coin_toman"):
        obj = raw.get(rawkey) or {}
        for k in ("primary_source", "secondary_source"):
            v = obj.get(k)
            if v and v not in sources:
                sources.append(v)
    out["sources"] = sources[:8]
    return out


def choose_flight(raw, old_flight=None):
    p = raw.get("primary") or {}
    s = raw.get("secondary") or {}
    required = [p.get("price_cad"), p.get("outbound"), p.get("return"), p.get("source_url")]
    if not all(required):
        return old_flight or {}, "old" if old_flight else "missing"
    price = f(p.get("price_cad"))
    if price is None or price < 50 or price > 10000:
        return old_flight or {}, "old" if old_flight else "missing"

    status = "primary"
    sp = f(s.get("price_cad"))
    if sp and s.get("outbound") == p.get("outbound") and s.get("return") == p.get("return"):
        if within(price, sp, 0.12):
            price = (price + sp) / 2
            status = "verified"
        else:
            status = "warning"

    return {
        "price_cad": round(price),
        "outbound": p.get("outbound"),
        "return": p.get("return"),
        "airline": p.get("airline") or "N/A",
        "stops": p.get("stops") or "N/A",
        "source_url": p.get("source_url"),
        "source_name": p.get("source_name") or "",
    }, status


def build_snapshot(old=None):
    out = build_market_snapshot(old)
    iran_raw = openai_json(flight_prompt("iran"), 1600)
    van_raw = openai_json(flight_prompt("vancouver"), 1600)
    out["iran_flight"], out["iran_flight_status"] = choose_flight(iran_raw, (old or {}).get("iran_flight"))
    out["vancouver_flight"], out["vancouver_flight_status"] = choose_flight(van_raw, (old or {}).get("vancouver_flight"))

    warnings = []
    for k, st in (out.get("status") or {}).items():
        if "warning" in st:
            warnings.append(k)
    if "warning" in out.get("iran_flight_status", ""):
        warnings.append("iran_flight")
    if "warning" in out.get("vancouver_flight_status", ""):
        warnings.append("vancouver_flight")
    out["warnings"] = warnings
    out["verified_at"] = now_label()
    return out


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
        if v > 0:
            return (34, 214, 110)
        if v < 0:
            return (255, 72, 72)
    except Exception:
        pass
    return (160, 170, 180)


def rounded(draw, box, radius=22, fill=(10, 19, 27), outline=(45, 65, 78), width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def card_metric(draw, box, title, value, subtitle="", change=None, accent=(46, 204, 113), badge=""):
    rounded(draw, box, fill=(10, 19, 27), outline=accent, width=2)
    x1, y1, x2, y2 = box
    draw.text((x1 + 18, y1 + 16), rtl(title), font=font(25, True), fill=(236, 241, 245))
    draw.text((x1 + 18, y1 + 62), value, font=font(39, True), fill=(245, 248, 250))
    if subtitle:
        draw.text((x1 + 18, y1 + 112), rtl(subtitle), font=font(20), fill=(165, 178, 190))
    if change is not None:
        draw.text((x1 + 18, y2 - 38), fmt_pct(change), font=font(20, True), fill=color_for_change(change))
    if badge:
        draw.text((x2 - 70, y1 + 16), badge, font=font(18, True), fill=(180, 190, 200))


def flight_card(draw, box, title, flight, accent, badge=""):
    rounded(draw, box, fill=(9, 20, 30), outline=accent, width=2)
    x1, y1, x2, y2 = box
    draw.text((x1 + 20, y1 + 18), rtl(title), font=font(25, True), fill=(238, 243, 247))
    price = flight.get("price_cad") if isinstance(flight, dict) else None
    draw.text((x1 + 20, y1 + 66), f"C${fmt_num(price)}" if price is not None else "—", font=font(43, True), fill=accent)
    out = (flight or {}).get("outbound") or "—"
    ret = (flight or {}).get("return") or "—"
    draw.text((x1 + 20, y1 + 126), f"{out}  →  {ret}", font=font(20), fill=(220, 226, 231))
    airline = (flight or {}).get("airline") or "N/A"
    stops = (flight or {}).get("stops") or "N/A"
    draw.text((x1 + 20, y1 + 162), f"{airline}  •  {stops}", font=font(18), fill=(164, 178, 190))
    if badge:
        draw.text((x2 - 82, y1 + 18), badge, font=font(18, True), fill=(180, 190, 200))


def badge_for(status):
    if status == "verified":
        return "✓"
    if status in ("warning", "old-warning"):
        return "!"
    if status == "old":
        return "↺"
    return "•"


def render_dashboard(snapshot: dict) -> bytes:
    W, H = 1080, 1500
    img = Image.new("RGB", (W, H), (4, 10, 16))
    d = ImageDraw.Draw(img)
    d.text((48, 34), "METRA", font=font(44, True), fill=(242, 245, 247))
    d.text((48, 82), "TRUSTED DASHBOARD", font=font(22, True), fill=(46, 204, 113))
    d.text((505, 48), snapshot.get("verified_at") or now_label(), font=font(22), fill=(190, 201, 210))
    rounded(d, (860, 34, 1035, 88), radius=18, fill=(8, 42, 28), outline=(46, 204, 113), width=2)
    label = "✓ CHECKED" if not snapshot.get("warnings") else "⚠ CHECKED"
    d.text((886, 49), label, font=font(20, True), fill=(46, 204, 113) if not snapshot.get("warnings") else (255, 190, 60))

    st = snapshot.get("status") or {}
    card_metric(d, (40, 145, 360, 420), "طلای جهانی", f"${fmt_num(snapshot.get('gold_usd_oz'))}", "هر اونس", snapshot.get("gold_change_pct"), (232, 170, 32), badge_for(st.get("gold_usd_oz")))
    card_metric(d, (380, 145, 700, 420), "USD / CAD", fmt_num(snapshot.get("usd_cad"), 4), "", snapshot.get("usd_cad_change_pct"), (34, 214, 110), badge_for(st.get("usd_cad")))
    card_metric(d, (720, 145, 1040, 420), "CAD / USD", fmt_num(snapshot.get("cad_usd"), 4), "محاسبه‌ای", None, (54, 130, 220), "=")

    card_metric(d, (40, 445, 280, 700), "USD / IRR", fmt_num(snapshot.get("usd_irr_toman")), "تومان", snapshot.get("usd_irr_change_pct"), (54, 130, 220), badge_for(st.get("usd_irr_toman")))
    card_metric(d, (300, 445, 540, 700), "CAD / IRR", fmt_num(snapshot.get("cad_irr_toman")), "تومان • محاسبه‌ای", None, (54, 130, 220), "=")
    card_metric(d, (560, 445, 800, 700), "طلای 18 عیار", fmt_num(snapshot.get("iran_gold18_toman_g")), "تومان / گرم", snapshot.get("iran_gold18_change_pct"), (34, 214, 110), badge_for(st.get("iran_gold18_toman_g")))
    card_metric(d, (820, 445, 1040, 700), "سکه امامی", fmt_num(snapshot.get("emami_coin_toman")), "تومان", snapshot.get("emami_coin_change_pct"), (34, 214, 110), badge_for(st.get("emami_coin_toman")))

    d.text((46, 740), rtl("ارزان‌ترین بلیت رفت و برگشت"), font=font(30, True), fill=(242, 245, 247))
    flight_card(d, (40, 800, 520, 1085), "مونترال ⇄ تهران", snapshot.get("iran_flight") or {}, (56, 139, 253), badge_for(snapshot.get("iran_flight_status")))
    flight_card(d, (560, 800, 1040, 1085), "مونترال ⇄ ونکوور", snapshot.get("vancouver_flight") or {}, (34, 214, 110), badge_for(snapshot.get("vancouver_flight_status")))

    rounded(d, (40, 1120, 1040, 1310), fill=(8, 18, 25), outline=(44, 60, 72), width=2)
    d.text((64, 1143), rtl("وضعیت اعتبار"), font=font(28, True), fill=(242, 245, 247))
    warns = snapshot.get("warnings") or []
    if not warns:
        d.text((64, 1200), rtl("اعداد اصلی بررسی شده‌اند؛ مقادیر محاسبه‌ای مشخص شده‌اند."), font=font(22), fill=(34, 214, 110))
    else:
        d.text((64, 1200), rtl("برخی آیتم‌ها اختلاف منبع داشتند و با ! مشخص شده‌اند."), font=font(22), fill=(255, 190, 60))

    srcs = ", ".join((snapshot.get("sources") or [])[:5])
    d.text((48, 1350), rtl("✓ منبع اصلی معتبر نمایش داده می‌شود؛ منبع دوم نقش کنترل دارد."), font=font(19), fill=(155, 168, 180))
    if srcs:
        d.text((48, 1390), f"Sources: {srcs}", font=font(16), fill=(125, 138, 150))
    d.text((48, 1430), rtl("↺ یعنی آخرین مقدار معتبر قبلی حفظ شده است."), font=font(17), fill=(125, 138, 150))

    b = io.BytesIO()
    img.save(b, format="PNG", optimize=True)
    return b.getvalue()


def inline_buttons(snapshot):
    rows = []
    ir = (snapshot.get("iran_flight") or {}).get("source_url")
    va = (snapshot.get("vancouver_flight") or {}).get("source_url")
    if ir:
        rows.append([{"text": "✈️ بررسی بلیت تهران", "url": ir}])
    if va:
        rows.append([{"text": "✈️ بررسی بلیت ونکوور", "url": va}])
    return rows


def refresh_worker(notify_chat=None):
    global SNAPSHOT, SNAPSHOT_TIME, REFRESHING
    try:
        with LOCK:
            old = dict(SNAPSHOT) if SNAPSHOT else None
        fresh = build_snapshot(old)
        with LOCK:
            SNAPSHOT = fresh
            SNAPSHOT_TIME = time.time()
        if notify_chat:
            telegram_send_photo(render_dashboard(fresh), "✅ داشبورد به‌روزرسانی شد.", notify_chat, inline_buttons(fresh))
    except Exception as exc:
        print(f"[{VERSION}] refresh error: {type(exc).__name__}: {exc}", flush=True)
        if notify_chat:
            telegram_send_message("⚠️ بروزرسانی کامل نشد؛ آخرین داده معتبر قبلی حفظ شد.", notify_chat, menu=True)
    finally:
        with LOCK:
            REFRESHING = False


def start_refresh(notify_chat=None):
    global REFRESHING
    with LOCK:
        if REFRESHING:
            return False
        REFRESHING = True
    EXECUTOR.submit(refresh_worker, notify_chat)
    return True


def show_dashboard(chat_id):
    with LOCK:
        snap = dict(SNAPSHOT) if SNAPSHOT else None
        age = time.time() - SNAPSHOT_TIME if SNAPSHOT_TIME else 10**9
    if snap:
        telegram_send_photo(render_dashboard(snap), "📊 داشبورد", chat_id, inline_buttons(snap))
        if age > TTL:
            start_refresh()
    else:
        start_refresh(chat_id)
        telegram_send_message("⏳ اولین داشبورد در حال آماده‌شدن است.", chat_id, menu=True)


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
        telegram_send_message("📊 Metra Trusted Dashboard\nعدد اصلی از منبع معتبر می‌آید؛ منبع دوم فقط برای کنترل است.", chat_id, menu=True)
    elif raw == "📊 داشبورد" or low == "/dashboard":
        show_dashboard(chat_id)
    elif raw == "💰 ارز و طلا":
        show_dashboard(chat_id)
    elif raw == "✈️ بلیط‌ها":
        show_dashboard(chat_id)
    elif raw == "🚨 هشدارها":
        with LOCK:
            snap = dict(SNAPSHOT) if SNAPSHOT else None
        if not snap:
            telegram_send_message("هنوز داده‌ای آماده نیست.", chat_id, menu=True)
        elif snap.get("warnings"):
            telegram_send_message("⚠️ برخی آیتم‌ها اختلاف منبع دارند و در داشبورد با ! مشخص شده‌اند.", chat_id, menu=True)
        else:
            telegram_send_message("✅ هشدار اعتبار مهمی وجود ندارد.", chat_id, menu=True)
    elif raw == "🔄 بررسی مجدد" or low == "/refresh":
        if start_refresh(chat_id):
            telegram_send_message("🔄 بررسی مجدد شروع شد؛ بعد از تکمیل تصویر جدید خودکار می‌آید.", chat_id, menu=True)
        else:
            telegram_send_message("⏳ بررسی در حال انجام است.", chat_id, menu=True)
    else:
        telegram_send_message("یکی از دکمه‌های منو را انتخاب کن.", chat_id, menu=True)


def scheduler_loop():
    while True:
        try:
            with LOCK:
                age = time.time() - SNAPSHOT_TIME if SNAPSHOT_TIME else 10**9
            if age > TTL:
                start_refresh()
        except Exception as exc:
            print(f"[{VERSION}] scheduler error: {exc}", flush=True)
        time.sleep(60)


def polling_loop():
    offset = None
    print(f"[{VERSION}] polling started", flush=True)
    while True:
        try:
            data = get_updates(offset)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if update.get("message"):
                    handle_message(update["message"])
        except Exception as exc:
            print(f"[{VERSION}] telegram error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(2)


def startup():
    print(f"[{VERSION}] START", flush=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    start_refresh()
    try:
        telegram_send_message("✅ V9 فعال شد — منبع اصلی نمایش داده می‌شود و منبع دوم نقش کنترل دارد.", TELEGRAM_CHAT_ID, menu=True)
    except Exception as exc:
        print(f"[{VERSION}] startup telegram error: {exc}", flush=True)
    polling_loop()


if __name__ == "__main__":
    startup()
