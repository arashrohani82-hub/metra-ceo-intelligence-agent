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

VERSION = "CEO-BOT-V11-DIRECT-MARKETS"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=2)
SNAPSHOT = {}
REFRESHING = set()

MARKET_TTL = 15 * 60
MARKET_RETRY_DELAY = 10 * 60
FLIGHT_TTL = 12 * 60 * 60
OPENAI_COOLDOWN = 6 * 60 * 60

LAST_MARKET_SUCCESS = 0
LAST_MARKET_ATTEMPT = 0
LAST_FLIGHT_ATTEMPT = 0
OPENAI_BLOCKED_UNTIL = 0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MetraCEO/11.0; +https://github.com/arashrohani82-hub/metra-ceo-intelligence-agent)",
    "Accept-Language": "fa,en;q=0.8",
}

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


def now_label():
    return datetime.now().strftime("%d %b %Y • %H:%M")


def fv(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def pct_change(new, old):
    if not new or not old:
        return None
    try:
        return (float(new) / float(old) - 1.0) * 100.0
    except Exception:
        return None


def parse_number(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def safe_get(url, *, timeout=20, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
    r.raise_for_status()
    return r


def fetch_tgju_profile(slug, unit_divisor=1.0):
    url = f"https://www.tgju.org/profile/{slug}"
    text = safe_get(url).text

    m = re.search(r"نرخ\s*فعلی\s*::?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)
    if not m:
        # Fallback for rendered/plain-text variants.
        plain = re.sub(r"<[^>]+>", " ", text)
        plain = re.sub(r"\s+", " ", plain)
        m = re.search(r"نرخ\s*فعلی\s*:?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", plain)
    if not m:
        raise RuntimeError(f"TGJU value not found: {slug}")

    value = parse_number(m.group(1))
    if value is None:
        raise RuntimeError(f"TGJU invalid value: {slug}")
    value /= unit_divisor

    change = None
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain)
    cm = re.search(r"درصد\s*تغییر\s*نسبت\s*به\s*روز\s*گذشته\s*([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%", plain)
    if cm:
        change = parse_number(cm.group(1))

    return value, change, url


def fetch_usdcad_boc():
    url = "https://www.bankofcanada.ca/valet/observations/FXCADUSD/json"
    data = safe_get(url, params={"recent": 2}).json()
    obs = data.get("observations") or []
    vals = []
    for row in obs:
        v = fv((row.get("FXCADUSD") or {}).get("v"))
        if v:
            vals.append(v)
    if not vals:
        raise RuntimeError("Bank of Canada FXCADUSD unavailable")
    current = vals[-1]
    change = pct_change(current, vals[-2]) if len(vals) >= 2 else None
    return current, change, url


def fetch_usdcad_yahoo():
    # Yahoo symbol CAD=X represents USD/CAD.
    url = "https://query1.finance.yahoo.com/v8/finance/chart/CAD=X"
    data = safe_get(url, params={"range": "2d", "interval": "5m"}).json()
    result = (((data.get("chart") or {}).get("result") or [None])[0] or {})
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
    vals = [fv(x) for x in closes if fv(x)]
    if not vals:
        raise RuntimeError("Yahoo USD/CAD unavailable")
    current = vals[-1]
    # Approximate daily move using the first usable point in this 2-day window.
    change = pct_change(current, vals[0]) if len(vals) > 1 else None
    return current, change, "https://finance.yahoo.com/quote/CAD=X/"


def sane(name, value):
    v = fv(value)
    if v is None:
        return False
    ranges = {
        "gold_usd_oz": (1000, 10000),
        "usd_cad": (1.0, 2.0),
        "usd_irr_toman": (20000, 1000000),
        "iran_gold18_toman_g": (1000000, 100000000),
        "emami_coin_toman": (10000000, 1000000000),
    }
    lo, hi = ranges[name]
    return lo <= v <= hi


def guard_jump(name, value, old_value):
    v, old = fv(value), fv(old_value)
    if v is None:
        return old, "old" if old is not None else "missing"
    if old is None:
        return v, "primary"
    limits = {
        "gold_usd_oz": 0.05,
        "usd_cad": 0.025,
        "usd_irr_toman": 0.10,
        "iran_gold18_toman_g": 0.10,
        "emami_coin_toman": 0.12,
    }
    if abs(v - old) / old > limits[name]:
        return old, "old-warning"
    return v, "primary"


def build_markets(old):
    out = {}
    status = {}
    sources = []
    errors = []

    # 1) Global gold ounce directly from TGJU global ounce profile.
    try:
        v, ch, src = fetch_tgju_profile("ons", 1.0)
        if not sane("gold_usd_oz", v):
            raise RuntimeError(f"gold out of range: {v}")
        out["gold_usd_oz"], status["gold_usd_oz"] = guard_jump("gold_usd_oz", v, old.get("gold_usd_oz"))
        out["gold_change_pct"] = ch
        sources.append("TGJU Gold Ounce")
    except Exception as e:
        errors.append(f"gold:{type(e).__name__}")
        out["gold_usd_oz"] = old.get("gold_usd_oz")
        out["gold_change_pct"] = old.get("gold_change_pct")
        status["gold_usd_oz"] = "old" if out["gold_usd_oz"] else "missing"

    # 2) USD/CAD directly from Bank of Canada; Yahoo only as a fallback.
    try:
        try:
            v, ch, src = fetch_usdcad_boc()
            src_name = "Bank of Canada"
        except Exception:
            v, ch, src = fetch_usdcad_yahoo()
            src_name = "Yahoo Finance"
        if not sane("usd_cad", v):
            raise RuntimeError(f"USD/CAD out of range: {v}")
        out["usd_cad"], status["usd_cad"] = guard_jump("usd_cad", v, old.get("usd_cad"))
        out["usd_cad_change_pct"] = ch
        sources.append(src_name)
    except Exception as e:
        errors.append(f"fx:{type(e).__name__}")
        out["usd_cad"] = old.get("usd_cad")
        out["usd_cad_change_pct"] = old.get("usd_cad_change_pct")
        status["usd_cad"] = "old" if out["usd_cad"] else "missing"

    # 3) Iran free-market USD, 18K gold and Emami coin directly from TGJU.
    tgju_specs = [
        ("usd_irr_toman", "price_dollar_rl", 10.0, "usd_irr_change_pct", "TGJU Free USD"),
        ("iran_gold18_toman_g", "geram18", 10.0, "iran_gold18_change_pct", "TGJU 18K Gold"),
        ("emami_coin_toman", "sekee", 10.0, "emami_coin_change_pct", "TGJU Emami Coin"),
    ]
    for key, slug, divisor, chkey, src_name in tgju_specs:
        try:
            v, ch, _ = fetch_tgju_profile(slug, divisor)
            if not sane(key, v):
                raise RuntimeError(f"{key} out of range: {v}")
            out[key], status[key] = guard_jump(key, v, old.get(key))
            out[chkey] = ch
            sources.append(src_name)
        except Exception as e:
            errors.append(f"{key}:{type(e).__name__}")
            out[key] = old.get(key)
            out[chkey] = old.get(chkey)
            status[key] = "old" if out[key] else "missing"

    if out.get("usd_cad"):
        out["cad_usd"] = 1.0 / out["usd_cad"]
    else:
        out["cad_usd"] = old.get("cad_usd")

    if out.get("usd_irr_toman") and out.get("usd_cad"):
        out["cad_irr_toman"] = out["usd_irr_toman"] / out["usd_cad"]
    else:
        out["cad_irr_toman"] = old.get("cad_irr_toman")

    out["status"] = status
    out["sources"] = list(dict.fromkeys(sources))[:6]
    out["market_errors"] = errors
    out["markets_at"] = now_label()
    out["updated_at"] = now_label()
    return out


# ---------------- Flights: OpenAI is isolated here only ----------------

def parse_json_text(text):
    text = re.sub(r"^```(?:json)?\s*", "", (text or "").strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            return json.loads(text[s:e + 1])
        raise


def openai_json(prompt, max_tokens=1400, timeout=90):
    global OPENAI_BLOCKED_UNTIL
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")
    if time.time() < OPENAI_BLOCKED_UNTIL:
        raise RuntimeError("OpenAI cooldown active")

    payload = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_tokens,
        "instructions": "Return only valid JSON. Never invent a flight price. Accept only a visible round-trip total with exact dates for the same itinerary.",
        "input": prompt,
    }
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if r.status_code == 429:
        OPENAI_BLOCKED_UNTIL = time.time() + OPENAI_COOLDOWN
        raise RuntimeError("OpenAI 429; 6h cooldown activated")
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


def flight_prompt(route):
    if route == "iran":
        dest, horizon = "Tehran IKA", "within the next 90 days, stay 7-30 nights"
    else:
        dest, horizon = "Vancouver YVR", "within the next 60 days, stay 3-7 nights"
    return f"""
Current time: {datetime.now().isoformat(timespec='minutes')}.
Find the cheapest CURRENTLY VISIBLE round-trip economy itinerary for 1 adult from Montreal YUL to {dest}, {horizon}.
Return exactly:
{{"price_cad": number|null, "outbound": "YYYY-MM-DD"|null, "return": "YYYY-MM-DD"|null, "airline": string|null, "stops": string|null, "source_url": string|null, "source_name": string|null}}
Price must be the total round-trip CAD price and both dates must belong to the same itinerary. Reject teaser/from and one-way prices.
"""


def build_flight(route, old_flight):
    raw = openai_json(flight_prompt(route))
    price = fv(raw.get("price_cad"))
    if not price or price < 50 or price > 10000 or not raw.get("outbound") or not raw.get("return") or not raw.get("source_url"):
        return old_flight or {}, "old" if old_flight else "missing"
    return {
        "price_cad": round(price),
        "outbound": raw.get("outbound"),
        "return": raw.get("return"),
        "airline": raw.get("airline") or "N/A",
        "stops": raw.get("stops") or "N/A",
        "source_url": raw.get("source_url"),
        "source_name": raw.get("source_name") or "",
    }, "primary"


def merge_snapshot(part):
    with LOCK:
        SNAPSHOT.update(part)
        SNAPSHOT["updated_at"] = now_label()


def telegram_send_message(text, chat_id=TELEGRAM_CHAT_ID, menu=True):
    data = {"chat_id": chat_id, "text": text}
    if menu:
        data["reply_markup"] = json.dumps(MAIN_KEYBOARD, ensure_ascii=False)
    r = requests.post(f"{TELEGRAM_API}/sendMessage", data=data, timeout=25)
    r.raise_for_status()


def telegram_send_photo(image_bytes, caption="", chat_id=TELEGRAM_CHAT_ID, inline_keyboard=None):
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


def fmt(v, d=0):
    if v is None:
        return "—"
    try:
        n = float(v)
        return f"{n:,.{d}f}" if d else f"{n:,.0f}"
    except Exception:
        return "—"


def pct(v):
    if v is None:
        return ""
    try:
        v = float(v)
        return f"{'▲' if v > 0 else '▼' if v < 0 else '→'} {v:+.2f}%"
    except Exception:
        return ""


def cchange(v):
    try:
        return (34, 214, 110) if float(v) > 0 else (255, 72, 72) if float(v) < 0 else (160, 170, 180)
    except Exception:
        return (160, 170, 180)


def rounded(d, box, outline=(45, 65, 78)):
    d.rounded_rectangle(box, radius=22, fill=(10, 19, 27), outline=outline, width=2)


def badge(st):
    return "!" if "warning" in str(st) else "↺" if st == "old" else "•" if st in ("primary", None) else "—"


def metric(d, box, title, value, sub="", change=None, accent=(46, 204, 113), b=""):
    rounded(d, box, accent)
    x1, y1, x2, y2 = box
    d.text((x1 + 18, y1 + 16), rtl(title), font=font(25, True), fill=(236, 241, 245))
    d.text((x1 + 18, y1 + 62), value, font=font(39, True), fill=(245, 248, 250))
    if sub:
        d.text((x1 + 18, y1 + 112), rtl(sub), font=font(19), fill=(165, 178, 190))
    if change is not None:
        d.text((x1 + 18, y2 - 38), pct(change), font=font(20, True), fill=cchange(change))
    if b:
        d.text((x2 - 50, y1 + 16), b, font=font(18, True), fill=(180, 190, 200))


def flight_card(d, box, title, fl, accent, st):
    rounded(d, box, accent)
    x1, y1, x2, y2 = box
    d.text((x1 + 20, y1 + 18), rtl(title), font=font(25, True), fill=(238, 243, 247))
    p = (fl or {}).get("price_cad")
    d.text((x1 + 20, y1 + 66), f"C${fmt(p)}" if p else "—", font=font(43, True), fill=accent)
    d.text((x1 + 20, y1 + 126), f"{(fl or {}).get('outbound') or '—'}  →  {(fl or {}).get('return') or '—'}", font=font(20), fill=(220, 226, 231))
    d.text((x1 + 20, y1 + 162), f"{(fl or {}).get('airline') or '—'}  •  {(fl or {}).get('stops') or '—'}", font=font(18), fill=(164, 178, 190))
    d.text((x2 - 50, y1 + 18), badge(st), font=font(18, True), fill=(180, 190, 200))


def render_dashboard(s):
    W, H = 1080, 1500
    img = Image.new("RGB", (W, H), (4, 10, 16))
    d = ImageDraw.Draw(img)
    d.text((48, 34), "METRA", font=font(44, True), fill=(242, 245, 247))
    d.text((48, 82), "DIRECT DATA DASHBOARD", font=font(22, True), fill=(46, 204, 113))
    d.text((505, 48), s.get("updated_at") or now_label(), font=font(22), fill=(190, 201, 210))
    rounded(d, (860, 34, 1035, 88), (46, 204, 113))
    d.text((881, 50), "● DIRECT", font=font(19, True), fill=(46, 204, 113))

    st = s.get("status") or {}
    metric(d, (40, 145, 360, 420), "طلای جهانی", f"${fmt(s.get('gold_usd_oz'))}", "دلار / اونس", s.get("gold_change_pct"), (232, 170, 32), badge(st.get("gold_usd_oz")))
    metric(d, (380, 145, 700, 420), "USD / CAD", fmt(s.get("usd_cad"), 4), "", s.get("usd_cad_change_pct"), (34, 214, 110), badge(st.get("usd_cad")))
    metric(d, (720, 145, 1040, 420), "CAD / USD", fmt(s.get("cad_usd"), 4), "محاسبه‌ای", None, (54, 130, 220), "=")

    metric(d, (40, 445, 280, 700), "USD / IRR", fmt(s.get("usd_irr_toman")), "تومان", s.get("usd_irr_change_pct"), (54, 130, 220), badge(st.get("usd_irr_toman")))
    metric(d, (300, 445, 540, 700), "CAD / IRR", fmt(s.get("cad_irr_toman")), "تومان • محاسبه‌ای", None, (54, 130, 220), "=")
    metric(d, (560, 445, 800, 700), "طلای 18 عیار", fmt(s.get("iran_gold18_toman_g")), "تومان / گرم", s.get("iran_gold18_change_pct"), (34, 214, 110), badge(st.get("iran_gold18_toman_g")))
    metric(d, (820, 445, 1040, 700), "سکه امامی", fmt(s.get("emami_coin_toman")), "تومان", s.get("emami_coin_change_pct"), (34, 214, 110), badge(st.get("emami_coin_toman")))

    d.text((46, 740), rtl("ارزان‌ترین بلیت رفت و برگشت"), font=font(30, True), fill=(242, 245, 247))
    flight_card(d, (40, 800, 520, 1085), "مونترال ⇄ تهران", s.get("iran_flight") or {}, (56, 139, 253), s.get("iran_flight_status"))
    flight_card(d, (560, 800, 1040, 1085), "مونترال ⇄ ونکوور", s.get("vancouver_flight") or {}, (34, 214, 110), s.get("vancouver_flight_status"))

    rounded(d, (40, 1120, 1040, 1310))
    d.text((64, 1143), rtl("وضعیت داده"), font=font(28, True), fill=(242, 245, 247))
    errors = s.get("market_errors") or []
    if errors:
        d.text((64, 1200), rtl("برخی منابع موقتاً در دسترس نبودند؛ داده معتبر قبلی حفظ شده است."), font=font(21), fill=(255, 190, 60))
    else:
        d.text((64, 1200), rtl("بازار مستقیماً از منابع داده دریافت شده؛ AI در قیمت بازار دخالت ندارد."), font=font(21), fill=(34, 214, 110))

    src = ", ".join((s.get("sources") or [])[:5])
    if src:
        d.text((48, 1360), f"Sources: {src}", font=font(16), fill=(125, 138, 150))
    d.text((48, 1400), rtl("بازار: هر 15 دقیقه • بلیت: حداکثر هر 12 ساعت • ↺ داده معتبر قبلی"), font=font(17), fill=(125, 138, 150))

    b = io.BytesIO()
    img.save(b, format="PNG", optimize=True)
    return b.getvalue()


def inline_buttons(s):
    rows = []
    for text, key in (("✈️ بررسی تهران", "iran_flight"), ("✈️ بررسی ونکوور", "vancouver_flight")):
        url = (s.get(key) or {}).get("source_url")
        if url:
            rows.append([{"text": text, "url": url}])
    return rows


def show_dashboard(chat_id):
    with LOCK:
        s = dict(SNAPSHOT)
    if not s:
        telegram_send_message("⏳ اولین داده بازار در حال دریافت مستقیم است.", chat_id, True)
        start_market_refresh(chat_id)
        return
    telegram_send_photo(render_dashboard(s), "📊 آخرین داده بازار", chat_id, inline_buttons(s))


def refresh_markets(notify=None):
    global LAST_MARKET_SUCCESS, LAST_MARKET_ATTEMPT
    try:
        LAST_MARKET_ATTEMPT = time.time()
        with LOCK:
            old = dict(SNAPSHOT)
        part = build_markets(old)
        merge_snapshot(part)
        LAST_MARKET_SUCCESS = time.time()
        print(f"[{VERSION}] market refresh OK sources={part.get('sources')} errors={part.get('market_errors')}", flush=True)
        if notify:
            show_dashboard(notify)
    except Exception as e:
        print(f"[{VERSION}] market refresh error: {type(e).__name__}: {e}", flush=True)
        if notify:
            telegram_send_message("⚠️ بازار موقتاً به‌روزرسانی نشد؛ درخواست یک‌دقیقه‌ای تکرار نمی‌شود.", notify, True)
    finally:
        with LOCK:
            REFRESHING.discard("markets")


def start_market_refresh(notify=None, force=False):
    now = time.time()
    with LOCK:
        if "markets" in REFRESHING:
            return False
        if not force and LAST_MARKET_ATTEMPT and now - LAST_MARKET_ATTEMPT < MARKET_RETRY_DELAY:
            return False
        REFRESHING.add("markets")
    EXECUTOR.submit(refresh_markets, notify)
    return True


def refresh_flights(notify=None):
    global LAST_FLIGHT_ATTEMPT
    key = "flights"
    try:
        LAST_FLIGHT_ATTEMPT = time.time()
        with LOCK:
            old = dict(SNAPSHOT)
        iran, iran_st = build_flight("iran", old.get("iran_flight"))
        merge_snapshot({"iran_flight": iran, "iran_flight_status": iran_st, "flights_at": now_label()})
        time.sleep(10)
        with LOCK:
            old2 = dict(SNAPSHOT)
        van, van_st = build_flight("vancouver", old2.get("vancouver_flight"))
        merge_snapshot({"vancouver_flight": van, "vancouver_flight_status": van_st, "flights_at": now_label()})
        if notify:
            show_dashboard(notify)
    except Exception as e:
        print(f"[{VERSION}] flight refresh: {type(e).__name__}: {e}", flush=True)
        if notify:
            telegram_send_message("⚠️ بلیت‌ها فعلاً به‌روزرسانی نشدند؛ بازار همچنان فعال است.", notify, True)
    finally:
        with LOCK:
            REFRESHING.discard(key)


def start_flight_refresh(notify=None, force=False):
    global LAST_FLIGHT_ATTEMPT
    now = time.time()
    with LOCK:
        if "flights" in REFRESHING:
            return False
        if not force and LAST_FLIGHT_ATTEMPT and now - LAST_FLIGHT_ATTEMPT < FLIGHT_TTL:
            return False
        REFRESHING.add("flights")
    EXECUTOR.submit(refresh_flights, notify)
    return True


def handle_message(m):
    chat_id = str(m.get("chat", {}).get("id", ""))
    if not chat_id or chat_id != TELEGRAM_CHAT_ID:
        return
    raw = (m.get("text") or "").strip()
    low = raw.lower()

    if low in {"/start", "/help", "hello", "hi"}:
        telegram_send_message("📊 Metra Dashboard V11\nقیمت‌های بازار مستقیم از منابع داده می‌آیند؛ OpenAI فقط برای جستجوی بلیت استفاده می‌شود.", chat_id, True)
    elif raw in {"📊 داشبورد", "💰 ارز و طلا"} or low == "/dashboard":
        show_dashboard(chat_id)
    elif raw == "✈️ بلیط‌ها":
        show_dashboard(chat_id)
        if start_flight_refresh(chat_id, force=True):
            telegram_send_message("✈️ جستجوی بلیت شروع شد؛ نتیجه جداگانه به‌روزرسانی می‌شود.", chat_id, True)
        else:
            telegram_send_message("⏳ جستجوی بلیت در حال انجام است یا در cooldown قرار دارد.", chat_id, True)
    elif raw == "🔄 بررسی مجدد" or low == "/refresh":
        started = start_market_refresh(chat_id, force=True)
        telegram_send_message("🔄 بازار مستقیم در حال به‌روزرسانی است." if started else "⏳ به‌روزرسانی بازار از قبل در حال انجام است.", chat_id, True)
    elif raw == "🚨 هشدارها":
        with LOCK:
            s = dict(SNAPSHOT)
        warnings = [k for k, v in (s.get("status") or {}).items() if "warning" in str(v)]
        errors = s.get("market_errors") or []
        if warnings or errors:
            telegram_send_message("⚠️ کنترل داده: " + ", ".join(warnings + errors), chat_id, True)
        else:
            telegram_send_message("✅ هشدار داده مهمی وجود ندارد.", chat_id, True)
    else:
        telegram_send_message("یکی از دکمه‌های منو را انتخاب کن.", chat_id, True)


def scheduler_loop():
    # Important: never retry failed market requests every minute.
    while True:
        try:
            now = time.time()
            market_due = (not LAST_MARKET_SUCCESS and (not LAST_MARKET_ATTEMPT or now - LAST_MARKET_ATTEMPT >= MARKET_RETRY_DELAY)) or (LAST_MARKET_SUCCESS and now - LAST_MARKET_SUCCESS >= MARKET_TTL)
            if market_due:
                start_market_refresh()
            if LAST_FLIGHT_ATTEMPT and now - LAST_FLIGHT_ATTEMPT >= FLIGHT_TTL:
                start_flight_refresh()
        except Exception as e:
            print(f"[{VERSION}] scheduler: {type(e).__name__}: {e}", flush=True)
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
        except Exception as e:
            print(f"[{VERSION}] telegram: {type(e).__name__}: {e}", flush=True)
            time.sleep(2)


def startup():
    print(f"[{VERSION}] START", flush=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    start_market_refresh(force=True)
    try:
        telegram_send_message("✅ V11 فعال شد — قیمت بازار مستقیم است و درخواست یک‌دقیقه‌ای OpenAI حذف شد.", TELEGRAM_CHAT_ID, True)
    except Exception as e:
        print(f"[{VERSION}] startup telegram: {e}", flush=True)
    polling_loop()


if __name__ == "__main__":
    startup()
