import csv, json, logging, os, re, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

RECORDER_BASE = "https://legacy.recorder.maricopa.gov/recdocdata/"
RECORDER_SEARCH = f"{RECORDER_BASE}GetRecDataRecentPgDn.aspx"
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


def make_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    driver = webdriver.Chrome(options=options)
    return driver


def build_url(code, dfrom, dto, start=0):
    b = dfrom.replace("/", "%2F")
    e = dto.replace("/", "%2F")
    return f"{RECORDER_SEARCH}?rec={start}&suf=&nm=&bdt={b}&edt={e}&cde={code}&max=20&res=True&doc1={code}&doc2=&doc3=&doc4=&doc5="


def fetch_page(driver, url):
    for i in range(3):
        try:
            driver.get(url)
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(8)
            html = driver.page_source
            log.info(f"Response length: {len(html)}")
            return BeautifulSoup(html, "lxml")
        except Exception as e:
            log.warning(f"Attempt {i+1}: {e}")
            time.sleep(5)
    return None


def parse_page(soup, code, label):
    records = []
    log.info(f"Tables found: {len(soup.find_all('table'))}, text sample: {soup.get_text()[:300]}")
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


def fetch_detail(driver, url):
    r = {"owner":"","grantee":"","legal":"","amount":None}
    try:
        soup = fetch_page(driver, url)
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
    driver = make_driver()
    all_r = []
    for code, (url_code, label) in LEAD_TYPES.items():
        log.info(f"Searching: {label}")
        try:
            # Visit the search form and submit it
            driver.get(RECORDER_BASE)
            time.sleep(3)
            
            # Fill in the form
            driver.find_element(By.NAME, "RecordDateFrom").clear()
            driver.find_element(By.NAME, "RecordDateFrom").send_keys(dfrom)
            driver.find_element(By.NAME, "RecordDateTo").clear()
            driver.find_element(By.NAME, "RecordDateTo").send_keys(dto)
            
            # Select document type
            from selenium.webdriver.support.ui import Select
            sel = Select(driver.find_element(By.NAME, "DocTypeCode"))
            sel.select_by_value(url_code)
            
            # Submit
            driver.find_element(By.CSS_SELECTOR, "input[type=submit]").click()
            time.sleep(8)
            
            soup = BeautifulSoup(driver.page_source, "lxml")
            batch = parse_page(soup, code, label)
            if batch:
                for rec in batch:
                    try: rec.update(fetch_detail(driver, rec["clerk_url"])); time.sleep(0.3)
                    except Exception: pass
                all_r.extend(batch)
                log.info(f"  {len(batch)} records")
        except Exception as e:
            log.error(f"Error on {label}: {e}")
    driver.quit()
    return all_r


def score(rec, all_r):
    s = 0; flags = []
    amt = rec.get("amount")
    owner = rec.get("owner","")
    filed = rec.get("filed","")
    lbl = rec.get("cat_label","").lower()
    if "lis pendens" in lbl: flags.append("Lis Pendens")
    elif "foreclosure" in lbl: flags.append("Foreclosure")
    elif "tax deed" in lbl: flags.append("Tax Deed")
    elif "judgment" in lbl: flags.append("Judgment")
    elif "federal tax" in lbl: flags.append("Federal Tax Lien")
    elif "state tax" in lbl: flags.append("State Tax Lien")
    elif "mechanic" in lbl: flags.append("Mechanic Lien")
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
    w=n.split(); return (" ".join(w[1:])," ".join(w[:1])) if len(w)>=2 else ("",n)


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
    dto=f"{today.month}/{today.day}/{today.year}"
    dfrom=f"{(today-timedelta(days=7)).month}/{(today-timedelta(days=7)).day}/{(today-timedelta(days=7)).year}"
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

