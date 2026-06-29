import csv, concurrent.futures, json, logging, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON = REPO_ROOT / "data" / "records.json"

SEARCH_API = "https://publicapi.recorder.maricopa.gov/documents/search"
DETAIL_API = "https://publicapi.recorder.maricopa.gov/documents/{}"
ASSESSOR_GEO = "https://gis.mcassessor.maricopa.gov/arcgis/rest/services/Parcels/MapServer/0/query"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://recorder.maricopa.gov/",
    "Origin": "https://recorder.maricopa.gov",
    "Accept": "application/json, text/plain, */*",
}

DOC_CODE_MAPPING = {
    "LP": "LP", "NS": "NOFC", "JG": "JUD",
    "FL": "LNFED", "SL": "SL", "ML": "LNMECH",
    "LN": "LN", "HL": "MEDLN", "PJ": "PRO",
    "TD": "TAXDEED",
}

DOC_CODE_LABELS = {
    "LP": "Lis Pendens", "NS": "Notice of Trustees Sale",
    "JG": "Judgment", "FL": "Federal Tax Lien",
    "SL": "State Tax Lien", "ML": "Mechanic Lien",
    "LN": "Liens", "HL": "Medical Lien",
    "PJ": "Probate", "TD": "Tax Deed",
}

def fetch_code(code, begin_date, end_date):
    records, page = [], 1
    doc_type = DOC_CODE_MAPPING.get(code, code)
    cat_label = DOC_CODE_LABELS.get(code, code)
    while True:
        params = {
            "businessNames": "", "firstNames": "", "lastNames": "",
            "middleNameIs": "", "documentCode": code,
            "beginDate": begin_date, "endDate": end_date,
            "pageSize": 20, "pageNumber": page, "maxResults": 500,
        }
        try:
            resp = requests.get(SEARCH_API, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"Code {code} page {page}: {e}")
            break
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = None
            for key in ("searchResults", "results", "documents", "items", "data", "records"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
            if items is None:
                if "recordingNumber" in data:
                    items = [data]
                else:
                    items = []
        else:
            items = []
        if not items:
            break
        for item in items:
            doc_num = str(item.get("recordingNumber", "")).strip()
            if not doc_num:
                continue
            suffix = item.get("recordingSuffix", "").strip()
            if suffix:
                doc_num = f"{doc_num}-{suffix}"
            records.append({
                "doc_num": doc_num,
                "doc_type": doc_type,
                "cat": code,
                "cat_label": cat_label,
                "filed": _nd(item.get("recordingDate", "")),
                "owner": "",
                "grantee": "",
                "amount": None,
                "clerk_url": f"https://recorder.maricopa.gov/recording/document-search-results.html?recordingNumber={doc_num}",
                "prop_address": "", "prop_city": "", "prop_state": "AZ", "prop_zip": "",
                "mail_address": "", "mail_city": "", "mail_state": "AZ", "mail_zip": "",
            })
        log.info(f"Code {code} page {page}: +{len(items)} (total {len(records)})")
        if len(items) < 20:
            break
        page += 1
        time.sleep(0.3)
    return records


def scrape_all(begin_date, end_date):
    all_results = []
    for code in ["LP", "NS", "JG", "FL", "SL", "ML", "LN", "HL", "PJ", "TD"]:
        results = fetch_code(code, begin_date, end_date)
        log.info(f"=== Code {code}: {len(results)} records ===")
        all_results.extend(results)
        time.sleep(0.3)
    return all_results


def fetch_detail(doc_num):
    result = {"owner": "", "grantee": "", "amount": None, "parcel": ""}
    try:
        resp = requests.get(DETAIL_API.format(doc_num), headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return result
        data = resp.json()
        names = data.get("names", [])
        if isinstance(names, list) and names:
            result["owner"] = str(names[0]).strip()
            result["grantee"] = str(names[1]).strip() if len(names) > 1 else ""
        elif isinstance(names, str) and names.strip():
            result["owner"] = names.strip()
        if not result["owner"]:
            for k in ("grantor", "grantorName", "grantorNames"):
                v = data.get(k)
                if v: result["owner"] = str(v).strip(); break
        if not result["grantee"]:
            for k in ("grantee", "granteeName", "granteeNames"):
                v = data.get(k)
                if v: result["grantee"] = str(v).strip(); break
        for key in ("consideration", "considerationAmount", "amount",
                    "lienAmount", "debtAmount", "totalAmount", "balance"):
            val = data.get(key)
            if val is not None:
                amt = _pa(str(val))
                if amt:
                    result["amount"] = amt
                    break
        for key in ("parcelNumber", "apn", "assessorParcelNumber",
                    "taxParcelNumber", "parcel", "parcelNum"):
            val = data.get(key)
            if val and str(val).strip():
                result["parcel"] = re.sub(r"[^0-9A-Za-z]", "", str(val))
                break
    except Exception as e:
        log.debug(f"Detail {doc_num}: {e}")
    return result


def _probe_assessor():
    try:
        resp = requests.get(
            ASSESSOR_GEO,
            params={"where": "1=1", "outFields": "*", "resultRecordCount": 1, "f": "json"},
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=15,
        )
        d = resp.json()
        features = d.get("features", [])
        attrs = features[0]["attributes"] if features else {}
        log.info(f"GIS PROBE status={resp.status_code} field_names={list(attrs.keys())}")
        log.info(f"GIS PROBE sample_record={json.dumps(attrs, default=str)}")
    except Exception as e:
        log.warning(f"GIS PROBE failed: {e}")


def _parse_gis_json(data, label=""):
    if "error" in data:
        return {}
    features = data.get("features", [])
    if not features:
        return {}
    attrs = features[0].get("attributes", {})
    addr = str(attrs.get("PHYSICAL_ADDRESS") or "").strip()
    if not addr:
        num = str(attrs.get("PHYSICAL_STREET_NUM") or "").strip()
        dir_ = str(attrs.get("PHYSICAL_STREET_DIR") or "").strip()
        name = str(attrs.get("PHYSICAL_STREET_NAME") or "").strip()
        typ = str(attrs.get("PHYSICAL_STREET_TYPE") or "").strip()
        suf = str(attrs.get("PHYSICAL_STREET_SUFFIX") or "").strip()
        addr = " ".join(filter(None, [num, dir_, name, typ, suf])).strip()
    city = str(attrs.get("PHYSICAL_CITY") or "").strip()
    zipcode = str(attrs.get("PHYSICAL_ZIP") or "").strip().split(".")[0]
    mail_full = str(attrs.get("MAIL_ADDRESS") or "").strip()
    mail_addr = str(attrs.get("MAIL_ADDR1") or attrs.get("MAIL_ADDR") or "").strip()
    mail_addr2 = str(attrs.get("MAIL_ADDR2") or "").strip()
    if not mail_addr and mail_full:
        mail_addr = mail_full
    elif mail_addr2:
        mail_addr = f"{mail_addr} {mail_addr2}".strip()
    mail_city = str(attrs.get("MAIL_CITY") or "").strip()
    mail_state = str(attrs.get("MAIL_STATE") or "AZ").strip()
    mail_zip = str(attrs.get("MAIL_ZIP") or "").strip().split(".")[0]
    prop_value = None
    for vk in ("FCV_CUR", "SALE_PRICE", "LPV_CUR"):
        v = attrs.get(vk)
        if v and str(v).strip() not in ("", "0", "None", "null"):
            fv = _pa(str(v))
            if fv and fv > 0:
                prop_value = fv
                break
    result = {}
    if addr:
        result["prop_address"] = addr
        result["prop_city"] = city
        result["prop_zip"] = zipcode
    if mail_addr:
        result["mail_address"] = mail_addr
        result["mail_city"] = mail_city
        result["mail_state"] = mail_state
        result["mail_zip"] = mail_zip
    if prop_value:
        result["prop_value"] = prop_value
    return result


def fetch_assessor_by_apn(apn):
    if not apn or len(re.sub(r"[^0-9]", "", apn)) < 7:
        return {}
    digits = re.sub(r"[^0-9]", "", apn)
    apn_fmt = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}" if len(digits) >= 8 else apn
    try:
        resp = requests.get(
            ASSESSOR_GEO,
            params={"where": f"APN='{apn_fmt}'", "outFields": "*",
                    "resultRecordCount": 1, "f": "json"},
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=30,
        )
        if resp.status_code == 200:
            return _parse_gis_json(resp.json(), f"GIS-APN:{apn_fmt}")
    except Exception as e:
        log.warning(f"GIS APN {apn_fmt}: {e}")
    return {}


def fetch_assessor_by_name(name):
    name = (name or "").strip()
    if not name or len(name) < 5:
        return {}
    skip = ("LLC","INC","CORP","TRUST","BANK","MORTGAGE","LOAN","SERVICING",
            "FINANCIAL","FUND","INVESTMENT","PROP","REAL ESTATE","VENTURE",
            "HOMEOWNERS","ASSOCIATION","HOA","CREDIT UNION","FEDERAL")
    if any(k in name.upper() for k in skip):
        return {}
    words = [w for w in name.upper().split() if len(w) > 2 and w not in ("AND","THE","FOR","JR","SR","II","III")]
    if not words:
        return {}
    if len(words) >= 2:
        w1, w2 = words[0].replace("'","''"), words[1].replace("'","''")
        where = f"UPPER(OWNER_NAME) LIKE '%{w1}%' AND UPPER(OWNER_NAME) LIKE '%{w2}%'"
    else:
        w1 = words[0].replace("'","''")
        where = f"UPPER(OWNER_NAME) LIKE '%{w1}%'"
    try:
        resp = requests.get(
            ASSESSOR_GEO,
            params={"where": where, "outFields": "*", "resultRecordCount": 1, "f": "json"},
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=30,
        )
        if resp.status_code == 200:
            return _parse_gis_json(resp.json(), f"GIS-NAME:{name[:25]}")
    except Exception as e:
        log.warning(f"GIS name {name[:25]}: {e}")
    return {}


def enrich_names(records, workers=8):
    log.info(f"Enriching {len(records)} records ({workers} workers)...")
    def worker(rec):
        detail = fetch_detail(rec["doc_num"])
        rec["owner"] = detail["owner"]
        rec["grantee"] = detail["grantee"]
        if detail["amount"] is not None:
            rec["amount"] = detail["amount"]
        addr = {}
        if detail["parcel"]:
            addr = fetch_assessor_by_apn(detail["parcel"])
        if not addr:
            name = rec.get("grantee") or rec.get("owner", "")
            addr = fetch_assessor_by_name(name)
        if addr:
            rec.update(addr)
        if rec.get("amount") is None and addr.get("prop_value"):
            rec["amount"] = addr["prop_value"]
        return rec
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(worker, records))
    named = sum(1 for r in records if r.get("owner"))
    addressed = sum(1 for r in records if r.get("prop_address"))
    log.info(f"Enriched: {named} names | {addressed} addresses")
    return records


