"""
Maricopa County Motivated Seller Lead Scraper v3
Uses DIRECT GET URLs discovered from the recorder portal.
URL pattern: GetRecDataRecentPgDn.aspx?rec=0&bdt=MM%2FDD%2FYYYY&edt=...&cde=LP&max=100&res=True
"""
from __future__ import annotations
import csv, io, json, logging, re, sys, time, traceback, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

try:
    from dbfread import DBF
    HAS_DBFREAD = True
except ImportError:
    HAS_DBFREAD = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

RECORDER_BASE   = "https://legacy.recorder.maricopa.gov/recdocdata"
RECORDER_SEARCH = f"{RECORDER_BASE}/GetRecDataRecentPgDn.aspx"

LEAD_TYPES = {
    "LP":      ("LP",    "Lis Pendens"),
    "NOFC":    ("NOFC",  "Notice of Foreclosure"),
    "TAXDEED": ("TAXD",  "Tax Deed"),
    "JUD":     ("JUD",   "Judgment"),
    "LNFED":   ("FEDTL", "Federal Tax Lien"),
    "LNSTATE": ("STTL",  "State Tax Lien"),
    "LN":      ("LIEN",  "Lien"),
    "LNMECH":  ("MECH",  "Mechanic Lien"),
    "MEDLN":   ("MEDN",  "Medical Lien"),
    "PRO":     ("PROB",  "Probate"),
    "RELLP":   ("RLLP",  "Release Lis Pendens"),
}

DOC_CODE_MAP = {
    "LIS PEND": ("LP","Lis Pendens"), "NOFC": ("NOFC","Notice of Foreclosure"),
    "TAXD": ("TAXDEED","Tax Deed"), "TAX DEED": ("TAXDEED","Tax Deed"),
    "JUD": ("JUD","Judgment"), "JUDG": ("JUD","Judgment"),
    "FEDTL": ("LN","Federal Tax Lien"), "FED TAX": ("LN","Federal Tax Lien"),
    "STTL": ("LN","State Tax Lien"), "STATE TAX": ("LN","State Tax Lien"),
    "LIEN": ("LN","Lien"), "MECH": ("LN","Mechanic Lien"),
    "MEDN": ("LN","Medical Lien"), "PROB": ("PRO","Probate"),
    "RLLP": ("RELLP","Release Lis Pendens"), "HOA": ("LN","HOA Lien"),
}

RETRY_ATTEMPTS = 3
RETRY_DELAY    = 5
PAGE_SIZE      = 100

REPO_ROOT      = Path(__file__).parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON      = REPO_ROOT / "data"      / "records.json"
PARCEL_CACHE   = REPO_ROOT / "data"      / "parcel_cache.json"

ASSESSOR_DOWNLOAD_PAGE  = "https://mcassessor.maricopa.gov/downloads.php"
ASSESSOR_ZIP_CANDIDATES = [
    "https://mcassessor.maricopa.gov/downloads/parcel.zip",
    "https://mcassessor.maricopa.gov/downloads/Parcel.zip",
    "https://mcassessor.maricopa.gov/downloads/parcels.zip",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://legacy.recorder.maricopa.gov/recdocdata/",
}

def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get("https://legacy.recorder.maricopa.gov/recdocdata/", timeout=15)
        log.info("Session ready.")
    except Exception as e:
        log.warning(f"Session init: {e}")
    return s

def build_url(url_code, date_from, date_to, start=0):
    bdt = date_from.replace("/", "%2F")
    edt = date_to.replace("/", "%2F")
    return f"{RECORDER_SEARCH}?rec={start}&suf=&nm=&bdt={bdt}&edt={edt}&cde={url_code}&max={PAGE_SIZE}&res=True&doc1={url_code}"

def fetch_page(session, url):
    for i in range(1, RETRY_ATTEMPTS+1):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log.warning(f"Attempt {i}: {e}")
            if i < RETRY_ATTEMPTS: time.sleep(RETRY_DELAY)
    return None

