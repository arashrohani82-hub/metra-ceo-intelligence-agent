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

VERSION = "CEO-BOT-V10-RESILIENT"
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

EXECUTOR = ThreadPoolExecutor(max_workers=3)
LOCK = threading.Lock()
SNAPSHOT = {}
SNAPSHOT_TIME = 0
REFRESHING = set()
TTL_MARKETS = 30 * 60
TTL_FLIGHTS = 4 * 60 * 60

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
You are a high-accuracy market and travel data collector. Return ONLY valid JSON, no markdown.
Never invent a number. A solid primary source is enough to display a value; a second source is only a cross-check.
Iran FX must be FREE-MARKET TOMAN, not official rate. Units must be explicit.
For flights, accept only a visible round-trip economy total in CAD for one adult from Montreal YUL with exact outbound and return dates for the same itinerary. Reject one-way, teaser/from, monthly or stale prices.
"""


def now_label():
    return datetime.now().strftime("%d %b %Y • %H:%M")


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


def openai_json(prompt, max_tokens=2200, timeout=110):
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
        timeout=timeout,
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


def market_prompt():
    return f"""
Current time: {datetime.now().isoformat(timespec='minutes')}.
Collect CURRENT values. Return exactly:
{{
  "gold": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}},
  "usd_cad": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}},
  "usd_irr_toman": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}},
  "iran_gold18_toman_g": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}},
  "emami_coin_toman": {{"primary": number|null, "secondary": number|null, "change_pct": number|null, "primary_source": string|null, "secondary_source": string|null}}
}}
Gold is XAU/USD spot per troy ounce. USD/CAD means 1 USD in CAD. Iran values are free-market TOMAN. If only one source is clearly current and credible, keep primary and set secondary null.
"""


def flight_prompt(route):
    if route == "iran":
        dest, horizon = "Tehran IKA", "next 90 days, trip 7-30 nights"
    else:
        dest, horizon = "Vancouver YVR", "next 60 days, trip 3-7 nights"
    return f"""
