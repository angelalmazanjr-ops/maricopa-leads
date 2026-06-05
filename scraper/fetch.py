import csv, concurrent.futures, json, logging, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON = REPO_ROOT / "data" / "records.json"

SEARCH_API  = "https://publicapi.recorder.maricopa.gov/documents/search"
DETAIL_API  = "https://publicapi.recorder.maricopa.gov/documents/{}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://recorder.maricopa.gov/",
    "Origin": "https://recorder.maricopa.gov",
    "Accept": "application/json, text/plain, */*",
}

# Maps the API query code → doc_type code the dashboard expects
DOC_CODE_MAPPING = {
    "LP": "LP",       # Lis Pendens
    "NS": "NOFC",     # Notice of Trustees Sale → NOFC
    "JG": "JUD",      # Judgment
    "FL": "LNFED",    # Federal Tax Lien
    "SL": "SL",       # State Lien
    "ML": "LNMECH",   # Mechanic Lien
    "LN": "LN",       # Liens
    "HL": "MEDLN",    # Medical/Hospital Lien
    "PJ": "PRO",      # Probate
    "TD": "TAXDEED",  # Tax Deed
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


# ── Search API ─────────────────────────────────────────────────────

def fetch_code(code, begin_date, end_date):
    """Fetch all records for a doc code, handling pagination."""
    records = []
    page = 1
    doc_type = DOC_CODE_MAPPING.get(code, code)
    cat_label = DOC_CODE_LABELS.get(code, code)

    while True:
        params = {
            "businessNames": "",
            "firstNames": "",
            "lastNames": "",
            "middleNameIs": "",
            "documentCode": code,
            "beginDate": begin_date,
            "endDate": end_date,
            "pageSize": 20,
            "pageNumber": page,
            "maxResults": 500,
        }
        try:
            resp = requests.get(SEARCH_API, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"Code {code} page {page}: request failed — {e}")
            break

        items = data.get("searchResults") if isinstance(data, dict) else data
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
                "doc_type": doc_type,       # dashboard-compatible code
                "cat": code,
                "cat_label": cat_label,
                "filed": _nd(item.get("recordingDate", "")),
                "owner": "",               # filled in by enrich_names()
                "grantee": "",
                "amount": None,
                "clerk_url": f"https://recorder.maricopa.gov/recording/document-search-results.html?recordingNumber={doc_num}",
                "prop_address": "",
                "prop_city": "",
                "prop_state": "AZ",
                "prop_zip": "",
                "mail_address": "",
                "mail_city": "",
                "mail_state": "AZ",
                "mail_zip": "",
            })

        log.info(f"Code {code} page {page}: +{len(items)} (total {len(records)})")
        if len(items) < 20:
            break
        page += 1
        time.sleep(0.3)

    return records


def scrape_all(begin_date, end_date):
    doc_codes = ["LP", "NS", "JG", "FL", "SL", "ML", "LN", "HL", "PJ", "TD"]
    all_results = []
    for code in doc_codes:
        results = fetch_code(code, begin_date, end_date)
        log.info(f"=== Code {code}: {len(results)} records ===")
        all_results.extend(results)
        time.sleep(0.3)
    return all_results


# ── Detail API (names) ─────────────────────────────────────────────

def fetch_detail(doc_num):
    """Return owner names for a single recording number."""
    try:
        resp = requests.get(
            DETAIL_API.format(doc_num),
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            names = data.get("names", [])
            if isinstance(names, list):
                return [n.strip() for n in names if n.strip()]
            if isinstance(names, str) and names.strip():
                return [names.strip()]
    except Exception:
        pass
    return []


def enrich_names(records, workers=8):
    """Fetch owner/grantee names for all records using a thread pool."""
    log.info(f"Enriching names for {len(records)} records ({workers} workers)...")

    def worker(rec):
        names = fetch_detail(rec["doc_num"])
        if names:
            rec["owner"] = names[0]
            rec["grantee"] = names[1] if len(names) > 1 else ""
        return rec

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(worker, records))

    named = sum(1 for r in records if r.get("owner"))
    log.info(f"Names filled: {named}/{len(records)}")
    return records


# ── Scoring ────────────────────────────────────────────────────────

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


# ── Helpers ────────────────────────────────────────────────────────

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


# ── Save / Export ──────────────────────────────────────────────────

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


# ── Main ───────────────────────────────────────────────────────────

def main():
    today     = datetime.now().date()
    dto_iso   = today.strftime("%Y-%m-%d")
    dfrom_iso = (today - timedelta(days=7)).strftime("%Y-%m-%d")

    log.info("=== Maricopa Motivated Seller Scraper ===")
    log.info(f"Date range: {dfrom_iso} → {dto_iso}")

    # 1. Scrape search results
    raw = scrape_all(dfrom_iso, dto_iso)
    log.info(f"Raw records: {len(raw)}")

    # 2. Enrich with owner names from detail API
    enrich_names(raw)

    # 3. Score + sort
    final = []
    for rec in raw:
        try:
            s, fl = score(rec, raw)
            rec["score"] = s; rec["flags"] = fl
            final.append(rec)
        except Exception as e:
            log.error(f"Score error: {e}")

    final.sort(key=lambda r: r.get("score", 0), reverse=True)

    # 4. Save
    save_json(final, dfrom_iso, dto_iso)
    export_csv(final, dto_iso)
    log.info(f"=== DONE: {len(final)} leads ===")


if __name__ == "__main__":
    main()
