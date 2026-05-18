import csv, json, logging, os, re, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

RECORDER_BASE = "https://legacy.recorder.maricopa.gov/recdocdata"
RECORDER_SEARCH = f"{RECORDER_BASE}/GetRecDataRecentPgDn.aspx"
REPO_ROOT = Path(__file__).parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON = REPO_ROOT / "data" / "records.json"

LEAD_TYPES = {
    "LP": ("LP", "Lis Pendens"),
    "NOFC": ("NOFC", "Notice of Foreclosure"),
    "TAXDEED": ("TAXD", "Tax Deed"),
    "JUD": ("JUD", "Judgment"),
    "LNFED": ("FEDTL", "Federal Tax Lien"),
    "LNSTATE": ("STTL", "State Tax Lien"),
    "LN": ("LIEN", "Lien"),
    "LNMECH": ("MECH", "Mechanic Lien"),
    "MEDLN": ("MEDN", "Medical Lien"),
    "PRO": ("PROB", "Probate"),
    "RELLP": ("RLLP", "Release Lis Pendens"),
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer": "https://legacy.recorder.maricopa.gov/recdocdata/",
}

def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    try: s.get("https://legacy.recorder.maricopa.gov/recdocdata/", timeout=15)
    except Exception: pass
    return s

def build_url(code, dfrom, dto, start=0):
    b = dfrom.replace("/", "%2F")
    e = dto.replace("/", "%2F")
    url = f"{RECORDER_SEARCH}?rec={start}&suf=&nm=&bdt={b}&edt={e}&cde={code}&max=20&res=True&doc1={code}&doc2=&doc3=&doc4=&doc5="
    api_key = os.environ.get("SCRAPER_API_KEY", "")
    return f"http://api.scraperapi.com?api_key={api_key}&url={url}" if api_key else url

def fetch_page(session, url):
    for i in range(3):
        try:
            r = session.get(url, timeout=60)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log.warning(f"Attempt {i+1}: {e}")
            time.sleep(5)
    return None

def parse_page(soup, code, label):
    records = []
    table = next((t for t in soup.find_all("table") if "RECORDING NUMBER" in t.get_text("|").upper()), None)
    if not table: return records
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 2: continue
        try:
            num = cells[0].get_text(strip=True)
            date = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            raw = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            link = cells[0].find("a")
            if link and link.get("href"):
                href = link["href"]
                url = href if href.startswith("http") else RECORDER_BASE + "/" + href.lstrip("/")
            else:
                url = f"{RECORDER_BASE}/GetRecordedDocData.aspx?rec={re.sub(r'[^0-9]','',num)}"
            if not num: continue
            records.append({"doc_num":num,"doc_type":raw or code,"cat":code,"cat_label":label,
                "filed":_nd(date),"owner":"","grantee":"","legal":"","amount":None,"clerk_url":url,
                "prop_address":"","prop_city":"","prop_state":"AZ","prop_zip":"",
                "mail_address":"","mail_city":"","mail_state":"AZ","mail_zip":""})
        except Exception as e: log.debug(f"Row: {e}")
    return records

def fetch_detail(session, url):
    r = {"owner":"","grantee":"","legal":"","amount":None}
    try:
        soup = fetch_page(session, url)
        if not soup: return r
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                lbl = cells[0].get_text(strip=True).lower()
                val = cells[1].get_text(strip=True)
                if "grantor" in lbl and not r["owner"]: r["owner"] = val
                elif "grantee" in lbl and not r["grantee"]: r["grantee"] = val
                elif "legal" in lbl and not r["legal"]: r["legal"] = val
                elif "consideration" in lbl and not r["amount"]: r["amount"] = _pa(val)
    except Exception as e: log.debug(f"Detail: {e}")
    return r

def scrape_all(dfrom, dto):
    session = make_session()
    all_r = []
    for code, (url_code, label) in LEAD_TYPES.items():
        log.info(f"Searching: {label}")
        try:
            start = 0
            while True:
                soup = fetch_page(session, build_url(url_code, dfrom, dto, start))
                if not soup: break
                batch = parse_page(soup, code, label)
                if not batch: break
                for rec in batch:
                    try: rec.update(fetch_detail(session, rec["clerk_url"])); time.sleep(0.3)
                    except Exception: pass
                all_r.extend(batch)
                log.info(f"  {len(batch)} records")
                if len(batch) >= 100: start += 100; time.sleep(1)
                else: break
            time.sleep(2)
        except Exception as e: log.error(f"Failed {label}: {e}")
    return all_r

