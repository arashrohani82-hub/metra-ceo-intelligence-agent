from main_v12 import app as bot
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

bot.VERSION = "CEO-BOT-V16-LAB-AUCTIONS"

# Keep the stable market dashboard and add a dedicated laboratory-equipment
# auction intelligence tab. Flight/OpenAI background activity stays disabled.
bot.MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 داشبورد"}],
        [{"text": "💰 ارز و طلا"}],
        [{"text": "🧪 مزایده تجهیزات آزمایشگاه"}],
        [{"text": "🚨 هشدارها"}, {"text": "🔄 بررسی مجدد"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "یک گزینه را انتخاب کن",
}


def no_flight_refresh(notify=None, force=False):
    return False


bot.start_flight_refresh = no_flight_refresh

# Reuse the existing dashboard renderer but crop away the flight section.
_base_render = bot.render_dashboard


def render_market_only(s):
    from PIL import Image, ImageDraw
    import io

    raw = _base_render(s)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img = img.crop((0, 0, 1080, 790))
    d = ImageDraw.Draw(img)
    d.text((48, 742), bot.rtl("بازار: به‌روزرسانی مستقیم هر 15 دقیقه"), font=bot.font(18), fill=(125, 138, 150))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


bot.render_dashboard = render_market_only


# ---------------- Lab equipment auction intelligence ----------------

def auction_prompt():
    return f"""
Current date/time: {bot.datetime.now().isoformat(timespec='minutes')}.
Search the live web for ACTIVE auctions, surplus sales, liquidations, lab closures,
or government/university surplus listings in Canada that can be useful for a
construction materials testing laboratory.

Geographic priority:
1. Quebec, especially Montreal / Laval / Longueuil / Quebec City
2. Ontario, especially Ottawa / Toronto
3. Elsewhere in Canada only when the opportunity is unusually strong

Target equipment:
- SOIL / GEOTECHNICAL: sieve shaker, sieves, drying oven, precision balance,
  Atterberg limits, Proctor compaction, CBR, permeability, direct shear,
  consolidation, triaxial, soil density / moisture equipment
- CONCRETE: compression testing machine, concrete cylinder testing press,
  curing tank/chamber, concrete saw, core drill, slump cone, air meter,
  unit weight equipment, concrete mixer, molds
- ASPHALT / AGGREGATE: Marshall stability, gyratory compactor, ignition oven,
  asphalt extraction, density equipment, aggregate testing equipment
- Complete materials-testing laboratories or laboratory liquidation lots

Important sources to check when available include GCSurplus, GovDeals, HiBid,
Ritchie Bros, university/government surplus, industrial auctioneers and lab
liquidation sales.

Return ONLY valid JSON in this exact shape:
{{
  "items": [
    {{
      "title": "string",
      "category": "soil|concrete|asphalt|general",
      "location": "string|null",
      "closing_date": "string|null",
      "current_bid_cad": number|null,
      "buyer_premium": "string|null",
      "source_name": "string",
      "source_url": "https://...",
      "relevance_score": integer,
      "why_it_matters": "short string"
    }}
  ],
  "checked_at": "string"
}}

Rules:
- Include at most 8 items, ranked best first.
- Include only listings that appear active/current. If status is unclear, exclude it.
- Never invent a bid, closing date, location, or URL. Use null when unavailable.
- relevance_score is 1-100 based on usefulness for building a soil/concrete/asphalt lab.
- Prefer actual testing equipment over generic construction machinery.
- If there are no strong active listings, return an empty items list.
"""


def fetch_lab_auctions():
    return bot.openai_json(auction_prompt(), max_tokens=2200, timeout=120)


def fmt_money(v):
    try:
        return f"${float(v):,.0f} CAD"
    except Exception:
        return "نامشخص"


def auction_results_text(data):
    items = (data or {}).get("items") or []
    if not items:
        return (
            "🧪 مزایده تجهیزات آزمایشگاه\n\n"
            "فعلاً مورد فعال و قابل‌اعتمادِ قوی برای تجهیزات خاک، بتن یا آسفالت پیدا نشد.\n"
            "دوباره بعداً این تب را بزن تا جست‌وجوی زنده تکرار شود."
        )

    lines = ["🧪 مزایده تجهیزات آزمایشگاه", ""]
    labels = {"soil": "خاک", "concrete": "بتن", "asphalt": "آسفالت", "general": "عمومی"}
    for i, item in enumerate(items[:8], 1):
        score = item.get("relevance_score")
        category = labels.get(str(item.get("category") or "").lower(), "عمومی")
        lines.append(f"{i}) {item.get('title') or 'بدون عنوان'}")
        lines.append(f"   🧭 {item.get('location') or 'مکان نامشخص'} | 🧱 {category} | ⭐ {score or '-'} / 100")
        if item.get("current_bid_cad") is not None:
            lines.append(f"   💵 Bid فعلی: {fmt_money(item.get('current_bid_cad'))}")
        if item.get("closing_date"):
            lines.append(f"   ⏰ پایان: {item.get('closing_date')}")
        if item.get("buyer_premium"):
            lines.append(f"   🧾 Buyer premium: {item.get('buyer_premium')}")
        if item.get("why_it_matters"):
            lines.append(f"   💡 {item.get('why_it_matters')}")
        if item.get("source_name"):
            lines.append(f"   🔎 {item.get('source_name')}")
        if item.get("source_url"):
            lines.append(f"   🔗 {item.get('source_url')}")
        lines.append("")

    lines.append("اولویت ربات: Québec → Ontario → بقیه کانادا")
    lines.append("فقط آگهی‌هایی نمایش داده می‌شوند که در جست‌وجوی فعلی فعال به نظر برسند.")
    return "\n".join(lines)


def run_auction_search(chat_id):
    try:
        data = fetch_lab_auctions()
        text = auction_results_text(data)
        # Telegram messages have a practical length limit. Split conservatively.
        if len(text) <= 3900:
            bot.telegram_send_message(text, chat_id, True)
        else:
            chunks = []
            current = []
            size = 0
            for block in text.split("\n\n"):
                if size + len(block) + 2 > 3800 and current:
                    chunks.append("\n\n".join(current))
                    current, size = [], 0
                current.append(block)
                size += len(block) + 2
            if current:
                chunks.append("\n\n".join(current))
            for chunk in chunks:
                bot.telegram_send_message(chunk, chat_id, True)
    except Exception as exc:
        print(f"[{bot.VERSION}] auction search: {type(exc).__name__}: {exc}", flush=True)
        bot.telegram_send_message(
            "⚠️ جست‌وجوی مزایده فعلاً کامل نشد. اتصال جست‌وجوی وب یا OpenAI را بررسی کن و دوباره این تب را بزن.",
            chat_id,
            True,
        )
    finally:
        with bot.LOCK:
            bot.REFRESHING.discard("lab_auctions")


def start_auction_search(chat_id):
    with bot.LOCK:
        if "lab_auctions" in bot.REFRESHING:
            return False
        bot.REFRESHING.add("lab_auctions")
    bot.EXECUTOR.submit(run_auction_search, chat_id)
    return True


def market_only_handle_message(m):
    chat_id = str(m.get("chat", {}).get("id", ""))
    if not chat_id or chat_id != bot.TELEGRAM_CHAT_ID:
        return
    raw = (m.get("text") or "").strip()
    low = raw.lower()

    if low in {"/start", "/help", "hello", "hi"}:
        bot.telegram_send_message(
            "📊 Metra CEO Intelligence\nارز و طلا مستقیم از منابع داده دریافت می‌شوند.\n🧪 تب مزایده برای تجهیزات آزمایشگاه خاک، بتن و آسفالت فعال است.",
            chat_id,
            True,
        )
    elif raw in {"📊 داشبورد", "💰 ارز و طلا"} or low == "/dashboard":
        bot.show_dashboard(chat_id)
    elif raw == "🧪 مزایده تجهیزات آزمایشگاه" or low in {"/auction", "/auctions"}:
        if start_auction_search(chat_id):
            bot.telegram_send_message(
                "🔎 در حال جست‌وجوی زنده مزایده‌های تجهیزات Soil / Concrete / Asphalt در کانادا…",
                chat_id,
                True,
            )
        else:
            bot.telegram_send_message("⏳ جست‌وجوی مزایده از قبل در حال انجام است.", chat_id, True)
    elif raw == "🔄 بررسی مجدد" or low == "/refresh":
        started = bot.start_market_refresh(chat_id, force=True)
        bot.telegram_send_message("🔄 بازار در حال به‌روزرسانی است." if started else "⏳ به‌روزرسانی بازار از قبل در حال انجام است.", chat_id, True)
    elif raw == "🚨 هشدارها":
        with bot.LOCK:
            s = dict(bot.SNAPSHOT)
        warnings = [k for k, v in (s.get("status") or {}).items() if "warning" in str(v)]
        errors = s.get("market_errors") or []
        if warnings or errors:
            bot.telegram_send_message("⚠️ کنترل داده: " + ", ".join(warnings + errors), chat_id, True)
        else:
            bot.telegram_send_message("✅ هشدار داده مهمی وجود ندارد.", chat_id, True)
    else:
        bot.telegram_send_message("یکی از دکمه‌های منو را انتخاب کن.", chat_id, True)


bot.handle_message = market_only_handle_message


class HealthHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/status", "/health"):
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "metra-ceo-intelligence-agent",
                    "version": bot.VERSION,
                    "lab_auction_tab": True,
                },
            )
        else:
            self._send_json(404, {"error": "not_found"})

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[{bot.VERSION}] health server listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    bot.startup()
