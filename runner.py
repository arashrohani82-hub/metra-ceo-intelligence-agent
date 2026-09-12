from main_v12 import app as bot
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

bot.VERSION = "CEO-BOT-V22-WEATHER"

MAIN_MENU = {
    "keyboard": [
        [{"text": "📊 داشبورد"}],
        [{"text": "💰 ارز و طلا"}],
        [{"text": "🔄 بررسی مجدد"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "یک گزینه را انتخاب کن",
}

bot.MAIN_KEYBOARD = MAIN_MENU

WEATHER_LAT = float(os.environ.get("WEATHER_LAT", "45.6066"))
WEATHER_LON = float(os.environ.get("WEATHER_LON", "-73.7124"))
WEATHER_CITY = os.environ.get("WEATHER_CITY", "Laval")
WEATHER_COLD_C = float(os.environ.get("WEATHER_COLD_C", "-15"))
WEATHER_DROP_C = float(os.environ.get("WEATHER_DROP_C", "10"))
WEATHER_TTL = 30 * 60
WEATHER_CHECK_INTERVAL = 6 * 60 * 60

_WEATHER_CACHE = {"at": 0.0, "data": None}
_WEATHER_LOCK = threading.Lock()
_ALERTED_KEYS = set()


def no_flight_refresh(notify=None, force=False):
    return False


bot.start_flight_refresh = no_flight_refresh


def _fetch_weather(force=False):
    now = time.time()
    with _WEATHER_LOCK:
        if not force and _WEATHER_CACHE["data"] and now - _WEATHER_CACHE["at"] < WEATHER_TTL:
            return _WEATHER_CACHE["data"]

    params = {
        "latitude": WEATHER_LAT,
        "longitude": WEATHER_LON,
        "timezone": "America/Toronto",
        "forecast_days": 10,
        "current": "temperature_2m",
        "daily": "temperature_2m_max,temperature_2m_min",
    }
    data = bot.safe_get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20).json()
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    days = []
    for i, day in enumerate(dates[:10]):
        if i >= len(highs) or i >= len(lows):
            break
        days.append({"date": day, "high": float(highs[i]), "low": float(lows[i])})

    alerts = []
    for i, day in enumerate(days):
        if day["low"] <= WEATHER_COLD_C:
            alerts.append({
                "type": "cold",
                "date": day["date"],
                "low": day["low"],
                "text": f"دمای خیلی پایین: {day['low']:.0f}°C",
            })
        if i > 0:
            drop = days[i - 1]["low"] - day["low"]
            if drop >= WEATHER_DROP_C:
                alerts.append({
                    "type": "drop",
                    "date": day["date"],
                    "low": day["low"],
                    "drop": drop,
                    "text": f"افت شدید دما: {drop:.0f}°C نسبت به روز قبل",
                })

    current_value = (data.get("current") or {}).get("temperature_2m")
    result = {
        "city": WEATHER_CITY,
        "current": float(current_value) if current_value is not None else None,
        "days": days,
        "alerts": alerts,
    }
    with _WEATHER_LOCK:
        _WEATHER_CACHE["at"] = now
        _WEATHER_CACHE["data"] = result
    return result


def _weather_alert_text(weather):
    alerts = weather.get("alerts") or []
    if not alerts:
        return None
    lines = [f"🚨 هشدار دمای 10 روز آینده — {weather.get('city') or WEATHER_CITY}"]
    for item in alerts[:5]:
        if item.get("type") == "cold":
            lines.append(f"• {item['date']}: حداقل {item['low']:.0f}°C")
        else:
            lines.append(f"• {item['date']}: افت حدود {item['drop']:.0f}°C؛ حداقل {item['low']:.0f}°C")
    return "\n".join(lines)


def _weather_watch_loop():
    time.sleep(20)
    while True:
        try:
            weather = _fetch_weather(force=True)
            fresh = []
            for alert in weather.get("alerts") or []:
                key = (alert.get("type"), alert.get("date"), round(float(alert.get("low") or 0), 1))
                if key not in _ALERTED_KEYS:
                    _ALERTED_KEYS.add(key)
                    fresh.append(alert)
            if fresh:
                temp = dict(weather)
                temp["alerts"] = fresh
                text = _weather_alert_text(temp)
                if text:
                    bot.telegram_send_message(text, bot.TELEGRAM_CHAT_ID, True)
        except Exception as exc:
            print(f"[{bot.VERSION}] weather watch: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(WEATHER_CHECK_INTERVAL)


_base_render = bot.render_dashboard


def _draw_card(d, xy, title, main, sub, accent=(46, 204, 113)):
    x1, y1, x2, y2 = xy
    d.rounded_rectangle(xy, radius=18, fill=(19, 26, 37), outline=accent, width=2)
    d.text((x1 + 24, y1 + 18), bot.rtl(title), font=bot.font(22), fill=(225, 231, 239))
    d.text((x1 + 24, y1 + 58), main, font=bot.font(34), fill=(248, 250, 252))
    d.text((x1 + 24, y1 + 104), bot.rtl(sub), font=bot.font(18), fill=(145, 158, 171))


def render_market_only(s):
    from PIL import Image, ImageDraw

    raw = _base_render(s)
    base = Image.open(io.BytesIO(raw)).convert("RGB").crop((0, 0, 1080, 790))
    canvas = Image.new("RGB", (1080, 1130), (9, 15, 24))
    canvas.paste(base, (0, 0))
    d = ImageDraw.Draw(canvas)

    d.text(
        (48, 742),
        bot.rtl("بازار: به‌روزرسانی مستقیم هر 15 دقیقه"),
        font=bot.font(18),
        fill=(125, 138, 150),
    )

    y = 805
    d.text((48, y), bot.rtl(f"هواشناسی {WEATHER_CITY}"), font=bot.font(28), fill=(235, 241, 247))

    try:
        w = _fetch_weather()
        days = w.get("days") or []
        current = w.get("current")
        today = days[0] if len(days) > 0 else None
        tomorrow = days[1] if len(days) > 1 else None

        if today:
            main = f"{current:.0f}°C" if current is not None else f"{today['high']:.0f}°C"
            sub = f"امروز | بیشینه {today['high']:.0f}°  کمینه {today['low']:.0f}°"
        else:
            main, sub = "--", "داده امروز در دسترس نیست"
        _draw_card(d, (48, 850, 510, 1000), "امروز", main, sub, (41, 182, 246))

        if tomorrow:
            main2 = f"{tomorrow['high']:.0f}° / {tomorrow['low']:.0f}°"
            sub2 = "فردا | بیشینه / کمینه"
        else:
            main2, sub2 = "--", "داده فردا در دسترس نیست"
        _draw_card(d, (570, 850, 1032, 1000), "فردا", main2, sub2, (46, 204, 113))

        alerts = w.get("alerts") or []
        if alerts:
            first = alerts[0]
            if first.get("type") == "cold":
                alert_text = f"هشدار: {first['date']} کمینه {first['low']:.0f}°C"
            else:
                alert_text = f"هشدار: {first['date']} افت {first['drop']:.0f}°C، کمینه {first['low']:.0f}°C"
            d.rounded_rectangle((48, 1025, 1032, 1092), radius=16, fill=(56, 28, 28), outline=(239, 68, 68), width=2)
            d.text((72, 1044), bot.rtl(alert_text), font=bot.font(21), fill=(255, 220, 220))
        else:
            coldest = min(days, key=lambda x: x["low"]) if days else None
            cold_text = "10 روز آینده: هشدار سرمای شدید نداریم"
            if coldest:
                cold_text += f" | کمترین {coldest['low']:.0f}°C در {coldest['date']}"
            d.rounded_rectangle((48, 1025, 1032, 1092), radius=16, fill=(20, 42, 33), outline=(46, 204, 113), width=2)
            d.text((72, 1044), bot.rtl(cold_text), font=bot.font(21), fill=(210, 246, 225))
    except Exception as exc:
        d.rounded_rectangle((48, 850, 1032, 1000), radius=18, fill=(36, 28, 28), outline=(239, 68, 68), width=2)
        d.text((72, 900), bot.rtl("هواشناسی موقتاً در دسترس نیست"), font=bot.font(24), fill=(255, 220, 220))
        print(f"[{bot.VERSION}] weather dashboard: {type(exc).__name__}: {exc}", flush=True)

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


bot.render_dashboard = render_market_only


def handle_message(m):
    chat_id = str(m.get("chat", {}).get("id", ""))
    if not chat_id or chat_id != bot.TELEGRAM_CHAT_ID:
        return

    raw = (m.get("text") or "").strip()
    low = raw.lower()

    if low in {"/start", "/help", "hello", "hi"}:
        bot.MAIN_KEYBOARD = MAIN_MENU
        bot.telegram_send_message(
            "📊 Metra CEO Intelligence\nداده‌های بازار + هواشناسی امروز، فردا و هشدار سرمای 10 روزه",
            chat_id,
            True,
        )
    elif raw in {"📊 داشبورد", "💰 ارز و طلا"} or low == "/dashboard":
        bot.MAIN_KEYBOARD = MAIN_MENU
        bot.show_dashboard(chat_id)
    elif raw == "🔄 بررسی مجدد" or low == "/refresh":
        bot.MAIN_KEYBOARD = MAIN_MENU
        started = bot.start_market_refresh(chat_id, force=True)
        try:
            _fetch_weather(force=True)
        except Exception:
            pass
        bot.telegram_send_message(
            "🔄 بازار و هواشناسی در حال به‌روزرسانی است."
            if started
            else "🔄 هواشناسی به‌روزرسانی شد؛ بازار از قبل در حال به‌روزرسانی است.",
            chat_id,
            True,
        )
    else:
        bot.MAIN_KEYBOARD = MAIN_MENU
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
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "metra-ceo-intelligence-agent",
                    "version": bot.VERSION,
                    "mode": "focused-dashboard-markets-weather",
                    "weather_city": WEATHER_CITY,
                    "weather_cold_threshold_c": WEATHER_COLD_C,
                    "weather_drop_threshold_c": WEATHER_DROP_C,
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
    threading.Thread(target=_weather_watch_loop, daemon=True).start()
    bot.startup()
