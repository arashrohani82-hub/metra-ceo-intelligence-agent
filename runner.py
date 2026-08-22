from main_v12 import app as bot

bot.VERSION = "CEO-BOT-V15-MARKETS-ONLY"

# Remove flight controls and all flight/OpenAI activity. Keep the stable market dashboard.
bot.MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 داشبورد"}],
        [{"text": "💰 ارز و طلا"}],
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
    # Keep header + market cards only and add a clean footer.
    img = img.crop((0, 0, 1080, 790))
    d = ImageDraw.Draw(img)
    d.text((48, 742), bot.rtl("بازار: به‌روزرسانی مستقیم هر 15 دقیقه"), font=bot.font(18), fill=(125, 138, 150))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


bot.render_dashboard = render_market_only


def market_only_handle_message(m):
    chat_id = str(m.get("chat", {}).get("id", ""))
    if not chat_id or chat_id != bot.TELEGRAM_CHAT_ID:
        return
    raw = (m.get("text") or "").strip()
    low = raw.lower()

    if low in {"/start", "/help", "hello", "hi"}:
        bot.telegram_send_message("📊 Metra Market Dashboard\nارز و طلا مستقیم از منابع داده دریافت می‌شوند.", chat_id, True)
    elif raw in {"📊 داشبورد", "💰 ارز و طلا"} or low == "/dashboard":
        bot.show_dashboard(chat_id)
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

if __name__ == "__main__":
    bot.startup()
