import csv, json, logging, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON = REPO_ROOT / "data" / "records.json"

DOC_CODES = ["LP", "NS", "JG", "FL", "SL", "ML", "LN", "HL", "PJ", "TD"]

DOC_CODE_LABELS = {
    "LP": "Lis Pendens",
    "NS": "Notice of Trustees Sale",
    "JG": "Judgment",
    "FL": "Federal Tax Lien",
    "SL": "State Tax Lien",
    "ML": "Mechanic Lien",
    "LN": "Liens",
    "HL": "Medical Lien",
    "PJ": "Probate",
    "TD": "Tax Deed",
}


def make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)


def scrape_all(dfrom, dto):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = make_driver()
    all_results = []
    base = "https://recorder.maricopa.gov/recording/document-search-results.html"

    for code in DOC_CODES:
        label = DOC_CODE_LABELS.get(code, code)
        url = (
            f"{base}?lastNames=&firstNames=&middleNameIs="
            f"&documentTypeSelector=code&documentCode={code}"
            f"&beginDate={dfrom}&endDate={dto}"
        )
        try:
            driver.get(url)
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#table-content tr"))
                )
            except Exception:
                log.info(f"Code {code}: no results (timeout waiting for table)")
                continue

            time.sleep(2)
            rows = driver.find_elements(By.CSS_SELECTOR, "#table-content tr")
            log.info(f"Code {code}: {len(rows)} rows found")

            for row in rows:
                try:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) < 3:
                        continue
                    num = cells[0].text.strip()
                    date = cells[1].text.strip()
                    doc_type = cells[2].text.strip()
                    try:
                        link_el = cells[0].find_element(By.TAG_NAME, "a")
                        clerk_url = link_el.get_attribute("href") or ""
                    except Exception:
                        clerk_url = ""
                    if not num:
                        continue
                    all_results.append({
                        "doc_num": num,
                        "doc_type": doc_type,
                        "cat": code,
                        "cat_label": label,
                        "filed": _nd(date),
                        "owner": "",
                        "grantee": "",
                        "amount": None,
                        "clerk_url": clerk_url,
                        "prop_address": "",
                        "prop_city": "",
                        "prop_state": "AZ",
                        "prop_zip": "",
                        "mail_address": "",
                        "mail_city": "",
                        "mail_state": "AZ",
                        "mail_zip": "",
                    })
                except Exception as e:
                    log.debug(f"Row error: {e}")
                    continue

        except Exception as e:
            log.error(f"Code {code}: {e}")
            continue

    driver.quit()
    log.info(f"Total raw records: {len(all_results)}")
    return all_results


def score(rec, all_r):
    s = 0
    flags = []
    amt   = rec.get("amount")
    owner = rec.get("owner", "")
    filed = rec.get("filed", "")
    lbl   = rec.get("cat_label", "").lower()

    if "lis pendens" in lbl:      flags.append("Lis Pendens")
    elif "trustees sale" in lbl:  flags.append("Foreclosure")
    elif "tax deed" in lbl:       flags.append("Tax Deed")
    elif "judgment" in lbl:       flags.append("Judgment")
    elif "federal tax" in lbl:    flags.append("Federal Tax Lien")
    elif "state tax" in lbl:      flags.append("State Tax Lien")
    elif "mechanic" in lbl:       flags.append("Mechanic Lien")
    elif "medical" in lbl:        flags.append("Medical Lien")
    elif "probate" in lbl:        flags.append("Probate")

    if owner and any(k in owner.upper() for k in ("LLC","INC","CORP","TRUST")):
        flags.append("LLC / corp owner")

    try:
        age = (datetime.now().date() - datetime.strptime(filed, "%Y-%m-%d").date()).days
        if age <= 7:
            flags.append("New this week"); s += 5
    except Exception:
        pass

    s += len(set(flags)) * 10
    if amt:
        s += 15 if amt > 100_000 else (10 if amt > 50_000 else 0)
    if rec.get("prop_address") or rec.get("mail_address"):
        s += 5

    return min(s, 100), list(dict.fromkeys(flags))


def _nd(raw):
    raw = str(raw).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%-m-%-d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return raw


def _pa(raw):
    c = re.sub(r"[^\d.]", "", str(raw or ""))
    try:
        v = float(c); return v if v > 0 else None
    except Exception:
        return None


def _sn(full):
    n = full.strip()
    if any(k in n.upper() for k in ("LLC","INC","CORP","TRUST","ESTATE","ASSOC","HOA")):
        return "", n
    if "," in n:
        p = [x.strip() for x in n.split(",", 1)]; return p[1], p[0]
    w = n.split()
    return (" ".join(w[1:]), " ".join(w[:1])) if len(w) >= 2 else ("", n)


def save_json(records, dfrom, dto):
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "Maricopa County Recorder",
        "date_range": {"from": dfrom, "to": dto},
        "total": len(records),
        "with_address": sum(1 for r in records if r.get("prop_address") or r.get("mail_address")),
        "records": records,
    }
    for path in (DASHBOARD_JSON, DATA_JSON):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str))
        log.info(f"Saved {len(records)} -> {path}")


def export_csv(records, dto):
    out = REPO_ROOT / "data" / f"ghl_export_{dto.replace('-','')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "First Name","Last Name",
        "Mailing Address","Mailing City","Mailing State","Mailing Zip",
        "Property Address","Property City","Property State","Property Zip",
        "Lead Type","Document Type","Date Filed","Document Number",
        "Amount/Debt Owed","Seller Score","Motivated Seller Flags",
        "Source","Public Records URL",
    ]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for rec in records:
            fn, ln = _sn(rec.get("owner",""))
            w.writerow({
                "First Name": fn, "Last Name": ln,
                "Mailing Address": rec.get("mail_address",""),
                "Mailing City": rec.get("mail_city",""),
                "Mailing State": rec.get("mail_state","AZ"),
                "Mailing Zip": rec.get("mail_zip",""),
                "Property Address": rec.get("prop_address",""),
                "Property City": rec.get("prop_city",""),
                "Property State": rec.get("prop_state","AZ"),
                "Property Zip": rec.get("prop_zip",""),
                "Lead Type": rec.get("cat_label",""),
                "Document Type": rec.get("doc_type",""),
                "Date Filed": rec.get("filed",""),
                "Document Number": rec.get("doc_num",""),
                "Amount/Debt Owed": rec.get("amount",""),
                "Seller Score": rec.get("score",""),
                "Motivated Seller Flags": " | ".join(rec.get("flags",[])),
                "Source": "Maricopa County Recorder",
                "Public Records URL": rec.get("clerk_url",""),
            })
    log.info(f"CSV exported -> {out}")


def main():
    today     = datetime.now().date()
    dto_iso   = today.strftime("%Y-%m-%d")
    dfrom_iso = (today - timedelta(days=7)).strftime("%Y-%m-%d")

    log.info("=== Maricopa Motivated Seller Scraper ===")
    log.info(f"Date range: {dfrom_iso} -> {dto_iso}")

    raw = scrape_all(dfrom_iso, dto_iso)

    final = []
    for rec in raw:
        try:
            s, fl = score(rec, raw)
            rec["score"] = s
            rec["flags"] = fl
            final.append(rec)
        except Exception as e:
            log.error(f"Score error: {e}")

    final.sort(key=lambda r: r.get("score", 0), reverse=True)

    save_json(final, dfrom_iso, dto_iso)
    export_csv(final, dto_iso)
    log.info(f"=== DONE: {len(final)} leads ===")


if __name__ == "__main__":
    main()
