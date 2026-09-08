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
    "Centris": ["centris.ca"],
    "REALTOR.ca": ["realtor.ca"],
    "Spacelist": ["spacelist.ca"],
    "LoopNet": ["loopnet.ca", "loopnet.com"],
    "CBRE": ["cbre.ca"],
    "Colliers": ["collierscanada.com", "colliers.com"],
}

REGION_QUERIES = {
    "montreal": ["Montreal industrial space for lease", "Montréal local industriel à louer"],
    "laval": ["Laval industrial space for lease", "Laval local industriel à louer"],
    "southshore": ["Longueuil Brossard industrial space for lease", "Rive-Sud local industriel à louer"],
    "westisland": ["West Island Montreal industrial space for lease", "Saint-Laurent Dorval Pointe-Claire industrial space for lease"],
    "grandmontreal": ["Greater Montreal industrial space for lease", "Grand Montréal local industriel à louer"],
}

LAB_HINTS = [
    "industrial", "warehouse", "light industrial", "flex space", "garage", "loading", "drive-in",
    "dock", "concrete floor", "drain", "220v", "240v", "208v", "3 phase", "three phase",
    "ventilation", "water", "washroom", "sprinkler", "yard", "zoning", "laboratory", "lab",
]

BAD = ["office only", "coworking", "retail only", "medical office", "virtual office"]


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


def multi_search(domain, query, max_results=10):
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


def _extract(pattern, text, flags=re.I):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def _score(text):
    low = text.lower()
    if any(x in low for x in BAD):
        return 5
    score = 30
    hits = sum(1 for x in LAB_HINTS if x in low)
    score += min(45, hits * 5)
    if any(x in low for x in ["garage", "drive-in", "loading dock", "dock door"]):
        score += 8
    if any(x in low for x in ["3 phase", "three phase", "600v", "240v", "208v"]):
        score += 8
    if any(x in low for x in ["drain", "floor drain", "water"]):
        score += 5
    if any(x in low for x in ["montreal", "montréal", "laval", "longueuil", "brossard", "dorval", "saint-laurent", "pointe-claire"]):
        score += 4
    return min(score, 100)


def inspect_listing(source_name, title, url):
    try:
        page = _get(url, timeout=20).text
    except Exception:
        page = ""
    plain = _plain(page)
    combined = f"{title} {plain[:16000]}"
    score = _score(combined)
    if score < 35:
        return None

    area = _extract(r"([0-9,]{3,10})\s*(?:sq\.?\s*ft|sf|pi2|pi²|pieds carrés)", combined)
    price = _extract(r"\$\s*([0-9,.]+)\s*(?:/\s*(?:sf|pi2|pi²|sq\.?\s*ft)|per square foot)", combined)
    monthly = _extract(r"\$\s*([0-9,.]+)\s*(?:/\s*month|par mois|monthly)", combined)
    address = _extract(r"([0-9]{1,6}\s+[A-Za-zÀ-ÿ0-9 .'-]{3,80}(?:Montreal|Montréal|Laval|Longueuil|Brossard|Dorval|Pointe-Claire|Saint-Laurent)[^|•]{0,40})", combined)

    low = combined.lower()
    features = []
    for label, keys in [
        ("garage/loading", ["garage", "drive-in", "loading dock", "dock door"]),
        ("3-phase/high-voltage", ["3 phase", "three phase", "600v", "240v", "208v"]),
        ("water/drain", ["floor drain", "drain", "water"]),
        ("ventilation", ["ventilation", "exhaust"]),
        ("industrial zoning", ["industrial zoning", "zoning industriel", "light industrial"]),
    ]:
        if any(k in low for k in keys):
            features.append(label)

    return {
        "title": title[:180],
        "address": address,
        "area_sf": int(area.replace(",", "")) if area else None,
        "price_per_sf": float(price.replace(",", "")) if price else None,
        "monthly_rent": float(monthly.replace(",", "")) if monthly else None,
        "features": features,
        "source_name": source_name,
        "source_url": url,
        "suitability_score": score,
    }


def search_lab_rentals(region="grandmontreal"):
    queries = REGION_QUERIES.get(region, REGION_QUERIES["grandmontreal"])
    items, seen, errors, diagnostics = [], set(), [], {}

    for source_name, domains in SOURCES.items():
        st = {"search_hits": 0, "inspected": 0, "accepted": 0, "errors": []}
        diagnostics[source_name] = st
        for domain in domains:
            for query in queries:
                results, errs = multi_search(domain, query, max_results=8)
                st["search_hits"] += len(results)
                st["errors"].extend(errs)
                for title, url, engine in results:
                    if url in seen:
                        continue
                    seen.add(url)
                    st["inspected"] += 1
                    item = inspect_listing(source_name, title, url)
                    if item:
                        item["search_engine"] = engine
                        items.append(item)
                        st["accepted"] += 1
        st["errors"] = list(dict.fromkeys(st["errors"]))[:3]
        errors.extend(f"{source_name}:{e}" for e in st["errors"])

    dedup = {}
    for item in items:
        key = re.sub(r"\W+", " ", (item.get("title") or "").lower()).strip()[:100]
        old = dedup.get(key)
        if not old or item["suitability_score"] > old["suitability_score"]:
            dedup[key] = item
    items = list(dedup.values())
    items.sort(key=lambda x: -int(x.get("suitability_score") or 0))

    return {
        "items": items[:12],
        "checked_at": datetime.now().isoformat(timespec="minutes"),
        "region": region,
        "diagnostics": diagnostics,
        "errors": list(dict.fromkeys(errors))[:8],
        "mode": "direct-web-lab-rental",
    }