def parse_results(soup, internal_code, cat_label):
    records = []
    table = next((t for t in soup.find_all("table") if "RECORDING NUMBER" in t.get_text(separator="|").upper()), None)
    if not table:
        return records
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 2: continue
        try:
            rec_num = cells[0].get_text(strip=True)
            rec_date = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            doc_raw  = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            link = cells[0].find("a")
            if link and link.get("href"):
                href = link["href"]
                doc_url = href if href.startswith("http") else RECORDER_BASE + "/" + href.lstrip("/")
            else:
                doc_url = f"{RECORDER_BASE}/GetRecordedDocData.aspx?rec={re.sub(r'[^0-9]','',rec_num)}"
            cat, label = next(((v,l) for k,(v,l) in DOC_CODE_MAP.items() if k in doc_raw.upper()), (internal_code, cat_label))
            if not rec_num: continue
            records.append({"doc_num":rec_num,"doc_type":doc_raw or internal_code,"cat":cat,"cat_label":label,
                "filed":_nd(rec_date),"owner":"","grantee":"","legal":"","amount":None,"clerk_url":doc_url,
                "prop_address":"","prop_city":"","prop_state":"AZ","prop_zip":"",
                "mail_address":"","mail_city":"","mail_state":"AZ","mail_zip":""})
        except Exception as e:
            log.debug(f"Row: {e}")
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
                elif ("consideration" in lbl or "amount" in lbl) and not r["amount"]: r["amount"] = _pa(val)
        if not r["owner"]:
            lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
            for i,line in enumerate(lines):
                nxt = lines[i+1] if i+1<len(lines) else ""
                ll = line.lower()
                if "grantor" in ll and not r["owner"]: r["owner"] = nxt
                elif "grantee" in ll and not r["grantee"]: r["grantee"] = nxt
                elif "legal" in ll and not r["legal"]: r["legal"] = nxt
    except Exception as e:
        log.debug(f"Detail {url}: {e}")
    return r

def scrape_type(session, int_code, url_code, label, dfrom, dto):
    all_r, start = [], 0
    while True:
        url = build_url(url_code, dfrom, dto, start)
        soup = fetch_page(session, url)
        if not soup: break
        batch = parse_results(soup, int_code, label)
        if not batch: break
        for rec in batch:
            try:
                rec.update(fetch_detail(session, rec["clerk_url"]))
                time.sleep(0.3)
            except Exception: pass
        all_r.extend(batch)
        log.info(f"    offset {start}: {len(batch)} records")
        if len(batch) >= PAGE_SIZE:
            start += PAGE_SIZE
            time.sleep(1)
        else:
            break
    return all_r

def scrape_all(dfrom, dto):
    session = make_session()
    all_r = []
    for code, (url_code, label) in LEAD_TYPES.items():
        log.info(f"Searching: {label} ({url_code})")
        try:
            batch = scrape_type(session, code, url_code, label, dfrom, dto)
            all_r.extend(batch)
            log.info(f"  → {len(batch)} records")
            time.sleep(2)
        except Exception as e:
            log.error(f"Failed {label}: {e}\n{traceback.format_exc()}")
    log.info(f"Total: {len(all_r)}")
    return all_r

