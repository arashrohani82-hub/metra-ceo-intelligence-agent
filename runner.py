from main_v12 import app as bot
from direct_auctions import search_lab_auctions
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

bot.VERSION = "CEO-BOT-V17-DIRECT-LAB-AUCTIONS"

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


def fmt_money(v):
    try:
        return f"${float(v):,.0f} CAD"
    except Exception:
        return "نامشخص"


def auction_results_text(data):
    items = (data or {}).get("items") or []
    errors = (data or {}).get("errors") or []
    if not items:
        msg = (
            "🧪 مزایده تجهیزات آزمایشگاه\n\n"
            "فعلاً مورد فعال و مرتبطِ قابل‌اعتماد برای تجهیزات خاک، بتن یا آسفالت پیدا نشد.\n"
            "جست‌وجو مستقیم از وب انجام شد و به OpenAI وابسته نیست."
        )
        if errors:
            msg += "\n\n⚠️ بعضی منابع موقتاً پاسخ ندادند: " + ", ".join(errors[:4])
        return msg

    labels = {"soil": "خاک", "concrete": "بتن", "asphalt": "آسفالت", "general": "عمومی"}
    lines = ["🧪 مزایده تجهیزات آزمایشگاه", "🌐 Direct web search — بدون وابستگی به OpenAI", ""]
    for i, item in enumerate(items[:8], 1):
        category = labels.get(str(item.get("category") or "").lower(), "عمومی")
        lines.append(f"{i}) {item.get('title') or 'بدون عنوان'}")
        lines.append(f"   🧭 {item.get('location') or 'مکان نامشخص'} | 🧱 {category} | ⭐ {item.get('relevance_score') or '-'} / 100")
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

    lines.append("اولویت: Québec → Ontario → بقیه کانادا")
    if errors:
        lines.append("⚠️ برخی منابع موقتاً پاسخ ندادند: " + ", ".join(errors[:4]))
    return "\n".join(lines)


def send_long(text, chat_id):
    if len(text) <= 3900:
        bot.telegram_send_message(text, chat_id, True)
        return
    current = ""
    for block in text.split("\n\n"):
        candidate = (current + "\n\n" + block).strip()
        if len(candidate) > 3800 and current:
            bot.telegram_send_message(current, chat_id, True)
            current = block
        else:
            current = candidate
    if current:
        bot.telegram_send_message(current, chat_id, True)


def run_auction_search(chat_id):
    try:
        data = search_lab_auctions()
        send_long(auction_results_text(data), chat_id)
    except Exception as exc:
        print(f"[{bot.VERSION}] direct auction search: {type(exc).__name__}: {exc}", flush=True)
        bot.telegram_send_message(
            "⚠️ جست‌وجوی مستقیم مزایده فعلاً کامل نشد. چند دقیقه دیگر دوباره امتحان کن.",
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


def handle_message(m):
    chat_id = str(m.get("chat", {}).get("id", ""))
    if not chat_id or chat_id != bot.TELEGRAM_CHAT_ID:
        return
    raw = (m.get("text") or "").strip()
    low = raw.lower()

    if low in {"/start", "/help", "hello", "hi"}:
        bot.telegram_send_message(
            "📊 Metra CEO Intelligence\nارز و طلا مستقیم از منابع داده دریافت می‌شوند.\n🧪 مزایده‌ها نیز مستقیماً از وب جست‌وجو می‌شوند و به OpenAI وابسته نیستند.",
            chat_id,
            True,
        )
    elif raw in {"📊 داشبورد", "💰 ارز و طلا"} or low == "/dashboard":
        bot.show_dashboard(chat_id)
    elif raw == "🧪 مزایده تجهیزات آزمایشگاه" or low in {"/auction", "/auctions"}:
        if start_auction_search(chat_id):
            bot.telegram_send_message(
                "🔎 در حال جست‌وجوی مستقیم مزایده‌های تجهیزات Soil / Concrete / Asphalt در کانادا…",
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


bot.handle_message = handle_message


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
            self._send_json(200, {
                "status": "ok",
                "service": "metra-ceo-intelligence-agent",
                "version": bot.VERSION,
                "lab_auction_tab": True,
                "auction_mode": "direct-web-no-openai",
            })
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