Current time: {datetime.now().isoformat(timespec='minutes')}.
Find the cheapest CURRENT visible round-trip economy itinerary for 1 adult from Montreal YUL to {dest}, {horizon}.
Return exactly:
{{"price_cad": number|null, "outbound": "YYYY-MM-DD"|null, "return": "YYYY-MM-DD"|null, "airline": string|null, "stops": string|null, "source_url": string|null, "source_name": string|null}}
Only return a fare when total CAD price and both dates are visible for the same itinerary.
"""


def fv(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def sane(name, v):
    v = fv(v)
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


def close_enough(a, b, tol):
    a, b = fv(a), fv(b)
    return bool(a and b and abs(a - b) / ((a + b) / 2) <= tol)


def choose_metric(name, obj, old, tol):
    p, s = fv((obj or {}).get("primary")), fv((obj or {}).get("secondary"))
    oldv = fv((old or {}).get(name))
    if not sane(name, p):
        return oldv, "old" if oldv is not None else "missing"
    status = "primary"
    if sane(name, s):
        if close_enough(p, s, tol):
            p = (p + s) / 2
            status = "verified"
        else:
            status = "warning"
    limits = {"gold_usd_oz":0.05,"usd_cad":0.025,"usd_irr_toman":0.10,"iran_gold18_toman_g":0.10,"emami_coin_toman":0.12}
    if oldv and abs(p-oldv)/oldv > limits[name]:
        return oldv, "old-warning"
    return p, status


def build_markets(old):
    raw = openai_json(market_prompt(), 2500)
    out, status, sources = {}, {}, []
    specs = [
        ("gold_usd_oz","gold",0.015,"gold_change_pct"),
        ("usd_cad","usd_cad",0.007,"usd_cad_change_pct"),
        ("usd_irr_toman","usd_irr_toman",0.04,"usd_irr_change_pct"),
        ("iran_gold18_toman_g","iran_gold18_toman_g",0.04,"iran_gold18_change_pct"),
        ("emami_coin_toman","emami_coin_toman",0.05,"emami_coin_change_pct"),
    ]
    for key, rawkey, tol, chkey in specs:
        obj = raw.get(rawkey) or {}
        out[key], status[key] = choose_metric(key, obj, old, tol)
        out[chkey] = fv(obj.get("change_pct"))
        for sk in ("primary_source","secondary_source"):
            src = obj.get(sk)
            if src and src not in sources:
                sources.append(src)
    if out.get("usd_cad"):
        out["cad_usd"] = 1/out["usd_cad"]
    else:
        out["cad_usd"] = fv(old.get("cad_usd")) if old else None
    if out.get("usd_irr_toman") and out.get("usd_cad"):
        out["cad_irr_toman"] = out["usd_irr_toman"]/out["usd_cad"]
    else:
        out["cad_irr_toman"] = fv(old.get("cad_irr_toman")) if old else None
    out["status"] = status
    out["sources"] = sources[:8]
    out["markets_at"] = now_label()
    return out


def build_flight(route, old_flight):
    raw = openai_json(flight_prompt(route), 1500, timeout=90)
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
    global SNAPSHOT, SNAPSHOT_TIME
    with LOCK:
        SNAPSHOT.update(part)
        SNAPSHOT_TIME = time.time()
        SNAPSHOT["updated_at"] = now_label()


def telegram_send_message(text, chat_id=TELEGRAM_CHAT_ID, menu=True):
    data = {"chat_id":chat_id,"text":text}
    if menu:
        data["reply_markup"] = json.dumps(MAIN_KEYBOARD, ensure_ascii=False)
    r = requests.post(f"{TELEGRAM_API}/sendMessage", data=data, timeout=25)
    r.raise_for_status()


def telegram_send_photo(image_bytes, caption="", chat_id=TELEGRAM_CHAT_ID, inline_keyboard=None):
    data = {"chat_id":chat_id,"caption":caption}
    if inline_keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard":inline_keyboard}, ensure_ascii=False)
    files = {"photo":("dashboard.png",image_bytes,"image/png")}
    r = requests.post(f"{TELEGRAM_API}/sendPhoto", data=data, files=files, timeout=35)
    r.raise_for_status()


def get_updates(offset=None):
    params = {"timeout":25}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
    r.raise_for_status()
    return r.json()


def font(size,bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG,size)


def rtl(text):
    try:
        return get_display(arabic_reshaper.reshape(str(text)))
    except Exception:
        return str(text)


def fmt(v,d=0):
    if v is None:
        return "—"
    try:
        n=float(v); return f"{n:,.{d}f}" if d else f"{n:,.0f}"
    except Exception:
        return "—"


def pct(v):
    if v is None: return ""
    try:
        v=float(v); return f"{'▲' if v>0 else '▼' if v<0 else '→'} {v:+.2f}%"
    except Exception:
        return ""


def cchange(v):
    try:
        return (34,214,110) if float(v)>0 else (255,72,72) if float(v)<0 else (160,170,180)
    except Exception:
        return (160,170,180)


def rounded(d,box,outline=(45,65,78)):
    d.rounded_rectangle(box,radius=22,fill=(10,19,27),outline=outline,width=2)


def badge(st):
    return "✓" if st=="verified" else "!" if "warning" in str(st) else "↺" if st=="old" else "•"


def metric(d,box,title,value,sub="",change=None,accent=(46,204,113),b=""):
    rounded(d,box,accent); x1,y1,x2,y2=box
    d.text((x1+18,y1+16),rtl(title),font=font(25,True),fill=(236,241,245))
    d.text((x1+18,y1+62),value,font=font(39,True),fill=(245,248,250))
    if sub: d.text((x1+18,y1+112),rtl(sub),font=font(19),fill=(165,178,190))
    if change is not None: d.text((x1+18,y2-38),pct(change),font=font(20,True),fill=cchange(change))
    if b: d.text((x2-50,y1+16),b,font=font(18,True),fill=(180,190,200))


def flight_card(d,box,title,fl,accent,st):
    rounded(d,box,accent); x1,y1,x2,y2=box
    d.text((x1+20,y1+18),rtl(title),font=font(25,True),fill=(238,243,247))
    p=(fl or {}).get("price_cad")
    d.text((x1+20,y1+66),f"C${fmt(p)}" if p else "—",font=font(43,True),fill=accent)
    d.text((x1+20,y1+126),f"{(fl or {}).get('outbound') or '—'}  →  {(fl or {}).get('return') or '—'}",font=font(20),fill=(220,226,231))
    d.text((x1+20,y1+162),f"{(fl or {}).get('airline') or '—'}  •  {(fl or {}).get('stops') or '—'}",font=font(18),fill=(164,178,190))
    d.text((x2-50,y1+18),badge(st),font=font(18,True),fill=(180,190,200))


def render_dashboard(s):
    W,H=1080,1500
    img=Image.new("RGB",(W,H),(4,10,16)); d=ImageDraw.Draw(img)
    d.text((48,34),"METRA",font=font(44,True),fill=(242,245,247))
    d.text((48,82),"RESILIENT DASHBOARD",font=font(22,True),fill=(46,204,113))
    d.text((505,48),s.get("updated_at") or now_label(),font=font(22),fill=(190,201,210))
    rounded(d,(860,34,1035,88),(46,204,113)); d.text((887,50),"● LIVE",font=font(20,True),fill=(46,204,113))
    st=s.get("status") or {}
    metric(d,(40,145,360,420),"طلای جهانی",f"${fmt(s.get('gold_usd_oz'))}","هر اونس",s.get("gold_change_pct"),(232,170,32),badge(st.get("gold_usd_oz")))
    metric(d,(380,145,700,420),"USD / CAD",fmt(s.get("usd_cad"),4),"",s.get("usd_cad_change_pct"),(34,214,110),badge(st.get("usd_cad")))
    metric(d,(720,145,1040,420),"CAD / USD",fmt(s.get("cad_usd"),4),"محاسبه‌ای",None,(54,130,220),"=")
    metric(d,(40,445,280,700),"USD / IRR",fmt(s.get("usd_irr_toman")),"تومان",s.get("usd_irr_change_pct"),(54,130,220),badge(st.get("usd_irr_toman")))
    metric(d,(300,445,540,700),"CAD / IRR",fmt(s.get("cad_irr_toman")),"تومان • محاسبه‌ای",None,(54,130,220),"=")
    metric(d,(560,445,800,700),"طلای 18 عیار",fmt(s.get("iran_gold18_toman_g")),"تومان / گرم",s.get("iran_gold18_change_pct"),(34,214,110),badge(st.get("iran_gold18_toman_g")))
    metric(d,(820,445,1040,700),"سکه امامی",fmt(s.get("emami_coin_toman")),"تومان",s.get("emami_coin_change_pct"),(34,214,110),badge(st.get("emami_coin_toman")))
    d.text((46,740),rtl("ارزان‌ترین بلیت رفت و برگشت"),font=font(30,True),fill=(242,245,247))
    flight_card(d,(40,800,520,1085),"مونترال ⇄ تهران",s.get("iran_flight") or {},(56,139,253),s.get("iran_flight_status"))
    flight_card(d,(560,800,1040,1085),"مونترال ⇄ ونکوور",s.get("vancouver_flight") or {},(34,214,110),s.get("vancouver_flight_status"))
    rounded(d,(40,1120,1040,1310)); d.text((64,1143),rtl("وضعیت داده"),font=font(28,True),fill=(242,245,247))
    d.text((64,1200),rtl("منبع اصلی معتبر نمایش داده می‌شود؛ بخش‌های دیگر مستقل به‌روزرسانی می‌شوند."),font=font(21),fill=(34,214,110))
    src=", ".join((s.get("sources") or [])[:5])
    if src: d.text((48,1360),f"Sources: {src}",font=font(16),fill=(125,138,150))
    d.text((48,1400),rtl("• منبع اصلی  ✓ تطبیق‌شده  ! اختلاف منبع  ↺ داده معتبر قبلی"),font=font(17),fill=(125,138,150))
    b=io.BytesIO(); img.save(b,format="PNG",optimize=True); return b.getvalue()


def inline_buttons(s):
    rows=[]
    for text,key in (("✈️ بررسی تهران","iran_flight"),("✈️ بررسی ونکوور","vancouver_flight")):
        url=(s.get(key) or {}).get("source_url")
        if url: rows.append([{"text":text,"url":url}])
    return rows


def show_dashboard(chat_id):
    with LOCK: s=dict(SNAPSHOT)
    if not s:
        telegram_send_message("⏳ داده بازار در حال آماده‌شدن است.",chat_id,True)
        start_refresh("markets",chat_id)
        return
    telegram_send_photo(render_dashboard(s),"📊 آخرین داده معتبر",chat_id,inline_buttons(s))


def refresh_markets(notify=None):
    try:
        with LOCK: old=dict(SNAPSHOT)
        part=build_markets(old); merge_snapshot(part)
        if notify: show_dashboard(notify)
    except Exception as e:
        print(f"[{VERSION}] markets error: {type(e).__name__}: {e}",flush=True)
        if notify:
            with LOCK: has=bool(SNAPSHOT)
            telegram_send_message("⚠️ بازار فعلاً به‌روزرسانی نشد؛ داده قبلی حفظ شد." if has else "⚠️ دریافت بازار موقتاً ناموفق بود؛ دوباره خودکار تلاش می‌کنم.",notify,True)
    finally:
        with LOCK: REFRESHING.discard("markets")


def refresh_flight(route,notify=None):
    key=f"flight_{route}"
    try:
        with LOCK:
            old=dict(SNAPSHOT); oldfl=old.get("iran_flight" if route=="iran" else "vancouver_flight")
        fl,st=build_flight(route,oldfl)
        part={("iran_flight" if route=="iran" else "vancouver_flight"):fl,("iran_flight_status" if route=="iran" else "vancouver_flight_status"):st,"flights_at":now_label()}
        merge_snapshot(part)
        if notify: show_dashboard(notify)
    except Exception as e:
        print(f"[{VERSION}] {route} flight error: {type(e).__name__}: {e}",flush=True)
    finally:
        with LOCK: REFRESHING.discard(key)


def start_refresh(kind,notify=None):
    with LOCK:
        if kind in REFRESHING: return False
        REFRESHING.add(kind)
    if kind=="markets": EXECUTOR.submit(refresh_markets,notify)
    elif kind=="iran": EXECUTOR.submit(refresh_flight,"iran",notify)
    elif kind=="vancouver": EXECUTOR.submit(refresh_flight,"vancouver",notify)
    return True


def refresh_all(notify=None):
    a=start_refresh("markets",notify)
    start_refresh("iran")
    start_refresh("vancouver")
    return a


def handle_message(m):
    chat_id=str(m.get("chat",{}).get("id",""))
    if not chat_id or chat_id!=TELEGRAM_CHAT_ID: return
    raw=(m.get("text") or "").strip(); low=raw.lower()
    if low in {"/start","/help","hello","hi"}:
        telegram_send_message("📊 Metra Dashboard V10\nهر بخش مستقل به‌روزرسانی می‌شود؛ خرابی یک منبع کل داشبورد را متوقف نمی‌کند.",chat_id,True)
    elif raw in {"📊 داشبورد","💰 ارز و طلا","✈️ بلیط‌ها"} or low=="/dashboard":
        show_dashboard(chat_id)
    elif raw=="🔄 بررسی مجدد" or low=="/refresh":
        refresh_all(chat_id); telegram_send_message("🔄 بروزرسانی شروع شد؛ بازار و بلیت‌ها مستقل بررسی می‌شوند.",chat_id,True)
    elif raw=="🚨 هشدارها":
        with LOCK: s=dict(SNAPSHOT)
        warns=[k for k,v in (s.get("status") or {}).items() if "warning" in str(v)]
        telegram_send_message("⚠️ اختلاف منبع: "+", ".join(warns) if warns else "✅ هشدار اعتبار مهمی وجود ندارد.",chat_id,True)
    else:
        telegram_send_message("یکی از دکمه‌ها را انتخاب کن.",chat_id,True)


def scheduler_loop():
    last_flights=0
    while True:
        try:
            with LOCK: age=time.time()-SNAPSHOT_TIME if SNAPSHOT_TIME else 10**9
            if age>TTL_MARKETS: start_refresh("markets")
            if time.time()-last_flights>TTL_FLIGHTS:
                start_refresh("iran"); start_refresh("vancouver"); last_flights=time.time()
        except Exception as e: print(f"[{VERSION}] scheduler: {e}",flush=True)
        time.sleep(60)


def polling_loop():
    offset=None; print(f"[{VERSION}] polling started",flush=True)
    while True:
        try:
            data=get_updates(offset)
            for u in data.get("result",[]):
                offset=u["update_id"]+1
                if u.get("message"): handle_message(u["message"])
        except Exception as e:
            print(f"[{VERSION}] telegram: {type(e).__name__}: {e}",flush=True); time.sleep(2)


def startup():
    print(f"[{VERSION}] START",flush=True)
    threading.Thread(target=scheduler_loop,daemon=True).start()
    start_refresh("markets")
    try: telegram_send_message("✅ V10 فعال شد — بازار سریع‌تر می‌آید و بلیت‌ها جداگانه تکمیل می‌شوند.",TELEGRAM_CHAT_ID,True)
    except Exception as e: print(f"startup telegram: {e}",flush=True)
    polling_loop()


if __name__=="__main__": startup()
