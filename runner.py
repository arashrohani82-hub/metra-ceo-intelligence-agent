from main_v12 import app as bot
from direct_auctions import search_lab_auctions
from direct_lab_rentals import search_lab_rentals
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

bot.VERSION = "CEO-BOT-V20-LAB-SETUP"

MAIN_MENU = {
    "keyboard": [
        [{"text": "📊 داشبورد"}],
        [{"text": "💰 ارز و طلا"}],
        [{"text": "🧪 خرید آزمایشگاه و تجهیزات"}],
        [{"text": "🏭 اجاره فضای آزمایشگاه"}],
        [{"text": "🚨 هشدارها"}, {"text": "🔄 بررسی مجدد"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "یک گزینه را انتخاب کن",
}

AUCTION_MENU = {
    "keyboard": [
        [{"text": "📍 فقط Québec"}, {"text": "📍 Québec + Ontario"}],
        [{"text": "🇨🇦 کل کانادا"}],
        [{"text": "🏭 تعطیلی و Liquidation آزمایشگاه"}],
        [{"text": "🏛 Government / University Surplus"}],
        [{"text": "🔙 بازگشت"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "نوع جست‌وجو را انتخاب کن",
}

RENTAL_MENU = {
    "keyboard": [
        [{"text": "🏙 Montréal"}, {"text": "🏢 Laval"}],
        [{"text": "🌉 Longueuil / Rive-Sud"}],
        [{"text": "✈️ West Island"}],
        [{"text": "📍 Grand Montréal"}],
        [{"text": "🔙 بازگشت"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "منطقه اجاره را انتخاب کن",
}

bot.MAIN_KEYBOARD = MAIN_MENU


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


def set_menu(menu):
    bot.MAIN_KEYBOARD = menu


def fmt_money(v):
    try:
        return f"${float(v):,.0f} CAD"
    except Exception:
        return "نامشخص"


def diagnostics_text(data):
    diagnostics = (data or {}).get("diagnostics") or {}
    if not diagnostics:
        return ""
    lines = ["", "📡 گزارش منابع بررسی‌شده:"]
    for name, st in diagnostics.items():
        hits = int(st.get("search_hits") or 0)
        inspected = int(st.get("inspected") or 0)
        accepted = int(st.get("accepted") or 0)
        closed = int(st.get("closed") or 0)
        low = int(st.get("low_relevance") or 0)
        filtered = int(st.get("filtered_by_mode") or 0)
        lines.append(f"• {name}: {hits} نتیجه | {inspected} بررسی | {accepted} نمایش")
        if closed or low or filtered:
            lines.append(f"  حذف: {closed} بسته + {low} ضعیف + {filtered} خارج از فیلتر")
        errs = st.get("search_errors") or st.get("errors") or []
        if errs:
            lines.append("  ⚠️ " + ", ".join(errs[:2]))
    return "\n".join(lines)


def auction_results_text(data):
    items = (data or {}).get("items") or []
    errors = (data or {}).get("errors") or []
    checked_at = (data or {}).get("checked_at") or ""
    mode = (data or {}).get("mode") or "canada"
    mode_label = {
        "quebec": "فقط Québec",
        "qcon": "Québec + Ontario",
        "canada": "کل کانادا",
        "closures": "تعطیلی / Liquidation آزمایشگاه",
        "govuni": "Government / University Surplus",
    }.get(mode, mode)

    if not items:
        msg = (
            "🧪 خرید آزمایشگاه و تجهیزات\n\n"
            f"فیلتر: {mode_label}\n"
            "فعلاً Listing مناسبی پیدا نشد، اما Lotهای عمومی آزمایشگاه، تجهیزات مهندسی، Materials Testing و Liquidation هم بررسی شدند."
        )
        msg += diagnostics_text(data)
        if errors:
            msg += "\n\n⚠️ خطاهای منبع: " + ", ".join(errors[:5])
        if checked_at:
            msg += f"\n\n🕒 بررسی: {checked_at}"
        return msg

    labels = {"soil": "خاک", "concrete": "بتن", "asphalt": "آسفالت", "general": "آزمایشگاهی/مهندسی"}
    lines = ["🧪 خرید آزمایشگاه و تجهیزات", f"🎯 فیلتر: {mode_label}", "🌐 Direct web search — بدون OpenAI", ""]
    for i, item in enumerate(items[:15], 1):
        category = labels.get(str(item.get("category") or "").lower(), "آزمایشگاهی/مهندسی")
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
            engine = item.get("search_engine")
            src = item.get("source_name") + (f" / {engine}" if engine else "")
            lines.append(f"   🔎 {src}")
        if item.get("image_url"):
            lines.append(f"   🖼 عکس: {item.get('image_url')}")
        if item.get("source_url"):
            lines.append(f"   🔗 لینک Listing: {item.get('source_url')}")
        lines.append("")

    lines.append(diagnostics_text(data))
    if errors:
        lines.append("⚠️ برخی جست‌وجوها پاسخ کامل ندادند: " + ", ".join(errors[:5]))
    if checked_at:
        lines.append(f"🕒 بررسی: {checked_at}")
    return "\n".join(x for x in lines if x is not None)


def rental_results_text(data):
    items = (data or {}).get("items") or []
    checked_at = (data or {}).get("checked_at") or ""
    region = (data or {}).get("region") or "grandmontreal"
    labels = {
        "montreal": "Montréal",
        "laval": "Laval",
        "southshore": "Longueuil / Rive-Sud",
        "westisland": "West Island",
        "grandmontreal": "Grand Montréal",
    }
    title = labels.get(region, region)

    if not items:
        msg = (
            f"🏭 اجاره فضای آزمایشگاه — {title}\n\n"
            "فعلاً فضای صنعتی مناسب و قابل‌اعتماد پیدا نشد. ربات دنبال فضای صنعتی/فلکس با امکان garage/loading، برق مناسب، آب/درین و شرایط قابل‌بررسی برای آزمایشگاه خاک، بتن و آسفالت می‌گردد."
        )
        msg += diagnostics_text(data)
        if checked_at:
            msg += f"\n\n🕒 بررسی: {checked_at}"
        return msg

    lines = [f"🏭 اجاره فضای آزمایشگاه — {title}", "🌐 Direct web search", ""]
    for i, item in enumerate(items[:12], 1):
        lines.append(f"{i}) {item.get('title') or 'بدون عنوان'}")
        if item.get("address"):
            lines.append(f"   📍 {item.get('address')}")
        details = []
        if item.get("area_sf"):
            details.append(f"{item.get('area_sf'):,} ft²")
        if item.get("price_per_sf") is not None:
            details.append(f"${item.get('price_per_sf'):,.2f}/ft²")
        if item.get("monthly_rent") is not None:
            details.append(f"${item.get('monthly_rent'):,.0f}/mois")
        details.append(f"⭐ {item.get('suitability_score') or '-'} / 100")
        lines.append("   📐 " + " | ".join(details))
        features = item.get("features") or []
        if features:
            lines.append("   ✅ " + ", ".join(features))
        lines.append("   💡 امتیاز بر اساس تناسب اولیه برای آزمایشگاه مصالح است؛ zoning، ظرفیت کف، برق و drain باید قبل از اجاره تأیید شوند.")
        if item.get("source_name"):
            engine = item.get("search_engine")
            lines.append(f"   🔎 {item.get('source_name')}{(' / ' + engine) if engine else ''}")
        if item.get("source_url"):
            lines.append(f"   🔗 {item.get('source_url')}")
        lines.append("")

    lines.append(diagnostics_text(data))
    if checked_at:
        lines.append(f"🕒 بررسی: {checked_at}")
    return "\n".join(x for x in lines if x is not None)


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


def run_auction_search(chat_id, mode):
    try:
        data = search_lab_auctions(mode=mode)
        send_long(auction_results_text(data), chat_id)
    except Exception as exc:
        print(f"[{bot.VERSION}] direct auction search: {type(exc).__name__}: {exc}", flush=True)
        bot.telegram_send_message("⚠️ جست‌وجوی مستقیم مزایده فعلاً کامل نشد. چند دقیقه دیگر دوباره امتحان کن.", chat_id, True)
    finally:
        with bot.LOCK:
            bot.REFRESHING.discard("lab_auctions")


def start_auction_search(chat_id, mode):
    with bot.LOCK:
        if "lab_auctions" in bot.REFRESHING:
            return False
        bot.REFRESHING.add("lab_auctions")
    bot.EXECUTOR.submit(run_auction_search, chat_id, mode)
    return True


def run_rental_search(chat_id, region):
    try:
        data = search_lab_rentals(region=region)
        send_long(rental_results_text(data), chat_id)
    except Exception as exc:
        print(f"[{bot.VERSION}] rental search: {type(exc).__name__}: {exc}", flush=True)
        bot.telegram_send_message("⚠️ جست‌وجوی فضای اجاره‌ای فعلاً کامل نشد. چند دقیقه دیگر دوباره امتحان کن.", chat_id, True)
    finally:
        with bot.LOCK:
            bot.REFRESHING.discard("lab_rentals")


def start_rental_search(chat_id, region):
    with bot.LOCK:
        if "lab_rentals" in bot.REFRESHING:
            return False
        bot.REFRESHING.add("lab_rentals")
    bot.EXECUTOR.submit(run_rental_search, chat_id, region)
    return True


def launch_mode(chat_id, mode, label):
    if start_auction_search(chat_id, mode):
        bot.telegram_send_message(
            f"🔎 در حال جست‌وجو: {label}\nLotهای آزمایشگاه، تجهیزات مهندسی، Soil / Concrete / Asphalt و Materials Testing بررسی می‌شوند…",
            chat_id,
            True,
        )
    else:
        bot.telegram_send_message("⏳ جست‌وجوی مزایده از قبل در حال انجام است.", chat_id, True)


def launch_rental(chat_id, region, label):
    if start_rental_search(chat_id, region):
        bot.telegram_send_message(
            f"🔎 در حال جست‌وجوی فضای مناسب آزمایشگاه در {label}…\nفضاهای industrial/flex با garage/loading، برق مناسب، آب/drain و امکان استفاده آزمایشگاهی بررسی می‌شوند.",
            chat_id,
            True,
        )
    else:
        bot.telegram_send_message("⏳ جست‌وجوی اجاره از قبل در حال انجام است.", chat_id, True)


def handle_message(m):
    chat_id = str(m.get("chat", {}).get("id", ""))
    if not chat_id or chat_id != bot.TELEGRAM_CHAT_ID:
        return
    raw = (m.get("text") or "").strip()
    low = raw.lower()

    if low in {"/start", "/help", "hello", "hi"}:
        set_menu(MAIN_MENU)
        bot.telegram_send_message(
            "📊 Metra CEO Intelligence\n🧪 خرید تجهیزات و مزایده‌ها\n🏭 اجاره فضای مناسب آزمایشگاه مصالح",
            chat_id,
            True,
        )
    elif raw in {"📊 داشبورد", "💰 ارز و طلا"} or low == "/dashboard":
        set_menu(MAIN_MENU)
        bot.show_dashboard(chat_id)
    elif raw == "🧪 خرید آزمایشگاه و تجهیزات" or low in {"/auction", "/auctions"}:
        set_menu(AUCTION_MENU)
        bot.telegram_send_message("🧪 چه نوع فرصتی را جست‌وجو کنم؟", chat_id, True)
    elif raw == "🏭 اجاره فضای آزمایشگاه" or low in {"/rentlab", "/labspace"}:
        set_menu(RENTAL_MENU)
        bot.telegram_send_message(
            "🏭 کدام منطقه را برای فضای آزمایشگاه بررسی کنم؟\nفضاهای صنعتی مناسب Soil / Concrete / Asphalt Lab اولویت دارند.",
            chat_id,
            True,
        )
    elif raw == "📍 فقط Québec":
        set_menu(AUCTION_MENU)
        launch_mode(chat_id, "quebec", "فقط Québec")
    elif raw == "📍 Québec + Ontario":
        set_menu(AUCTION_MENU)
        launch_mode(chat_id, "qcon", "Québec + Ontario")
    elif raw == "🇨🇦 کل کانادا":
        set_menu(AUCTION_MENU)
        launch_mode(chat_id, "canada", "کل کانادا")
    elif raw == "🏭 تعطیلی و Liquidation آزمایشگاه":
        set_menu(AUCTION_MENU)
        launch_mode(chat_id, "closures", "تعطیلی / Liquidation آزمایشگاه")
    elif raw == "🏛 Government / University Surplus":
        set_menu(AUCTION_MENU)
        launch_mode(chat_id, "govuni", "Government / University Surplus")
    elif raw == "🏙 Montréal":
        set_menu(RENTAL_MENU)
        launch_rental(chat_id, "montreal", "Montréal")
    elif raw == "🏢 Laval":
        set_menu(RENTAL_MENU)
        launch_rental(chat_id, "laval", "Laval")
    elif raw == "🌉 Longueuil / Rive-Sud":
        set_menu(RENTAL_MENU)
        launch_rental(chat_id, "southshore", "Longueuil / Rive-Sud")
    elif raw == "✈️ West Island":
        set_menu(RENTAL_MENU)
        launch_rental(chat_id, "westisland", "West Island")
    elif raw == "📍 Grand Montréal":
        set_menu(RENTAL_MENU)
        launch_rental(chat_id, "grandmontreal", "Grand Montréal")
    elif raw == "🔙 بازگشت":
        set_menu(MAIN_MENU)
        bot.telegram_send_message("به منوی اصلی برگشتی.", chat_id, True)
    elif raw == "🔄 بررسی مجدد" or low == "/refresh":
        set_menu(MAIN_MENU)
        started = bot.start_market_refresh(chat_id, force=True)
        bot.telegram_send_message("🔄 بازار در حال به‌روزرسانی است." if started else "⏳ به‌روزرسانی بازار از قبل در حال انجام است.", chat_id, True)
    elif raw == "🚨 هشدارها":
        set_menu(MAIN_MENU)
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
                "lab_acquisition_tab": True,
                "lab_rental_tab": True,
                "auction_mode": "direct-web-broad-lab-acquisition",
                "rental_mode": "direct-web-lab-rental",
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
