import html
import re
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "en-CA,en;q=0.9,fr-CA;q=0.8",
}

SOURCES = {
    "GCSurplus": {
        "domains": ["gcsurplus.ca"],
        "queries": [
            "laboratory equipment", "engineering equipment", "testing equipment",
            "materials testing", "construction testing", "laboratory lot",
            "lab liquidation", "laboratory closure", "surplus laboratory",
            "sieve shaker", "compression testing machine", "concrete testing",
            "soil testing", "geotechnical equipment", "asphalt testing",
        ],
        "type": "government",
    },
    "GovDeals": {
        "domains": ["govdeals.ca", "govdeals.com"],
        "queries": [
            "laboratory equipment Canada", "engineering equipment Canada",
            "testing equipment Canada", "materials testing Canada",
            "construction testing Canada", "lab liquidation Canada",
            "university laboratory surplus Canada", "concrete testing Canada",
            "soil testing Canada", "asphalt testing Canada",
        ],
        "type": "government",
    },
    "HiBid": {
        "domains": ["hibid.com"],
        "queries": [
            "laboratory equipment Canada auction", "engineering equipment Canada auction",
            "materials testing Canada auction", "testing laboratory liquidation Canada",
            "complete laboratory auction Canada", "concrete testing equipment Canada",
            "soil testing equipment Canada", "asphalt testing equipment Canada",
        ],
        "type": "industrial",
    },
    "Ritchie Bros": {
        "domains": ["rbauction.com"],
        "queries": [
            "laboratory equipment Canada", "testing equipment Canada",
            "materials testing equipment Canada", "concrete testing Canada",
            "asphalt testing Canada", "engineering equipment Canada",
        ],
        "type": "industrial",
    },
    "Industrial Auctions": {
        "domains": ["bidspotter.com", "proxibid.com", "machinio.com"],
        "queries": [
            "materials testing laboratory Canada auction", "laboratory liquidation Canada",
            "complete laboratory liquidation Canada", "engineering laboratory closure Canada",
            "construction materials testing equipment Canada", "geotechnical lab equipment Canada",
            "concrete compression tester Canada", "asphalt laboratory equipment Canada",
        ],
        "type": "industrial",
    },
}

TARGET = {
    "soil": [
        "soil", "geotechnical", "sieve", "sieve shaker", "proctor", "cbr", "atterberg",
        "triaxial", "direct shear", "consolidation", "permeability", "moisture density",
        "hydrometer", "soil compaction", "density test",
    ],
    "concrete": [
        "concrete", "compression testing", "compression tester", "cylinder", "slump", "air meter",
        "core drill", "curing", "concrete saw", "unit weight", "concrete mold", "concrete mould",
    ],
    "asphalt": [
        "asphalt", "marshall", "gyratory", "ignition oven", "bitumen", "aggregate", "extraction",
        "density gauge", "asphalt content", "rice test", "superpave",
    ],
    "general": [
        "laboratory equipment", "lab equipment", "engineering equipment", "testing equipment",
        "materials testing", "construction testing", "testing machine", "laboratory lot",
        "complete laboratory", "lab liquidation", "laboratory liquidation", "testing laboratory",
        "laboratory closure", "lab closure", "surplus laboratory", "engineering laboratory",
        "quality control laboratory", "qc laboratory",
    ],
}

BAD = [
    "pcr", "centrifuge", "microscope", "spectrometer", "medical", "biology",
    "dental", "pharmaceutical", "hospital",
]
CLOSED = [
    "sale is now closed", "auction closed", "bidding has ended", "item has been sold",
    "lot closed", "closed auction", "auction has ended", "this auction is closed",
]


def _get(url, params=None, timeout=20):
    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout, allow_redirects=True)
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


def ddg_search(domain, query, max_results=10):
    q = f"site:{domain} {query}"
    text = _get("https://html.duckduckgo.com/html/", params={"q": q}, timeout=25).text
    links = []
    pattern = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
    for href, title_html in pattern.findall(text):
        url = _unwrap_ddg(href)
        if domain not in url:
            continue
        title = _plain(title_html)
        if title and url:
            links.append((title, url))
        if len(links) >= max_results:
            break
    return links


