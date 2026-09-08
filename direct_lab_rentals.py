import html
import re
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

import requests

HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36","Accept-Language":"en-CA,en;q=0.9,fr-CA;q=0.8"}
SOURCES={"Centris":["centris.ca"],"REALTOR.ca":["realtor.ca"],"Spacelist":["spacelist.ca"],"LoopNet":["loopnet.ca","loopnet.com"],"CBRE":["cbre.ca"],"Colliers":["collierscanada.com","colliers.com"]}
REGION_QUERIES={
"montreal":["Montreal industrial space lease warehouse garage","Montréal local industriel à louer entrepôt garage"],
"laval":["Laval industrial space lease warehouse garage","Laval local industriel à louer entrepôt"],
"southshore":["Longueuil Brossard industrial space lease warehouse","Rive-Sud local industriel à louer entrepôt"],
"westisland":["West Island Montreal industrial space lease","Saint-Laurent Dorval Pointe-Claire industrial space lease"],
"grandmontreal":["Greater Montreal industrial space lease warehouse","Grand Montréal local industriel à louer entrepôt"]}
LAB_HINTS=["industrial","warehouse","entrepôt","local industriel","light industrial","flex space","garage","loading","drive-in","dock","concrete floor","drain","220v","240v","208v","600v","3 phase","three phase","ventilation","water","sprinkler","yard","zoning","laboratory","lab"]
BAD=["office only","coworking","retail only","medical office","virtual office"]

def _get(url,params=None,timeout=20):
 r=requests.get(url,params=params,headers=HEADERS,timeout=timeout,allow_redirects=True);r.raise_for_status();return r

def _plain(s):
 s=re.sub(r"<script.*?</script>|<style.*?</style>"," ",s or "",flags=re.I|re.S);s=re.sub(r"<[^>]+>"," ",s);return re.sub(r"\s+"," ",html.unescape(s)).strip()

def _unwrap(href):
 href=html.unescape(href or "")
 if "duckduckgo.com/l/?" in href:
  q=parse_qs(urlparse(href).query)
  if q.get("uddg"): return unquote(q["uddg"][0])
 if href.startswith("//"): return "https:"+href
 return href

def ddg_search(domain,query,max_results=10):
 text=_get("https://html.duckduckgo.com/html/",{"q":f"site:{domain} {query}"},25).text;out=[]
 for href,t in re.findall(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',text,re.I|re.S):
  u=_unwrap(href)
  if domain in u:
   out.append((_plain(t),u))
   if len(out)>=max_results: break
 return out

def bing_search(domain,query,max_results=10):
 text=_get("https://www.bing.com/search",{"q":f"site:{domain} {query}","count":max_results},25).text;out=[]
 for block in re.findall(r'<li[^>]+class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',text,re.I|re.S):
  m=re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',block,re.I|re.S)
  if m and domain in m.group(1): out.append((_plain(m.group(2)),html.unescape(m.group(1))))
 return out[:max_results]

def google_search(domain,query,max_results=10):
 text=_get("https://www.google.com/search",{"q":f"site:{domain} {query}","num":max_results},25).text;out=[]
 for href in re.findall(r'href="(/url\?q=[^"&]+|https?://[^"&]+)"',text,re.I):
  if href.startswith('/url?q='): href=unquote(href[7:])
  if domain in href and not any(href==u for _,u in out):
   out.append((query,href))
   if len(out)>=max_results: break
 return out

def multi_search(domain,query,max_results=10):
 out=[];seen=set();errors=[]
 # Do not let a DDG rate-limit kill discovery. Bing and Google are independent fallbacks.
 for engine,fn in (("Bing",bing_search),("Google",google_search),("DDG",ddg_search)):
  try:
   for title,url in fn(domain,query,max_results):
    clean=url.split("#")[0]
    if clean not in seen:
     seen.add(clean);out.append((title,clean,engine))
     if len(out)>=max_results:return out,errors
  except Exception as exc: errors.append(f"{engine}:{type(exc).__name__}")
 return out,errors

def _extract(p,t):
 m=re.search(p,t,re.I);return m.group(1).strip() if m else None

def _score(text):
 low=text.lower()
 if any(x in low for x in BAD):return 5
 hits=sum(1 for x in LAB_HINTS if x in low);score=25+min(50,hits*5)
 if any(x in low for x in ["garage","drive-in","loading dock","dock door"]):score+=8
 if any(x in low for x in ["3 phase","three phase","600v","240v","208v"]):score+=8
 if any(x in low for x in ["drain","floor drain","water"]):score+=5
 return min(score,100)

def inspect_listing(source,title,url):
 try: plain=_plain(_get(url,timeout=18).text)
 except Exception: plain=""
 combined=f"{title} {plain[:18000]}";score=_score(combined)
 # Search-engine result titles are still useful candidates even if the listing blocks scraping.
 if score<30:return None
 area=_extract(r"([0-9,]{3,10})\s*(?:sq\.?\s*ft|sf|pi2|pi²|pieds carrés)",combined)
 price=_extract(r"\$\s*([0-9,.]+)\s*(?:/\s*(?:sf|pi2|pi²|sq\.?\s*ft)|per square foot)",combined)
 monthly=_extract(r"\$\s*([0-9,.]+)\s*(?:/\s*month|par mois|monthly)",combined)
 low=combined.lower();features=[]
 for label,keys in [("garage/loading",["garage","drive-in","loading dock","dock door"]),("3-phase/high-voltage",["3 phase","three phase","600v","240v","208v"]),("water/drain",["floor drain","drain","water"]),("ventilation",["ventilation","exhaust"]),("industrial",["industrial","entrepôt","warehouse"] )]:
  if any(k in low for k in keys):features.append(label)
 return {"title":title[:180],"address":None,"area_sf":int(area.replace(',','')) if area else None,"price_per_sf":float(price.replace(',','')) if price else None,"monthly_rent":float(monthly.replace(',','')) if monthly else None,"features":features,"source_name":source,"source_url":url,"suitability_score":score}

def search_lab_rentals(region="grandmontreal"):
 queries=REGION_QUERIES.get(region,REGION_QUERIES["grandmontreal"]);items=[];seen=set();errors=[];diagnostics={}
 for source,domains in SOURCES.items():
  st={"search_hits":0,"inspected":0,"accepted":0,"errors":[]};diagnostics[source]=st
  for domain in domains:
   for query in queries:
    results,errs=multi_search(domain,query,8);st["search_hits"]+=len(results);st["errors"].extend(errs)
    for title,url,engine in results:
     if url in seen:continue
     seen.add(url);st["inspected"]+=1;item=inspect_listing(source,title,url)
     if item:item["search_engine"]=engine;items.append(item);st["accepted"]+=1
  st["errors"]=list(dict.fromkeys(st["errors"]))[:3];errors.extend(f"{source}:{e}" for e in st["errors"])
 dedup={}
 for item in items:
  key=item["source_url"].split('?')[0]
  if key not in dedup or item["suitability_score"]>dedup[key]["suitability_score"]:dedup[key]=item
 items=sorted(dedup.values(),key=lambda x:-int(x.get("suitability_score") or 0))
 return {"items":items[:15],"checked_at":datetime.now().isoformat(timespec="minutes"),"region":region,"diagnostics":diagnostics,"errors":list(dict.fromkeys(errors))[:12],"mode":"direct-web-lab-rental-v2"}
