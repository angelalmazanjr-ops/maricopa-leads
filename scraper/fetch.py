import csv, json, logging, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON = REPO_ROOT / "data" / "records.json"

# Public JSON API discovered from browser DevTools — no scraping needed
API_BASE = "https://publicapi.recorder.maricopa.gov/documents/search"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://recorder.maricopa.gov/",
    "Origin": "https://recorder.maricopa.gov",
    "Accept": "application/json, text/plain, */*",
}

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


def fetch_code(code, begin_date, end_date):
    """Fetch all records for a doc code from the public API, handling pagination."""
    records = []
    page = 1

    while True:
        params = {
            "businessNames": "",
            "firstNames": "",
            "lastNames": "",
            "middleNameIs": "",
            "documentCode": code,
            "beginDate": begin_date,   # YYYY-MM-DD
            "endDate": end_date,
            "pageSize": 20,
            "pageNumber": page,
            "maxResults": 500,
        }
        try:
            log.info(f"Code {code} page {page}: fetching...")
            resp = requests.get(API_BASE, params=params, headers=HEADERS, timeout=30)
            log.info(f"Code {code} page {page}: status {resp.status_code}, size {len(resp.text)}")
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"Code {code} page {page}: request failed — {e}")
            break

        # API returns {"searchResults": [...], "totalResults": N}
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("searchResults") or []
        else:
            items = []

        if not items:
            log.info(f"Code {code} page {page}: no items returned, stopping pagination")
            break

        cat_label = DOC_CODE_LABELS.get(code, code)
        for item in items:
            doc_num = str(item.get("recordingNumber", "")).strip()
            if not doc_num:
                continue
            suffix = item.get("recordingSuffix", "").strip()
            if suffix:
                doc_num = f"{doc_num}-{suffix}"

            records.append({
                "doc_num": doc_num,
                "doc_type": item.get("documentCode", "").strip(),
                "cat": code,
                "cat_label": cat_label,
                "filed": _nd(item.get("recordingDate", "")),
                "owner": item.get("names", "").strip(),
                "grantee": "",
                "legal": "",
                "amount": None,
                "clerk_url": f"https://recorder.maricopa.gov/recording/document-detail.html?doc={doc_num}",
                "prop_address": "",
                "prop_city": "",
                "prop_state": "AZ",
                "prop_zip": "",
                "mail_address": "",
                "mail_city": "",
                "mail_state": "AZ",
                "mail_zip": "",
            })

        log.info(f"Code {code} page {page}: got {len(items)} items (running total: {len(records)})")

        # Stop if we got fewer results than page size (last page)
        if len(items) < 20:
            break
        page += 1
        time.sleep(0.5)  # be polite

    return records


def scrape_all(begin_date, end_date):
    doc_codes = ["LP", "NS", "JG", "FL", "SL", "ML", "LN", "HL", "PJ", "TD"]
    all_results = []
    for code in doc_codes:
        results = fetch_code(code, begin_date, end_date)
        log.info(f"=== Code {code}: {len(results)} total records ===")
        all_results.extend(results)
        time.sleep(0.5)
    return all_results


def score(rec, all_r):
    s = 0
    flags = []
    amt = rec.get("amount")
    owner = rec.get("owner", "")
    filed = rec.get("filed", "")
    lbl = rec.get("cat_label", "").lower()

    if "lis pendens" in lbl:
        flags.append("Lis Pendens")
    elif "trustees sale" in lbl or "foreclosure" in lbl:
        flags.append("Foreclosure")
    elif "tax deed" in lbl:
        flags.append("Tax Deed")
    elif "judgment" in lbl:
        flags.append("Judgment")
    elif "federal tax" in lbl:
        flags.append("Federal Tax Lien")
    elif "state tax" in lbl:
        flags.append("State Tax Lien")
    elif "mechanic" in lbl:
        flags.append("Mechanic Lien")
    elif "medical" in lbl or "health" in lbl:
        flags.append("Medical Lien")
    elif "probate" in lbl:
        flags.append("Probate")

    if owner and any(k in owner.upper() for k in ("LLC", "INC", "CORP", "TRUST")):
        flags.append("LLC / corp owner")

    try:
        age = (datetime.now().date() - datetime.strptime(filed, "%Y-%m-%d").date()).days
        if age <= 7:
            flags.append("New this week")
            s += 5
    except Exception:
        pass

    s += len(set(flags)) * 10
    if amt:
        s += 15 if amt > 100000 else (10 if amt > 50000 else 0)
    if rec.get("prop_address") or rec.get("mail_address"):
        s += 5

    return min(s, 100), list(dict.fromkeys(flags))


def _nd(raw):
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return raw


def _pa(raw):
    c = re.sub(r"[^\d.]", "", str(raw or ""))
    try:
        v = float(c)
        return v if v > 0 else None
    except Exception:
        return None


def _sn(full):
    n = full.strip()
    if any(k in n.upper() for k in ("LLC", "INC", "CORP", "TRUST", "ESTATE")):
        return "", n
    if "," in n:
        p = [x.strip() for x in n.split(",", 1)]
        return p[1], p[0]
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
        log.info(f"Saved {len(records)} records -> {path}")


def export_csv(records, dto):
    out = REPO_ROOT / "data" / f"ghl_export_{dto.replace('-', '')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Source", "Public Records URL",
    ]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for rec in records:
            fn, ln = _sn(rec.get("owner", ""))
            w.writerow({
                "First Name": fn,
                "Last Name": ln,
                "Mailing Address": rec.get("mail_address", ""),
                "Mailing City": rec.get("mail_city", ""),
                "Mailing State": rec.get("mail_state", "AZ"),
                "Mailing Zip": rec.get("mail_zip", ""),
                "Property Address": rec.get("prop_address", ""),
                "Property City": rec.get("prop_city", ""),
                "Property State": rec.get("prop_state", "AZ"),
                "Property Zip": rec.get("prop_zip", ""),
                "Lead Type": rec.get("cat_label", ""),
                "Document Type": rec.get("doc_type", ""),
                "Date Filed": rec.get("filed", ""),
                "Document Number": rec.get("doc_num", ""),
                "Amount/Debt Owed": rec.get("amount", ""),
                "Seller Score": rec.get("score", ""),
                "Motivated Seller Flags": " | ".join(rec.get("flags", [])),
                "Source": "Maricopa County Recorder",
                "Public Records URL": rec.get("clerk_url", ""),
            })
    log.info(f"CSV exported -> {out}")


def main():
    today = datetime.now().date()
    dto_iso = today.strftime("%Y-%m-%d")
    dfrom_iso = (today - timedelta(days=7)).strftime("%Y-%m-%d")

    log.info("=== Maricopa Motivated Seller Scraper (Direct API) ===")
    log.info(f"Date range: {dfrom_iso} → {dto_iso}")

    raw = scrape_all(dfrom_iso, dto_iso)
    log.info(f"Total raw records: {len(raw)}")

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
    log.info(f"=== DONE: {len(final)} leads saved ===")


if __name__ == "__main__":
    main()