def bing_search(domain, query, max_results=10):
    q = f"site:{domain} {query}"
    text = _get("https://www.bing.com/search", params={"q": q, "count": max_results}, timeout=25).text
    links = []
    blocks = re.findall(r'<li[^>]+class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>', text, re.I | re.S)
    for block in blocks:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.I | re.S)
        if not m:
            continue
        url, title_html = html.unescape(m.group(1)), m.group(2)
        if domain not in url:
            continue
        title = _plain(title_html)
        if title and url:
            links.append((title, url))
        if len(links) >= max_results:
            break
    return links


def multi_search(domain, query, max_results=12):
    out, seen, errors = [], set(), []
    for engine, fn in (("DDG", ddg_search), ("Bing", bing_search)):
        try:
            for title, url in fn(domain, query, max_results=max_results):
                clean = url.split("#")[0]
                if clean in seen:
                    continue
                seen.add(clean)
                out.append((title, clean, engine))
                if len(out) >= max_results:
                    return out, errors
        except Exception as exc:
            errors.append(f"{engine}:{type(exc).__name__}")
    return out, errors


def _category_and_score(text):
    low = text.lower()
    strong = TARGET["soil"] + TARGET["concrete"] + TARGET["asphalt"]
    if any(x in low for x in BAD) and not any(k in low for k in strong):
        return "general", 15

    hits_by_cat = {cat: sum(1 for k in TARGET[cat] if k in low) for cat in TARGET}
    best_cat = max(hits_by_cat, key=hits_by_cat.get)
    best_hits = hits_by_cat[best_cat]
    general_hits = hits_by_cat["general"]

    if best_hits == 0:
        return "general", 22

    score = 24 + best_hits * 11 + general_hits * 5
    if any(x in low for x in ["complete laboratory", "laboratory liquidation", "lab liquidation", "laboratory closure", "lab closure"]):
        score += 18
    if any(x in low for x in ["laboratory", "lab equipment", "materials testing", "engineering equipment", "testing equipment"]):
        score += 8
    if any(x in low for x in ["quebec", "québec", "montreal", "montréal", "laval", "longueuil"]):
        score += 10
    elif any(x in low for x in ["ontario", "ottawa", "toronto"]):
        score += 6
    if any(x in low for x in ["auction", "surplus", "liquidation", "bidding"]):
        score += 4
    return best_cat, min(score, 100)


def _extract(pattern, text, flags=re.I):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def _extract_image(page):
    for pattern in [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ]:
        m = re.search(pattern, page or "", re.I)
        if m:
            return html.unescape(m.group(1))
    return None


def inspect_listing(source_name, title, url):
    page = ""
    fetch_error = None
    try:
        page = _get(url, timeout=20).text
    except Exception as exc:
        fetch_error = type(exc).__name__

    plain = _plain(page)
    low = plain.lower()
    if plain and any(x in low for x in CLOSED):
        return None, "closed"

    combined = f"{title} {plain[:16000]}"
    category, score = _category_and_score(combined)
    if score < 28:
        return None, "low_relevance"

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
    elif plain:
        location = _extract(r"(?:Location|Located in|Item location)\s*[:\-]\s*([^|•]{2,100})", plain)
        closing = _extract(r"(?:Closing date|Ends|Auction ends|Bidding ends|End date)\s*[:\-]\s*([^|•]{3,120})", plain)
        premium = _extract(r"Buyer'?s? premium\s*[:\-]\s*([0-9.]+\s*%)", plain)
        bid_s = _extract(r"(?:Current bid|High bid|Bid)\s*[:\-]?\s*\$\s*([0-9,.]+)", plain)
        if bid_s:
            try:
                bid = float(bid_s.replace(",", ""))
            except Exception:
                pass

    reason = {
        "soil": "مرتبط با آزمایشگاه خاک/ژئوتکنیک است.",
        "concrete": "برای آزمایش و کنترل کیفیت بتن کاربرد دارد.",
        "asphalt": "برای آزمایش آسفالت/سنگدانه و روسازی کاربرد دارد.",
        "general": "فرصت عمومی خرید تجهیزات مهندسی/آزمایشگاهی است و ارزش بررسی Lot را دارد.",
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
        "image_url": _extract_image(page),
        "relevance_score": score,
        "why_it_matters": reason,
        "page_fetch": "ok" if plain else (fetch_error or "empty"),
    }, None


