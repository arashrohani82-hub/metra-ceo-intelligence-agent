import time
from datetime import date, datetime

from main_v12 import app as bot

bot.VERSION = "CEO-BOT-V13-FLIGHTS"

_FLIGHT_CACHE = {"at": 0.0, "data": None}
_FLIGHT_CACHE_SECONDS = 5 * 60


def _days(a, b):
    try:
        da = datetime.strptime(a, "%Y-%m-%d").date()
        db = datetime.strptime(b, "%Y-%m-%d").date()
        return (db - da).days, da
    except Exception:
        return None, None


def _valid(route, item):
    if not isinstance(item, dict):
        return False
    try:
        price = float(item.get("price_cad"))
    except Exception:
        return False
    outbound = item.get("outbound")
    ret = item.get("return")
    url = item.get("source_url")
    if not outbound or not ret or not url:
        return False
    stay, dep = _days(outbound, ret)
    if stay is None or dep is None or dep < date.today():
        return False
    if route == "iran":
        return 500 <= price <= 5000 and 7 <= stay <= 30
    return 80 <= price <= 1500 and 3 <= stay <= 7


def _fetch_both_flights():
    now = time.time()
    if _FLIGHT_CACHE["data"] and now - _FLIGHT_CACHE["at"] < _FLIGHT_CACHE_SECONDS:
        return _FLIGHT_CACHE["data"]

    prompt = f"""
Current time: {datetime.now().isoformat(timespec='minutes')}.
Find the cheapest CURRENTLY VISIBLE round-trip economy fare in CAD for ONE adult for BOTH routes below.

A) Montreal YUL <-> Tehran IKA: departure within next 90 days, stay 7-30 nights.
B) Montreal YUL <-> Vancouver YVR: departure within next 60 days, stay 3-7 nights.

Accuracy rules:
- A fare is usable only if one source visibly supports the TOTAL ROUND-TRIP CAD price and exact outbound + return dates for the SAME itinerary.
- Reject one-way prices, 'from' teaser prices, monthly estimates, stale snippets, packages, points, or mixed itineraries.
- Prefer Google Flights, airline sites, and major booking-engine result pages when the exact itinerary is visible.
- If no defensible fare is visible for a route, return nulls for that route. Never estimate.
- source_url must be the URL supporting that exact fare or the closest directly-checkable booking/search page.

Return ONLY valid JSON exactly in this shape:
{{
  "iran": {{"price_cad": number|null, "outbound": "YYYY-MM-DD"|null, "return": "YYYY-MM-DD"|null, "airline": string|null, "stops": string|null, "source_url": string|null, "source_name": string|null}},
  "vancouver": {{"price_cad": number|null, "outbound": "YYYY-MM-DD"|null, "return": "YYYY-MM-DD"|null, "airline": string|null, "stops": string|null, "source_url": string|null, "source_name": string|null}}
}}
"""
    data = bot.openai_json(prompt, max_tokens=2200, timeout=120)
    if not isinstance(data, dict):
        raise RuntimeError("invalid combined flight response")
    _FLIGHT_CACHE["at"] = now
    _FLIGHT_CACHE["data"] = data
    return data


def build_flight_v13(route, old_flight):
    data = _fetch_both_flights()
    item = data.get(route) or {}
    if not _valid(route, item):
        return old_flight or {}, "old" if old_flight else "missing"
    return {
        "price_cad": round(float(item["price_cad"])),
        "outbound": item["outbound"],
        "return": item["return"],
        "airline": item.get("airline") or "N/A",
        "stops": item.get("stops") or "N/A",
        "source_url": item["source_url"],
        "source_name": item.get("source_name") or "",
    }, "primary"


bot.build_flight = build_flight_v13

if __name__ == "__main__":
    bot.startup()