def score(rec, all_r):
    flags, s = [], 30
    cat = rec.get("cat",""); lbl = rec.get("cat_label","").lower()
    amt = rec.get("amount"); owner = rec.get("owner",""); filed = rec.get("filed","")
    if cat=="LP": flags.append("Lis pendens")
    if cat in("LP","NOFC"): flags.append("Pre-foreclosure")
    if cat=="JUD": flags.append("Judgment lien")
    if cat=="TAXDEED": flags.append("Tax lien")
    if cat=="PRO": flags.append("Probate / estate")
    if cat=="LN":
        if "tax" in lbl: flags.append("Tax lien")
        elif "mechanic" in lbl: flags.append("Mechanic lien")
        else: flags.append("Lien")
    if owner and any(k in owner.upper() for k in ("LLC","INC","CORP","TRUST")): flags.append("LLC / corp owner")
    try:
        if (datetime.now().date()-datetime.strptime(filed,"%Y-%m-%d").date()).days<=7: flags.append("New this week"); s+=5
    except Exception: pass
    s += len(set(flags))*10
    if amt: s += 15 if amt>100000 else (10 if amt>50000 else 0)
    if rec.get("prop_address") or rec.get("mail_address"): s+=5
    return min(s,100), list(dict.fromkeys(flags))

def _nd(raw):
    for fmt in ("%m/%d/%Y","%Y-%m-%d","%m/%d/%y"):
        try: return datetime.strptime(raw.strip(),fmt).strftime("%Y-%m-%d")
        except Exception: pass
    return raw.strip()

def _pa(raw):
    c = re.sub(r"[^\d.]","",str(raw or ""))
    try: v=float(c); return v if v>0 else None
    except Exception: return None

def _sn(full):
    n=full.strip()
    if any(k in n.upper() for k in ("LLC","INC","CORP","TRUST","ESTATE")): return "",n
    if "," in n: p=[x.strip() for x in n.split(",",1)]; return p[1],p[0]
    w=n.split(); return (w[0]," ".join(w[1:])) if len(w)>=2 else ("",n)

def save_json(records, dfrom, dto):
    payload={"fetched_at":datetime.now(timezone.utc).isoformat(),"source":"Maricopa County Recorder",
             "date_range":{"from":dfrom,"to":dto},"total":len(records),
             "with_address":sum(1 for r in records if r.get("prop_address") or r.get("mail_address")),
             "records":records}
    for path in (DASHBOARD_JSON, DATA_JSON):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str))
        log.info(f"Saved {len(records)} -> {path}")

def export_csv(records, dto):
    out = REPO_ROOT/"data"/f"ghl_export_{dto.replace('-','')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields=["First Name","Last Name","Mailing Address","Mailing City","Mailing State","Mailing Zip",
            "Property Address","Property City","Property State","Property Zip",
            "Lead Type","Document Type","Date Filed","Document Number",
            "Amount/Debt Owed","Seller Score","Motivated Seller Flags","Source","Public Records URL"]
    with open(out,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for rec in records:
            fn,ln=_sn(rec.get("owner",""))
            w.writerow({"First Name":fn,"Last Name":ln,
                "Mailing Address":rec.get("mail_address",""),"Mailing City":rec.get("mail_city",""),
                "Mailing State":rec.get("mail_state","AZ"),"Mailing Zip":rec.get("mail_zip",""),
                "Property Address":rec.get("prop_address",""),"Property City":rec.get("prop_city",""),
                "Property State":rec.get("prop_state","AZ"),"Property Zip":rec.get("prop_zip",""),
                "Lead Type":rec.get("cat_label",""),"Document Type":rec.get("doc_type",""),
                "Date Filed":rec.get("filed",""),"Document Number":rec.get("doc_num",""),
                "Amount/Debt Owed":rec.get("amount",""),"Seller Score":rec.get("score",""),
                "Motivated Seller Flags":" | ".join(rec.get("flags",[])),"Source":"Maricopa County Recorder",
                "Public Records URL":rec.get("clerk_url","")})

def main():
    today=datetime.now().date()
    dto=today.strftime("%m/%d/%Y"); dfrom=(today-timedelta(days=7)).strftime("%m/%d/%Y")
    dto_iso=today.strftime("%Y-%m-%d"); dfrom_iso=(today-timedelta(days=7)).strftime("%Y-%m-%d")
    log.info("Maricopa Motivated Seller Scraper")
    raw=scrape_all(dfrom,dto)
    final=[]
    for rec in raw:
        try: s,fl=score(rec,raw); rec["score"]=s; rec["flags"]=fl; final.append(rec)
        except Exception as e: log.error(f"Score: {e}")
    final.sort(key=lambda r:r.get("score",0),reverse=True)
    save_json(final,dfrom_iso,dto_iso)
    export_csv(final,dto_iso)
    log.info(f"DONE. {len(final)} leads.")

if __name__=="__main__":
    main()