class ParcelIndex:
    def __init__(self):
        self._by_owner = {}
        self._by_apn   = {}
        self.loaded    = False

    def load_from_dbf(self, path):
        count = 0
        try:
            for rec in DBF(str(path), ignore_missing_memofile=True, encoding="latin-1"):
                try: self._ingest(dict(rec)); count += 1
                except Exception: pass
        except Exception as e:
            log.error(f"DBF: {e}")
        log.info(f"Loaded {count:,} parcels.")
        self.loaded = count > 0

    def load_from_cache(self, path):
        try:
            for apn, rec in json.loads(Path(path).read_text()).items():
                self._by_apn[apn] = rec
                for k in self._keys(rec.get("owner","")): self._by_owner.setdefault(k,[]).append(rec)
            self.loaded = bool(self._by_apn)
            log.info(f"Cache: {len(self._by_apn):,} parcels.")
            return self.loaded
        except Exception: return False

    def save_cache(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self._by_apn, default=str))

    def _ingest(self, raw):
        def g(*keys):
            for k in keys:
                for v in (raw.get(k), raw.get(k.upper()), raw.get(k.lower())):
                    if v: return str(v).strip()
            return ""
        apn, owner = g("APN","PARCEL","PARCELNO"), g("OWNER","OWN1","OWNER1")
        rec = {"apn":apn,"owner":owner,"prop_addr":g("SITE_ADDR","SITEADDR"),
               "prop_city":g("SITE_CITY","SITECITY"),"prop_state":"AZ","prop_zip":g("SITE_ZIP","SITEZIP"),
               "mail_addr":g("ADDR_1","MAILADR1") or g("SITE_ADDR","SITEADDR"),
               "mail_city":g("CITY","MAILCITY") or g("SITE_CITY","SITECITY"),
               "mail_state":g("STATE","MAILSTATE") or "AZ","mail_zip":g("ZIP","MAILZIP") or g("SITE_ZIP","SITEZIP")}
        if apn: self._by_apn[apn] = rec
        if owner:
            for k in self._keys(owner): self._by_owner.setdefault(k,[]).append(rec)

    @staticmethod
    def _keys(owner):
        o = owner.strip().upper(); keys = [o]
        if "," in o:
            p = [x.strip() for x in o.split(",",1)]; keys += [f"{p[1]} {p[0]}", f"{p[0]} {p[1]}"]
        else:
            w = o.split()
            if len(w)>=2: keys += [f"{w[-1]}, {' '.join(w[:-1])}", f"{w[-1]} {' '.join(w[:-1])}"]
        return list(dict.fromkeys(keys))

    def lookup(self, owner):
        if not owner: return None
        for k in self._keys(owner.upper()):
            hits = self._by_owner.get(k)
            if hits: return hits[0]
        return None

def build_parcel_index():
    idx = ParcelIndex()
    if PARCEL_CACHE.exists():
        if (time.time()-PARCEL_CACHE.stat().st_mtime)/3600 < 24 and idx.load_from_cache(PARCEL_CACHE):
            return idx
    if not HAS_DBFREAD:
        log.warning("dbfread not installed.")
        return idx
    session = make_session()
    zip_url = None
    try:
        r = session.get(ASSESSOR_DOWNLOAD_PAGE, timeout=30)
        for a in BeautifulSoup(r.text,"lxml").find_all("a",href=True):
            if re.search(r"parcel.*\.zip",a["href"],re.I):
                h = a["href"]; zip_url = h if h.startswith("http") else "https://mcassessor.maricopa.gov/"+h.lstrip("/"); break
    except Exception: pass
    for url in ([zip_url] if zip_url else [])+ASSESSOR_ZIP_CANDIDATES:
        if not url: continue
        try:
            r = session.get(url, timeout=120); r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                dbfs = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
                if not dbfs: continue
                dbfs.sort(key=lambda n:(0 if "parcel" in n.lower() else 1))
                tmp = Path("/tmp/maricopa_parcel.dbf")
                with zf.open(dbfs[0]) as src, open(tmp,"wb") as dst: dst.write(src.read())
            idx.load_from_dbf(tmp)
            if idx.loaded: idx.save_cache(PARCEL_CACHE); return idx
        except Exception as e:
            log.warning(f"ZIP {url}: {e}")
    if PARCEL_CACHE.exists(): idx.load_from_cache(PARCEL_CACHE)
    return idx

def enrich(records, idx):
    n = 0
    for rec in records:
        hit = idx.lookup(rec.get("owner",""))
        if hit:
            rec.update({"prop_address":hit.get("prop_addr",""),"prop_city":hit.get("prop_city",""),
                "prop_state":hit.get("prop_state","AZ"),"prop_zip":hit.get("prop_zip",""),
                "mail_address":hit.get("mail_addr",""),"mail_city":hit.get("mail_city",""),
                "mail_state":hit.get("mail_state","AZ"),"mail_zip":hit.get("mail_zip","")})
            n += 1
    log.info(f"Enriched {n}/{len(records)}.")
    return records

