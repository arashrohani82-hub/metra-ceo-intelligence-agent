import io
import threading
import time
from collections import defaultdict
from datetime import datetime

RESOURCE_ID = "5232a72d-235a-48eb-ae20-bb9d501300ad"
API_URL = "https://www.donneesquebec.ca/recherche/api/3/action/datastore_search_sql"
TTL = 6 * 60 * 60
_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _month_shift(dt, months):
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    return dt.replace(year=y, month=m, day=1)


def fetch_permits(bot, force=False):
    """Aggregate recent Montreal permits directly from the official CKAN DataStore."""
    now_ts = time.time()
    with _lock:
        if not force and _cache["data"] and now_ts - _cache["at"] < TTL:
            return _cache["data"]

    now = datetime.now()
    start = _month_shift(now.replace(day=1), -14).strftime("%Y-%m-%d")
    sql = f'''SELECT
        to_char(date_trunc('month', "date_emission"::timestamp), 'YYYY-MM') AS month,
        "arrondissement",
        "code_type_base_demande",
        count(*) AS n
      FROM "{RESOURCE_ID}"
      WHERE "date_emission" >= '{start}'
      GROUP BY 1, 2, 3
      ORDER BY 1 DESC'''

    payload = bot.safe_get(API_URL, params={"sql": sql}, timeout=30).json()
    if not payload.get("success"):
        raise RuntimeError("Données Québec permit API returned success=false")
    records = ((payload.get("result") or {}).get("records") or [])
    if not records:
        raise RuntimeError("No Montreal permit records returned")

    months = defaultdict(lambda: {
        "total": 0, "CO": 0, "TR": 0, "DE": 0, "CA": 0,
        "arr": defaultdict(int),
    })
    for row in records:
        month = str(row.get("month") or "")
        if not month:
            continue
        try:
            n = int(row.get("n") or 0)
        except Exception:
            n = 0
        code = str(row.get("code_type_base_demande") or "").upper()
        arr = str(row.get("arrondissement") or "N/D")
        months[month]["total"] += n
        if code in {"CO", "TR", "DE", "CA"}:
            months[month][code] += n
        months[month]["arr"][arr] += n

    current_month = now.strftime("%Y-%m")
    last_complete = _month_shift(now.replace(day=1), -1).strftime("%Y-%m")
    prev_complete = _month_shift(now.replace(day=1), -2).strftime("%Y-%m")
    yoy_month = _month_shift(now.replace(day=1), -13).strftime("%Y-%m")

    def pack(month):
        item = months.get(month) or {
            "total": 0, "CO": 0, "TR": 0, "DE": 0, "CA": 0, "arr": {},
        }
        return {
            "month": month,
            "total": item["total"],
            "CO": item["CO"],
            "TR": item["TR"],
            "DE": item["DE"],
            "CA": item["CA"],
            "top": sorted(item["arr"].items(), key=lambda x: x[1], reverse=True)[:3],
        }

    result = {
        "current": pack(current_month),
        "last": pack(last_complete),
        "previous": pack(prev_complete),
        "yoy": pack(yoy_month),
        "source": "Ville de Montréal / Données Québec",
    }
    with _lock:
        _cache["at"] = now_ts
        _cache["data"] = result
    return result


def _pct(new, old):
    if not old:
        return None
    return (new / old - 1.0) * 100.0


def append_permits(bot, image_bytes):
    """Append a compact Montreal permit market section to an existing dashboard PNG."""
    from PIL import Image, ImageDraw

    base = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = base.size
    canvas = Image.new("RGB", (w, h + 330), (9, 15, 24))
    canvas.paste(base, (0, 0))
    d = ImageDraw.Draw(canvas)
    y = h + 20
    d.text((48, y), bot.rtl("پرمیت‌های ساختمانی Montréal"), font=bot.font(28), fill=(235, 241, 247))

    try:
        p = fetch_permits(bot)
        last = p["last"]
        prev = p["previous"]
        yoy = p["yoy"]
        current = p["current"]
        mom = _pct(last["total"], prev["total"])
        yoy_pct = _pct(last["total"], yoy["total"])

        d.rounded_rectangle(
            (48, y + 45, 1032, y + 185), radius=18,
            fill=(19, 26, 37), outline=(99, 102, 241), width=2,
        )
        d.text((72, y + 65), bot.rtl(f"آخرین ماه کامل: {last['month']}"), font=bot.font(21), fill=(190, 200, 214))
        d.text((72, y + 98), f"{last['total']:,}", font=bot.font(38), fill=(248, 250, 252))
        d.text((245, y + 105), bot.rtl("پرمیت صادرشده"), font=bot.font(21), fill=(225, 231, 239))

        mom_text = "—" if mom is None else f"{mom:+.1f}%"
        yoy_text = "—" if yoy_pct is None else f"{yoy_pct:+.1f}%"
        breakdown = f"CO {last['CO']:,}  |  TR {last['TR']:,}  |  DE {last['DE']:,}  |  CA {last['CA']:,}"
        d.text((72, y + 150), breakdown, font=bot.font(18), fill=(145, 158, 171))
        d.text((650, y + 105), f"MoM {mom_text}   YoY {yoy_text}", font=bot.font(20), fill=(190, 200, 214))

        current_text = f"ماه جاری {current['month']}: {current['total']:,} تا امروز"
        d.text((72, y + 205), bot.rtl(current_text), font=bot.font(20), fill=(225, 231, 239))

        if last["top"]:
            top_text = " | ".join(f"{name}: {count:,}" for name, count in last["top"])
            d.text((72, y + 240), bot.rtl("Top arrondissements: " + top_text), font=bot.font(17), fill=(145, 158, 171))

        d.text(
            (72, y + 278),
            bot.rtl("منبع: Ville de Montréal / Données Québec • به‌روزرسانی هفتگی"),
            font=bot.font(16), fill=(112, 125, 139),
        )
    except Exception as exc:
        d.rounded_rectangle(
            (48, y + 45, 1032, y + 185), radius=18,
            fill=(36, 28, 28), outline=(239, 68, 68), width=2,
        )
        d.text(
            (72, y + 95), bot.rtl("داده پرمیت‌های Montréal موقتاً در دسترس نیست"),
            font=bot.font(23), fill=(255, 220, 220),
        )
        print(f"[{bot.VERSION}] permits dashboard: {type(exc).__name__}: {exc}", flush=True)

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()