def score(rec, all_r):
    s, flags = 0, []
    amt = rec.get("amount")
    owner = rec.get("owner", "")
    filed = rec.get("filed", "")
    lbl = rec.get("cat_label", "").lower()
    if "trustees sale" in lbl or "foreclosure" in lbl:
        flags.append("Foreclosure"); s += 25
    elif "tax deed" in lbl:
        flags.append("Tax Deed"); s += 25
    elif "lis pendens" in lbl:
        flags.append("Lis Pendens"); s += 20
    elif "federal tax" in lbl:
        flags.append("Federal Tax Lien"); s += 20
    elif "judgment" in lbl:
        flags.append("Judgment"); s += 15
    elif "probate" in lbl:
        flags.append("Probate"); s += 15
    elif "state tax" in lbl:
        flags.append("State Tax Lien"); s += 15
    elif "mechanic" in lbl:
        flags.append("Mechanic Lien"); s += 10
    elif "medical" in lbl:
        flags.append("Medical Lien"); s += 5
    if owner and any(k in owner.upper() for k in ("LLC","INC","CORP","TRUST","ESTATE","LP ","LLP")):
        flags.append("LLC / corp owner"); s += 10
    try:
        age = (datetime.now().date() - datetime.strptime(filed, "%Y-%m-%d").date()).days
        if age <= 3:
            flags.append("New (last 3 days)"); s += 15
        elif age <= 7:
            flags.append("New this week"); s += 10
    except Exception:
        pass
    if amt:
        if amt >= 200_000:
            flags.append(f"High debt ${amt:,.0f}"); s += 20
        elif amt >= 100_000:
            flags.append(f"Debt ${amt:,.0f}"); s += 15
        elif amt >= 50_000:
            flags.append(f"Debt ${amt:,.0f}"); s += 10
        elif amt > 0:
            flags.append(f"Debt ${amt:,.0f}"); s += 5
    if rec.get("prop_address") or rec.get("mail_address"):
        flags.append("Address found"); s += 5
    return min(s, 100), list(dict.fromkeys(flags))


def _nd(raw):
    raw = str(raw).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try: return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except: pass
    return raw

def _pa(raw):
    c = re.sub(r"[^\d.]", "", str(raw or ""))
    try:
        v = float(c); return v if v > 0 else None
    except: return None

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
    log.info(f"CSV -> {out}")


def main():
    today = datetime.now().date()
    dto_iso = today.strftime("%Y-%m-%d")
    dfrom_iso = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    log.info("=== Maricopa Motivated Seller Scraper ===")
    log.info(f"Date range: {dfrom_iso} -> {dto_iso}")
    _probe_assessor()
    raw = scrape_all(dfrom_iso, dto_iso)
    log.info(f"Raw records: {len(raw)}")
    enrich_names(raw, workers=2)
    final = []
    for rec in raw:
        try:
            s, fl = score(rec, raw)
            rec["score"] = s; rec["flags"] = fl
            final.append(rec)
        except Exception as e:
            log.error(f"Score error: {e}")
    final.sort(key=lambda r: r.get("score", 0), reverse=True)
    save_json(final, dfrom_iso, dto_iso)
    export_csv(final, dto_iso)
    log.info(f"=== DONE: {len(final)} leads ===")


if __name__ == "__main__":
    main()