def score(rec, all_r):
    flags, s = [], 30
    cat,lbl,amt,owner,filed = rec.get("cat",""),rec.get("cat_label","").lower(),rec.get("amount"),rec.get("owner",""),rec.get("filed","")
    if cat=="LP": flags.append("Lis pendens")
    if cat in("LP","NOFC"): flags.append("Pre-foreclosure")
    if cat=="JUD": flags.append("Judgment lien")
    if cat=="TAXDEED": flags.append("Tax lien")
    if cat=="PRO": flags.append("Probate / estate")
    if cat=="LN":
        if "tax" in lbl: flags.append("Tax lien")
        elif "mechanic" in lbl: flags.append("Mechanic lien")
        elif "hoa" in lbl: flags.append("HOA lien")
        elif "medical" in lbl: flags.append("Medicaid lien")
        else: flags.append("Lien")
    if owner and any(k in owner.upper() for k in ("LLC","INC","CORP","TRUST","LP ","LLP")): flags.append("LLC / corp owner")
    try:
        if (datetime.now().date()-datetime.strptime(filed,"%Y-%m-%d").date()).days<=7: flags.append("New this week"); s+=5
    except Exception: pass
    s += len(set(flags))*10
    cats = {r["cat"] for r in all_r if r.get("owner","").upper()==owner.upper() and owner}
    if "LP" in cats and "NOFC" in cats: s+=20
    if amt: s += 15 if amt>100000 else (10 if amt>50000 else 0)
    if rec.get("prop_address") or rec.get("mail_address"): s+=5
    return min(s,100), list(dict.fromkeys(flags))

def _nd(raw):
    for fmt in ("%m/%d/%Y","%Y-%m-%d","%m-%d-%Y","%m/%d/%y"):
        try: return datetime.strptime(raw.strip(),fmt).strftime("%Y-%m-%d")
        except Exception: pass
    return raw.strip()

def _pa(raw):
    c = re.sub(r"[^\d.]","",str(raw or ""))
    try: v=float(c); return v if v>0 else None
    except Exception: return None

def _sn(full):
    n = full.strip()
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
        log.info(f"Saved {len(records)} → {path}")

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
    log.info(f"CSV → {out}")

def main():
    today=datetime.now().date()
    dto=today.strftime("%m/%d/%Y"); dfrom=(today-timedelta(days=7)).strftime("%m/%d/%Y")
    dto_iso=today.strftime("%Y-%m-%d"); dfrom_iso=(today-timedelta(days=7)).strftime("%Y-%m-%d")
    log.info("="*60); log.info("Maricopa Motivated Seller Scraper v3"); log.info(f"Range: {dfrom} → {dto}"); log.info("="*60)
    log.info("Step 1/4: Parcel index…"); idx=build_parcel_index()
    log.info("Step 2/4: Scraping recorder…"); raw=scrape_all(dfrom,dto)
    log.info("Step 3/4: Enriching…")
    if idx.loaded: raw=enrich(raw,idx)
    log.info("Step 4/4: Scoring…")
    final=[]
    for rec in raw:
        try: s,fl=score(rec,raw); rec["score"]=s; rec["flags"]=fl; final.append(rec)
        except Exception as e: log.error(f"Score: {e}")
    final.sort(key=lambda r:r.get("score",0),reverse=True)
    save_json(final,dfrom_iso,dto_iso); export_csv(final,dto_iso)
    log.info("="*60); log.info(f"DONE. {len(final)} leads.")
    if final: log.info(f"Top: {final[0].get('owner')} | {final[0].get('cat_label')} | score {final[0].get('score')}")
    log.info("="*60)

if __name__=="__main__":
    main()