def _mode_accept(item, mode):
    t = f"{item.get('location') or ''} {item.get('title') or ''} {item.get('why_it_matters') or ''}".lower()
    source = (item.get("source_name") or "").lower()
    if mode == "quebec":
        return any(x in t for x in ["quebec", "québec", "montreal", "montréal", "laval", "longueuil", "gatineau", "sherbrooke"])
    if mode == "qcon":
        return any(x in t for x in ["quebec", "québec", "montreal", "montréal", "laval", "longueuil", "ontario", "ottawa", "toronto", "hamilton", "london"])
    if mode == "closures":
        return any(x in t for x in ["closure", "closing", "liquidation", "complete laboratory", "lab lot", "laboratory lot"])
    if mode == "govuni":
        return source in {"gcsurplus", "govdeals"} or any(x in t for x in ["university", "college", "government", "surplus"])
    return True


def search_lab_auctions(mode="canada"):
    candidates = []
    globally_seen = set()
    errors = []
    diagnostics = {}

    for source_name, cfg in SOURCES.items():
        stat = {
            "queries": 0, "search_hits": 0, "unique_urls": 0, "inspected": 0,
            "accepted": 0, "closed": 0, "low_relevance": 0, "filtered_by_mode": 0,
            "search_errors": [],
        }
        diagnostics[source_name] = stat
        local_seen = set()

        for domain in cfg["domains"]:
            for query in cfg["queries"]:
                stat["queries"] += 1
                results, search_errors = multi_search(domain, query, max_results=12)
                stat["search_hits"] += len(results)
                stat["search_errors"].extend(search_errors)
                for title, url, engine in results:
                    clean = url.split("#")[0]
                    if clean in local_seen or clean in globally_seen:
                        continue
                    local_seen.add(clean)
                    globally_seen.add(clean)
                    stat["unique_urls"] += 1
                    stat["inspected"] += 1
                    item, rejection = inspect_listing(source_name, title, clean)
                    if item:
                        item["search_engine"] = engine
                        if _mode_accept(item, mode):
                            candidates.append(item)
                            stat["accepted"] += 1
                        else:
                            stat["filtered_by_mode"] += 1
                    elif rejection == "closed":
                        stat["closed"] += 1
                    elif rejection == "low_relevance":
                        stat["low_relevance"] += 1

        if stat["search_errors"]:
            uniq = list(dict.fromkeys(stat["search_errors"]))
            stat["search_errors"] = uniq[:6]
            errors.extend(f"{source_name}:{e}" for e in uniq[:2])

    def geo_rank(item):
        t = f"{item.get('location') or ''} {item.get('title') or ''}".lower()
        if any(x in t for x in ["quebec", "québec", "montreal", "montréal", "laval", "longueuil"]):
            return 0
        if any(x in t for x in ["ontario", "ottawa", "toronto", "london, on"]):
            return 1
        return 2

    dedup = {}
    for item in candidates:
        host = urlparse(item.get("source_url") or "").netloc.lower().replace("www.", "")
        key = (re.sub(r"\W+", " ", (item.get("title") or "").lower()).strip()[:100], host)
        old = dedup.get(key)
        if not old or int(item.get("relevance_score") or 0) > int(old.get("relevance_score") or 0):
            dedup[key] = item

    candidates = list(dedup.values())
    candidates.sort(key=lambda x: (geo_rank(x), -int(x.get("relevance_score") or 0)))

    return {
        "items": candidates[:15],
        "checked_at": datetime.now().isoformat(timespec="minutes"),
        "errors": list(dict.fromkeys(errors))[:10],
        "diagnostics": diagnostics,
        "mode": mode,
        "engine": "direct-web-broad-lab-acquisition",
    }
