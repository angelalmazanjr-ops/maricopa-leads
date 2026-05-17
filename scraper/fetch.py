"""
Maricopa County Motivated Seller Lead Scraper
=============================================
Scrapes the Maricopa County Recorder legacy search portal at
https://legacy.recorder.maricopa.gov/recdocdata/
using requests + BeautifulSoup (no Playwright needed - it's a plain HTML form).

Enriches records with parcel/address data from the Maricopa County Assessor,
scores each lead, and writes output to dashboard/records.json and data/records.json.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sys
import time
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

try:
    from dbfread import DBF
    HAS_DBFREAD = True
except ImportError:
    HAS_DBFREAD = False
    logging.warning("dbfread not installed – parcel enrichment will be skipped.")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECORDER_SEARCH_URL = "https://legacy.recorder.maricopa.gov/recdocdata/"

# These are the EXACT dropdown text values from the Maricopa Recorder portal
# mapped to our internal category labels
LEAD_TYPES: dict[str, str] = {
    "LIS PENDENS":                                    "Lis Pendens",
    "LIENS-GOVT/NON-GOVT & GENERAL LIENS":            "Lien",
    "MATERIAL MANS MECH LN":                          "Mechanic Lien",
    "MEDICAL LN-FOR MOSTMEDICAL/HOSP/CHIRO LIENTYPES":"Medicaid Lien",
    "FEDERAL TAX LIEN":                               "Federal Tax Lien",
    "STATE TAX LIEN":                                 "State Tax Lien",
    "TAX DEED":                                       "Tax Deed",
    "JUDGMENT-GENERAL TYPES INCLUDNG CIVIL":          "Judgment",
    "CHILD SUPPORT JUDGEMENT/LIEN":                   "Judgment",
    "PROBATE/USE WITH MOST PROBATE DOC TYPES":        "Probate",
    "PROBATE DEED OF ANY TYPE/USED TO TRANSFER PROP": "Probate",
    "NOTICE OF TRUSTEES SALE":                        "Notice of Foreclosure",
    "HOMEOWNERS ASSN CONTACT INFO":                   "HOA Lien",
    "ASSIGNMENT OF LIS PENDENS":                      "Lis Pendens",
    "MODIFIED LIS PENDENS":                           "Lis Pendens",
    "PARTIAL RELEASE OF A LIS PENDENS":               "Release Lis Pendens",
    "MARGINAL RELEASE OF LIS PENDENS":                "Release Lis Pendens",
    "AMENDMENT OF A FEDERAL TAX LIEN":                "Federal Tax Lien",
    "AMENDMENT OF A STATE TAX LIEN":                  "State Tax Lien",
    "NON GOVERNMENTAL LIEN":                          "Lien",
    "GOVERNMENTAL LIEN":                              "Lien",
    "INHERITANCE TAX LIEN":                           "Tax Lien",
    "ECONOMIC SECURITY COMMISSION LIEN":              "Lien",
    "RACKETEERING LIEN":                              "Lien",
    "RESTITUTION OR RACKETEERING LIEN":               "Lien",
}

# Internal category codes for scoring/display
DOC_LABEL_TO_CAT: dict[str, str] = {
    "Lis Pendens":          "LP",
    "Notice of Foreclosure":"NOFC",
    "Tax Deed":             "TAXDEED",
    "Judgment":             "JUD",
    "Federal Tax Lien":     "LN",
    "State Tax Lien":       "LN",
    "Tax Lien":             "LN",
    "Lien":                 "LN",
    "Mechanic Lien":        "LN",
    "HOA Lien":             "LN",
    "Medicaid Lien":        "LN",
    "Probate":              "PRO",
    "Release Lis Pendens":  "RELLP",
}

RETRY_ATTEMPTS = 3
RETRY_DELAY    = 5

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
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def retry_get(session, url, **kwargs):
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = session.get(url, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except Exception as exc:
            log.warning(f"GET {url} attempt {attempt} failed: {exc}")
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"All {RETRY_ATTEMPTS} attempts failed for {url}")


def retry_post(session, url, data, **kwargs):
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = session.post(url, data=data, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except Exception as exc:
            log.warning(f"POST {url} attempt {attempt} failed: {exc}")
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"All {RETRY_ATTEMPTS} attempts failed for {url}")


# ---------------------------------------------------------------------------
# Recorder scraper  (plain HTML form, no JS needed)
# ---------------------------------------------------------------------------

def get_form_fields(session: requests.Session) -> dict:
    """
    Load the recorder search page and extract any hidden form fields
    (e.g. ASP.NET __VIEWSTATE, __EVENTVALIDATION, etc.)
    """
    fields = {}
    try:
        r = retry_get(session, RECORDER_SEARCH_URL)
        soup = BeautifulSoup(r.text, "lxml")
        for inp in soup.find_all("input", type="hidden"):
            name = inp.get("name", "")
            val  = inp.get("value", "")
            if name:
                fields[name] = val
        log.info(f"Extracted {len(fields)} hidden form fields.")
    except Exception as exc:
        log.warning(f"Could not load form page: {exc}")
    return fields


def search_document_type(
    session: requests.Session,
    hidden_fields: dict,
    doc_title: str,
    date_from: str,
    date_to: str,
) -> list[dict]:
    """
    Submit the recorder search form for one document type and collect all pages.

    The form POSTs to the same URL with:
      - SearchType = "D" (by document code/title)
      - DocType    = the exact dropdown text
      - BeginDate  = mm/dd/yyyy
      - EndDate    = mm/dd/yyyy
    """
    records: list[dict] = []

    # Base POST payload - field names observed from the recorder form
    payload = {
        **hidden_fields,
        "SearchType":    "D",        # Search by Document type
        "DocType":       doc_title,  # Exact dropdown text
        "BeginDate":     date_from,
        "EndDate":       date_to,
        "LastName":      "",
        "FirstName":     "",
        "MI":            "",
        "BusName":       "",
        "RecordingYear": "",
        "RecordingNum":  "",
        "Suffix":        "",
        "Book":          "",
        "Page":          "",
        "cmdSearch":     "Search",
    }

    page_num = 1
    next_url = RECORDER_SEARCH_URL

    while True:
        try:
            if page_num == 1:
                r = retry_post(session, next_url, data=payload)
            else:
                # Subsequent pages are usually GET with a page param,
                # or another POST with updated hidden fields
                r = retry_post(session, next_url, data=payload)

            soup = BeautifulSoup(r.text, "lxml")
            batch = parse_results(soup, doc_title)
            records.extend(batch)
            log.debug(f"  {doc_title} page {page_num}: {len(batch)} rows")

            if not batch:
                break

            # Check for next page link
            next_link = find_next_page(soup)
            if not next_link:
                break

            # Build next page URL
            if next_link.startswith("http"):
                next_url = next_link
            else:
                next_url = "https://legacy.recorder.maricopa.gov" + next_link

            # Update hidden fields for next page
            for inp in soup.find_all("input", type="hidden"):
                name = inp.get("name", "")
                val  = inp.get("value", "")
                if name:
                    payload[name] = val

            page_num += 1
            time.sleep(1)  # be polite

        except Exception as exc:
            log.error(f"Error on page {page_num} for {doc_title}: {exc}")
            break

    return records


def find_next_page(soup: BeautifulSoup) -> str | None:
    """Find a 'Next' pagination link in the results page."""
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if text in ("next", "next >", "next»", ">", "»"):
            return a["href"]
    # Also check for input buttons that submit to next page
    for inp in soup.find_all("input", value=re.compile(r"next", re.I)):
        return None  # Would need JS - stop here
    return None


def parse_results(soup: BeautifulSoup, doc_title: str) -> list[dict]:
    """
    Parse the HTML results table from the recorder search.

    The Maricopa Recorder results table has columns like:
    Recording Date | Document Type | Grantor | Grantee | Legal | Amount | Doc Number
    """
    records: list[dict] = []
    cat_label = LEAD_TYPES.get(doc_title, doc_title)
    cat       = DOC_LABEL_TO_CAT.get(cat_label, "LN")

    # Find the results table - it usually has class "search-results" or similar
    tables = soup.find_all("table")
    result_table = None

    for t in tables:
        headers_text = t.get_text(separator="|").lower()
        if any(k in headers_text for k in ("grantor", "grantee", "recording", "document")):
            result_table = t
            break

    if not result_table:
        # Check for "no records found" message
        body = soup.get_text().lower()
        if "no record" in body or "no result" in body or "0 record" in body:
            log.debug(f"No records found for {doc_title}")
        return records

    rows = result_table.find_all("tr")
    if len(rows) < 2:
        return records

    # Parse header row
    headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]

    def col(cells, *names):
        for name in names:
            for i, h in enumerate(headers):
                if name in h and i < len(cells):
                    return cells[i].get_text(strip=True)
        return ""

    def col_href(cells, *names):
        for name in names:
            for i, h in enumerate(headers):
                if name in h and i < len(cells):
                    a = cells[i].find("a")
                    if a and a.get("href"):
                        href = a["href"]
                        if href.startswith("http"):
                            return href
                        return "https://legacy.recorder.maricopa.gov" + href
        return ""

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells or len(cells) < 2:
            continue
        try:
            # Try to get fields by header name
            filed     = col(cells, "date", "recorded", "recording date", "filed")
            doc_type  = col(cells, "document type", "doc type", "type")
            grantor   = col(cells, "grantor", "owner", "from name")
            grantee   = col(cells, "grantee", "to name")
            legal     = col(cells, "legal", "description", "legal description")
            amount    = col(cells, "amount", "consideration", "value")
            doc_num   = col(cells, "number", "doc number", "document number", "rec number")
            clerk_url = col_href(cells, "number", "doc", "view", "image")

            # Fallback: if headers didn't match, try positional (common layout)
            if not doc_num and not grantor:
                # Positional fallback based on typical recorder layout:
                # 0=RecNum, 1=Date, 2=DocType, 3=Grantor, 4=Grantee, 5=Legal, 6=Amount
                if len(cells) >= 4:
                    doc_num  = cells[0].get_text(strip=True)
                    filed    = cells[1].get_text(strip=True)
                    grantor  = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                    grantee  = cells[4].get_text(strip=True) if len(cells) > 4 else ""
                    legal    = cells[5].get_text(strip=True) if len(cells) > 5 else ""
                    amount   = cells[6].get_text(strip=True) if len(cells) > 6 else ""
                    a = cells[0].find("a")
                    if a and a.get("href"):
                        href = a["href"]
                        clerk_url = href if href.startswith("http") else \
                                    "https://legacy.recorder.maricopa.gov" + href

            if not doc_num and not grantor:
                continue

            # Build direct doc URL if we have a doc number
            if not clerk_url and doc_num:
                clean_num = re.sub(r"[^\d]", "", doc_num)
                if clean_num:
                    clerk_url = (
                        f"https://legacy.recorder.maricopa.gov/recdocdata/"
                        f"?recno={clean_num}"
                    )

            records.append({
                "doc_num":      doc_num.strip(),
                "doc_type":     doc_title,
                "cat":          cat,
                "cat_label":    cat_label,
                "filed":        _normalise_date(filed),
                "owner":        grantor.strip(),
                "grantee":      grantee.strip(),
                "legal":        legal.strip(),
                "amount":       _parse_amount(amount),
                "clerk_url":    clerk_url,
                "prop_address": "",
                "prop_city":    "",
                "prop_state":   "AZ",
                "prop_zip":     "",
                "mail_address": "",
                "mail_city":    "",
                "mail_state":   "AZ",
                "mail_zip":     "",
            })
        except Exception as exc:
            log.debug(f"Bad row: {exc}")

    return records


def scrape_all_types(date_from: str, date_to: str) -> list[dict]:
    """Main scrape loop across all document types."""
    session = make_session()
    hidden_fields = get_form_fields(session)
    all_records: list[dict] = []

    for doc_title, doc_label in LEAD_TYPES.items():
        log.info(f"Searching: {doc_title}")
        try:
            batch = search_document_type(
                session, hidden_fields, doc_title, date_from, date_to
            )
            all_records.extend(batch)
            log.info(f"  → {len(batch)} records")
            time.sleep(2)  # polite delay between searches
        except Exception as exc:
            log.error(f"Failed {doc_title}: {exc}")

    log.info(f"Total raw records: {len(all_records)}")
    return all_records


# ---------------------------------------------------------------------------
# Parcel / Assessor helpers
# ---------------------------------------------------------------------------

class ParcelIndex:
    def __init__(self):
        self._by_owner: dict[str, list[dict]] = {}
        self._by_apn:   dict[str, dict]       = {}
        self.loaded = False

    def load_from_dbf(self, dbf_path) -> None:
        log.info(f"Loading parcel DBF: {dbf_path}")
        count = 0
        try:
            table = DBF(str(dbf_path), ignore_missing_memofile=True, encoding="latin-1")
            for rec in table:
                try:
                    self._ingest(dict(rec))
                    count += 1
                except Exception:
                    pass
        except Exception as exc:
            log.error(f"DBF read failed: {exc}")
        log.info(f"Loaded {count:,} parcel records.")
        self.loaded = count > 0

    def load_from_cache(self, cache_path) -> bool:
        try:
            data = json.loads(Path(cache_path).read_text())
            for apn, rec in data.items():
                self._by_apn[apn] = rec
                for key in self._owner_keys(rec.get("owner", "")):
                    self._by_owner.setdefault(key, []).append(rec)
            self.loaded = bool(self._by_apn)
            log.info(f"Loaded {len(self._by_apn):,} parcels from cache.")
            return self.loaded
        except Exception:
            return False

    def save_cache(self, cache_path) -> None:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps(self._by_apn, default=str))
        log.info(f"Parcel cache saved: {len(self._by_apn):,} records.")

    def _ingest(self, raw: dict) -> None:
        def g(*keys):
            for k in keys:
                for variant in (k, k.upper(), k.lower()):
                    v = raw.get(variant)
                    if v:
                        return str(v).strip()
            return ""

        apn    = g("APN", "PARCEL", "PARCELNO")
        owner  = g("OWNER", "OWN1", "OWNER1", "OWNERNAME")
        saddr  = g("SITE_ADDR", "SITEADDR", "SITE_ADDRESS")
        scity  = g("SITE_CITY", "SITECITY")
        szip   = g("SITE_ZIP",  "SITEZIP")
        maddr  = g("ADDR_1", "MAILADR1", "MAIL_ADDR")
        mcity  = g("CITY", "MAILCITY", "MAIL_CITY")
        mstate = g("STATE", "MAILSTATE")
        mzip   = g("ZIP",  "MAILZIP")

        rec = {
            "apn": apn, "owner": owner,
            "prop_addr": saddr, "prop_city": scity,
            "prop_state": "AZ", "prop_zip": szip,
            "mail_addr": maddr or saddr,
            "mail_city": mcity or scity,
            "mail_state": mstate or "AZ",
            "mail_zip": mzip or szip,
        }
        if apn:
            self._by_apn[apn] = rec
        if owner:
            for key in self._owner_keys(owner):
                self._by_owner.setdefault(key, []).append(rec)

    @staticmethod
    def _owner_keys(owner: str) -> list[str]:
        o = owner.strip().upper()
        keys = [o]
        if "," in o:
            parts = [p.strip() for p in o.split(",", 1)]
            keys.append(f"{parts[1]} {parts[0]}")
            keys.append(f"{parts[0]} {parts[1]}")
        else:
            words = o.split()
            if len(words) >= 2:
                keys.append(f"{words[-1]}, {' '.join(words[:-1])}")
                keys.append(f"{words[-1]} {' '.join(words[:-1])}")
        return list(dict.fromkeys(keys))

    def lookup(self, owner_name: str) -> dict | None:
        if not owner_name:
            return None
        for key in self._owner_keys(owner_name.upper()):
            hits = self._by_owner.get(key)
            if hits:
                return hits[0]
        return None


def download_parcel_dbf(parcel_index: ParcelIndex) -> bool:
    if not HAS_DBFREAD:
        return False
    session = make_session()

    # Try to find ZIP URL from downloads page
    zip_url = None
    try:
        r = session.get(ASSESSOR_DOWNLOAD_PAGE, timeout=30)
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            if re.search(r"parcel.*\.zip", a["href"], re.I):
                href = a["href"]
                zip_url = href if href.startswith("http") else \
                          "https://mcassessor.maricopa.gov/" + href.lstrip("/")
                break
    except Exception as exc:
        log.warning(f"Could not parse assessor page: {exc}")

    candidates = ([zip_url] if zip_url else []) + ASSESSOR_ZIP_CANDIDATES

    for url in candidates:
        if not url:
            continue
        log.info(f"Trying assessor ZIP: {url}")
        try:
            r = session.get(url, timeout=120, stream=True)
            r.raise_for_status()
            zdata = io.BytesIO(r.content)
            with zipfile.ZipFile(zdata) as zf:
                dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
                if not dbf_names:
                    continue
                dbf_names.sort(key=lambda n: (0 if "parcel" in n.lower() else 1, n))
                tmp = Path("/tmp/maricopa_parcel.dbf")
                with zf.open(dbf_names[0]) as src, open(tmp, "wb") as dst:
                    dst.write(src.read())
            parcel_index.load_from_dbf(tmp)
            if parcel_index.loaded:
                parcel_index.save_cache(PARCEL_CACHE)
                return True
        except Exception as exc:
            log.warning(f"Failed {url}: {exc}")

    return False


def build_parcel_index() -> ParcelIndex:
    idx = ParcelIndex()
    if PARCEL_CACHE.exists():
        age_h = (time.time() - PARCEL_CACHE.stat().st_mtime) / 3600
        if age_h < 24:
            log.info(f"Using parcel cache ({age_h:.1f}h old).")
            if idx.load_from_cache(PARCEL_CACHE):
                return idx
    download_parcel_dbf(idx)
    if not idx.loaded and PARCEL_CACHE.exists():
        log.warning("Using stale parcel cache.")
        idx.load_from_cache(PARCEL_CACHE)
    return idx


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def enrich_records(records: list[dict], parcel_index: ParcelIndex) -> list[dict]:
    enriched = 0
    for rec in records:
        try:
            hit = parcel_index.lookup(rec.get("owner", ""))
            if hit:
                rec["prop_address"] = hit.get("prop_addr", "")
                rec["prop_city"]    = hit.get("prop_city", "")
                rec["prop_state"]   = hit.get("prop_state", "AZ")
                rec["prop_zip"]     = hit.get("prop_zip", "")
                rec["mail_address"] = hit.get("mail_addr", "")
                rec["mail_city"]    = hit.get("mail_city", "")
                rec["mail_state"]   = hit.get("mail_state", "AZ")
                rec["mail_zip"]     = hit.get("mail_zip", "")
                enriched += 1
        except Exception:
            pass
    log.info(f"Enriched {enriched}/{len(records)} records with parcel data.")
    return records


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _is_new_this_week(filed: str) -> bool:
    if not filed:
        return False
    try:
        d = datetime.strptime(filed, "%Y-%m-%d").date()
        return (datetime.now().date() - d).days <= 7
    except Exception:
        return False


def score_record(rec: dict, all_records: list[dict]) -> tuple[int, list[str]]:
    flags: list[str] = []
    score = 30

    cat       = rec.get("cat", "")
    cat_label = rec.get("cat_label", "")
    amount    = rec.get("amount")
    owner     = rec.get("owner", "")
    filed     = rec.get("filed", "")

    if cat == "LP":
        flags.append("Lis pendens")
    if cat in ("LP", "NOFC"):
        flags.append("Pre-foreclosure")
    if cat == "JUD":
        flags.append("Judgment lien")
    if cat == "LN":
        if "tax" in cat_label.lower():
            flags.append("Tax lien")
        elif "mechanic" in cat_label.lower() or "material" in cat_label.lower():
            flags.append("Mechanic lien")
        elif "hoa" in cat_label.lower() or "homeowner" in cat_label.lower():
            flags.append("HOA lien")
        elif "medicaid" in cat_label.lower() or "medical" in cat_label.lower():
            flags.append("Medicaid lien")
        else:
            flags.append("Lien")
    if cat == "TAXDEED":
        flags.append("Tax lien")
    if cat == "PRO":
        flags.append("Probate / estate")
    if owner and any(kw in owner.upper() for kw in ("LLC", "INC", "CORP", "TRUST", "LP ", "LLP")):
        flags.append("LLC / corp owner")
    if _is_new_this_week(filed):
        flags.append("New this week")
        score += 5

    score += len(set(flags)) * 10

    # LP + foreclosure combo for same owner
    owner_cats = {
        r["cat"] for r in all_records
        if r.get("owner", "").upper() == owner.upper() and owner
    }
    if "LP" in owner_cats and "NOFC" in owner_cats:
        score += 20

    if amount is not None:
        if amount > 100_000:
            score += 15
        elif amount > 50_000:
            score += 10

    if rec.get("prop_address") or rec.get("mail_address"):
        score += 5

    return min(score, 100), list(dict.fromkeys(flags))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_date(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y", "%B %d, %Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def _parse_amount(raw: str) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", raw)
    try:
        v = float(cleaned)
        return v if v > 0 else None
    except ValueError:
        return None


def _split_name(full_name: str) -> tuple[str, str]:
    name = full_name.strip()
    if any(kw in name.upper() for kw in ("LLC", "INC", "CORP", "TRUST", "ESTATE")):
        return "", name
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return parts[1], parts[0]
    words = name.split()
    if len(words) < 2:
        return "", name
    return words[0], " ".join(words[1:])


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_json(records: list[dict], date_from: str, date_to: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with_address = sum(1 for r in records if r.get("prop_address") or r.get("mail_address"))
    payload = {
        "fetched_at":   now,
        "source":       "Maricopa County Recorder",
        "date_range":   {"from": date_from, "to": date_to},
        "total":        len(records),
        "with_address": with_address,
        "records":      records,
    }
    for path in (DASHBOARD_JSON, DATA_JSON):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str))
        log.info(f"Saved {len(records)} records → {path}")


def export_ghl_csv(records: list[dict], date_to: str) -> Path:
    out_dir  = REPO_ROOT / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ghl_export_{date_to.replace('-', '')}.csv"

    fieldnames = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Source", "Public Records URL",
    ]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            first, last = _split_name(rec.get("owner", ""))
            writer.writerow({
                "First Name":             first,
                "Last Name":              last,
                "Mailing Address":        rec.get("mail_address", ""),
                "Mailing City":           rec.get("mail_city", ""),
                "Mailing State":          rec.get("mail_state", "AZ"),
                "Mailing Zip":            rec.get("mail_zip", ""),
                "Property Address":       rec.get("prop_address", ""),
                "Property City":          rec.get("prop_city", ""),
                "Property State":         rec.get("prop_state", "AZ"),
                "Property Zip":           rec.get("prop_zip", ""),
                "Lead Type":              rec.get("cat_label", ""),
                "Document Type":          rec.get("doc_type", ""),
                "Date Filed":             rec.get("filed", ""),
                "Document Number":        rec.get("doc_num", ""),
                "Amount/Debt Owed":       rec.get("amount", ""),
                "Seller Score":           rec.get("score", ""),
                "Motivated Seller Flags": " | ".join(rec.get("flags", [])),
                "Source":                 "Maricopa County Recorder",
                "Public Records URL":     rec.get("clerk_url", ""),
            })

    log.info(f"GHL CSV → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    today       = datetime.now().date()
    date_to     = today.strftime("%m/%d/%Y")
    date_from   = (today - timedelta(days=7)).strftime("%m/%d/%Y")
    date_to_iso = today.strftime("%Y-%m-%d")
    date_from_iso = (today - timedelta(days=7)).strftime("%Y-%m-%d")

    log.info("=" * 60)
    log.info("Maricopa County Motivated Seller Lead Scraper")
    log.info(f"Recorder portal: {RECORDER_SEARCH_URL}")
    log.info(f"Date range: {date_from} → {date_to}")
    log.info("=" * 60)

    # 1. Parcel index
    log.info("Step 1/4: Building parcel index…")
    parcel_index = build_parcel_index()

    # 2. Scrape recorder
    log.info("Step 2/4: Scraping Maricopa County Recorder…")
    raw_records = scrape_all_types(date_from, date_to)

    # 3. Enrich
    log.info("Step 3/4: Enriching with parcel data…")
    if parcel_index.loaded:
        raw_records = enrich_records(raw_records, parcel_index)

    # 4. Score + save
    log.info("Step 4/4: Scoring and saving…")
    final_records: list[dict] = []
    for rec in raw_records:
        try:
            score, flags = score_record(rec, raw_records)
            rec["score"] = score
            rec["flags"] = flags
            final_records.append(rec)
        except Exception as exc:
            log.error(f"Score failed for {rec.get('doc_num')}: {exc}")

    final_records.sort(key=lambda r: r.get("score", 0), reverse=True)

    save_json(final_records, date_from_iso, date_to_iso)
    export_ghl_csv(final_records, date_to_iso)

    log.info("=" * 60)
    log.info(f"DONE. {len(final_records)} leads.")
    log.info(f"  With address: {sum(1 for r in final_records if r.get('prop_address') or r.get('mail_address'))}")
    if final_records:
        log.info(f"  Top score:    {final_records[0]['score']}")
        log.info(f"  Top lead:     {final_records[0]['owner']} ({final_records[0]['cat_label']})")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
