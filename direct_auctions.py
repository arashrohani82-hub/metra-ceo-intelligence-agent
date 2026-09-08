import html
import re
from datetime import datetime
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MetraAuctionBot/1.0)",
    "Accept-Language": "en-CA,en;q=0.9,fr-CA;q=0.8",
}

SEARCHES = [
    ("GCSurplus", "gcsurplus.ca", "laboratory equipment soil concrete asphalt geotechnical testing Canada"),
    ("GCSurplus", "gcsurplus.ca", "sieve shaker compression testing machine laboratory equipment Canada"),
    ("GovDeals", "govdeals.ca", "laboratory testing equipment concrete soil asphalt Canada auction"),
    ("HiBid", "hibid.com", "laboratory equipment concrete soil asphalt Canada auction"),
    ("Ritchie Bros", "rbauction.com", "laboratory testing equipment concrete asphalt Canada auction"),
]

TARGET = {
    "soil": ["soil", "geotechnical", "sieve", "proctor", "cbr", "atterberg", "triaxial", "direct shear", "consolidation", "permeability", "moisture density"],
    "concrete": ["concrete", "compression testing", "cylinder", "slump", "air meter", "core drill", "curing", "concrete saw"],
    "asphalt": ["asphalt", "marshall", "gyratory", "ignition oven", "bitumen", "aggregate", "extraction", "density gauge"],
    "general": ["laboratory equipment", "lab equipment", "materials testing", "testing machine", "laboratory lot", "lab liquidation"],
}

BAD = ["pcr", "centrifuge", "microscope", "spectrometer", "medical", "chemistry", "biology", "incubator"]
CLOSED = ["sale is now closed", "auction closed", "bidding has ended", "item has been sold", "lot closed", "closed auction"]


def _get(url, params=None, timeout=20):
    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def _plain(s):
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s or "", flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _unwrap_ddg(href):
    href = html.unescape(href or "")
    if "duckduckgo.com/l/?" in href:
        q = parse_qs(urlparse(href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    if href.startswith("//"):
        return "https:" + href
    return href


def ddg_search(domain, query, max_results=8):
    q = f"site:{domain} {query}"
    text = _get("https://html.duckduckgo.com/html/", params={"q": q}, timeout=25).text
    links = []
    pattern = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
    for href, title_html in pattern.findall(text):
        url = _unwrap_ddg(href)
        if domain not in url:
            continue
        title = _plain(title_html)
        if not title or not url:
            continue
        links.append((title, url))
        if len(links) >= max_results:
            break
    return links


def _category_and_score(text):
    low = text.lower()
    if any(x in low for x in BAD) and not any(k in low for k in TARGET["soil"] + TARGET["concrete"] + TARGET["asphalt"]):
        return "general", 20
    best_cat, best_hits = "general", 0
    for cat in ("soil", "concrete", "asphalt", "general"):
        hits = sum(1 for k in TARGET[cat] if k in low)
        if hits > best_hits:
            best_cat, best_hits = cat, hits
    score = min(98, 35 + best_hits * 16)
    if "laboratory" in low or "lab " in low:
        score += 5
    if any(x in low for x in ["quebec", "québec", "montreal", "montréal", "laval", "longueuil", "ottawa", "ontario"]):
        score += 5
    return best_cat, min(score, 100)


def _extract(pattern, text, flags=re.I):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def inspect_listing(source_name, title, url):
    try:
        page = _get(url, timeout=20).text
    except Exception:
        page = ""
    plain = _plain(page)
    low = plain.lower()
    if plain and any(x in low for x in CLOSED):
        return None

    combined = f"{title} {plain[:9000]}"
    category, score = _category_and_score(combined)
    if score < 45:
        return None

    item_title = title
    location = None
    closing = None
    bid = None
    premium = None

    if source_name == "GCSurplus" and plain:
        item_title = _extract(r"Item:\s*(.+?)(?:Minimum bid:|Current Bid|Closing date:)", plain) or item_title
        location = _extract(r"Location\s*:\s*(.+?)(?:Sale / Lot|Closing date|Quantity)", plain)
        closing = _extract(r"Closing date:\s*(.+?)(?:Remaining:|Quantity:|Pictures|Details)", plain)
        bid_s = _extract(r"Current Bid \(CAD\):\s*\$\s*([0-9,.]+)", plain)
        if bid_s:
            try:
                bid = float(bid_s.replace(",", ""))
            except Exception:
                pass
    else:
        location = _extract(r"(?:Location|Located in)\s*[:\-]\s*([^|•]{2,80})", plain)
        closing = _extract(r"(?:Closing date|Ends|Auction ends|Bidding ends)\s*[:\-]\s*([^|•]{3,100})", plain)
        prem = _extract(r"Buyer'?s? premium\s*[:\-]\s*([0-9.]+\s*%)", plain)
        if prem:
            premium = prem
        bid_s = _extract(r"(?:Current bid|Bid)\s*[:\-]?\s*\$\s*([0-9,.]+)", plain)
        if bid_s:
            try:
                bid = float(bid_s.replace(",", ""))
            except Exception:
                pass

    reason = {
        "soil": "مرتبط با راه‌اندازی یا تکمیل آزمایشگاه خاک/ژئوتکنیک است.",
        "concrete": "برای آزمایش‌های بتن و کنترل کیفیت مصالح ساختمانی کاربرد دارد.",
        "asphalt": "برای آزمایش‌های آسفالت/سنگدانه و کنترل کیفیت روسازی کاربرد دارد.",
        "general": "تجهیزات عمومی آزمایشگاهی است؛ مشخصات Lot باید قبل از Bid بررسی شود.",
    }[category]

    return {
        "title": item_title[:180],
        "category": category,
        "location": location,
        "closing_date": closing,
        "current_bid_cad": bid,
        "buyer_premium": premium,
        "source_name": source_name,
        "source_url": url,
        "relevance_score": score,
        "why_it_matters": reason,
    }


def search_lab_auctions():
    candidates = []
    seen = set()
    errors = []
    for source_name, domain, query in SEARCHES:
        try:
            for title, url in ddg_search(domain, query, max_results=7):
                clean = url.split("#")[0]
                if clean in seen:
                    continue
                seen.add(clean)
                item = inspect_listing(source_name, title, clean)
                if item:
                    candidates.append(item)
        except Exception as exc:
            errors.append(f"{source_name}:{type(exc).__name__}")

    # Québec first, Ontario second, then relevance.
    def geo_rank(item):
        t = f"{item.get('location') or ''} {item.get('title') or ''}".lower()
        if any(x in t for x in ["quebec", "québec", "montreal", "montréal", "laval", "longueuil"]):
            return 0
        if any(x in t for x in ["ontario", "ottawa", "toronto", "london, on", " on "]):
            return 1
        return 2

    candidates.sort(key=lambda x: (geo_rank(x), -int(x.get("relevance_score") or 0)))
    return {
        "items": candidates[:8],
        "checked_at": datetime.now().isoformat(timespec="minutes"),
        "errors": errors,
        "mode": "direct-web-no-openai",
    }
