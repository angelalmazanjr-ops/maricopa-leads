"""
Maricopa County Motivated Seller Lead Scraper
=============================================
Scrapes the Maricopa County Clerk of Court public records portal for
distressed-property document types, enriches each record with parcel/
address data from the Maricopa County Assessor bulk download, scores
each lead, and writes output to dashboard/records.json and data/records.json.

Author : Production-ready automated scraper
Python : 3.10+
"""

from __future__ import annotations

import asyncio
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

# ---------------------------------------------------------------------------
# Optional imports – Playwright and dbfread are installed in CI; gracefully
# handle missing deps so unit-tests can import the module without them.
# ---------------------------------------------------------------------------
try:
    from playwright.async_api import async_playwright, Page, Browser
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    logging.warning("playwright not installed – clerk scraping will be skipped.")

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
CLERK_BASE = "https://www.clerkofcourt.maricopa.gov"
CLERK_RECORDS_URL = f"{CLERK_BASE}/records"

# Maricopa County Assessor bulk parcel download
# The assessor publishes a ZIP of DBF files; the current known landing page:
ASSESSOR_DOWNLOAD_PAGE = "https://mcassessor.maricopa.gov/downloads.php"
# Fallback direct URL patterns (the assessor rotates the exact filename).
ASSESSOR_ZIP_CANDIDATES = [
    "https://mcassessor.maricopa.gov/downloads/parcel.zip",
    "https://mcassessor.maricopa.gov/downloads/Parcel.zip",
    "https://mcassessor.maricopa.gov/downloads/parcels.zip",
]

# Lead types we care about  (code -> human label)
LEAD_TYPES: dict[str, str] = {
    "LP":       "Lis Pendens",
    "NOFC":     "Notice of Foreclosure",
    "TAXDEED":  "Tax Deed",
    "JUD":      "Judgment",
    "CCJ":      "Certified Judgment",
    "DRJUD":    "Domestic Judgment",
    "LNCORPTX": "Corp Tax Lien",
    "LNIRS":    "IRS Lien",
    "LNFED":    "Federal Lien",
    "LN":       "Lien",
    "LNMECH":   "Mechanic Lien",
    "LNHOA":    "HOA Lien",
    "MEDLN":    "Medicaid Lien",
    "PRO":      "Probate",
    "NOC":      "Notice of Commencement",
    "RELLP":    "Release Lis Pendens",
}

# Document-type → canonical category abbreviation used on the clerk portal
DOC_TYPE_CATEGORIES: dict[str, str] = {
    "LP":       "LP",
    "NOFC":     "NOFC",
    "TAXDEED":  "TAXDEED",
    "JUD":      "JUD",
    "CCJ":      "JUD",
    "DRJUD":    "JUD",
    "LNCORPTX": "LN",
    "LNIRS":    "LN",
    "LNFED":    "LN",
    "LN":       "LN",
    "LNMECH":   "LN",
    "LNHOA":    "LN",
    "MEDLN":    "LN",
    "PRO":      "PRO",
    "NOC":      "NOC",
    "RELLP":    "RELLP",
}

RETRY_ATTEMPTS = 3
RETRY_DELAY = 5  # seconds

# Paths (relative to repo root; GitHub Actions runs from repo root)
REPO_ROOT = Path(__file__).parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON = REPO_ROOT / "data" / "records.json"
PARCEL_CACHE = REPO_ROOT / "data" / "parcel_cache.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

def retry(attempts: int = RETRY_ATTEMPTS, delay: float = RETRY_DELAY):
    """Simple retry decorator."""
    def decorator(fn):
        import functools
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    log.warning(f"{fn.__name__} attempt {attempt}/{attempts} failed: {exc}")
                    if attempt < attempts:
                        time.sleep(delay)
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator


async def retry_async(coro_fn, *args, attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY, **kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            log.warning(f"{coro_fn.__name__} attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Parcel / Assessor helpers
# ---------------------------------------------------------------------------

class ParcelIndex:
    """In-memory lookup from owner name → parcel record."""

    def __init__(self):
        self._by_owner: dict[str, list[dict]] = {}
        self._by_apn: dict[str, dict] = {}
        self.loaded = False

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_from_dbf(self, dbf_path: str | Path) -> None:
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
            log.error(f"Failed to read DBF: {exc}")
        log.info(f"Loaded {count:,} parcel records from DBF.")
        self.loaded = count > 0

    def load_from_cache(self, cache_path: str | Path) -> bool:
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

    def save_cache(self, cache_path: str | Path) -> None:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps(self._by_apn, default=str))
        log.info(f"Parcel cache saved: {len(self._by_apn):,} records.")

    # ------------------------------------------------------------------
    # Ingestion helpers
    # ------------------------------------------------------------------

    def _ingest(self, raw: dict) -> None:
        def g(*keys: str) -> str:
            for k in keys:
                v = raw.get(k) or raw.get(k.upper()) or raw.get(k.lower())
                if v:
                    return str(v).strip()
            return ""

        apn    = g("APN", "PARCEL", "PARCELNO", "PARCEL_NUM")
        owner  = g("OWNER", "OWN1", "OWNER1", "OWNERNAME")
        saddr  = g("SITE_ADDR", "SITEADDR", "SITE_ADDRESS", "SADDR")
        scity  = g("SITE_CITY", "SITECITY")
        szip   = g("SITE_ZIP",  "SITEZIP")
        maddr  = g("ADDR_1", "MAILADR1", "MAIL_ADDR", "MAILADDR")
        mcity  = g("CITY", "MAILCITY", "MAIL_CITY")
        mstate = g("STATE", "MAILSTATE", "MAIL_STATE")
        mzip   = g("ZIP",  "MAILZIP",   "MAIL_ZIP")

        rec = {
            "apn":        apn,
            "owner":      owner,
            "prop_addr":  saddr,
            "prop_city":  scity,
            "prop_state": "AZ",
            "prop_zip":   szip,
            "mail_addr":  maddr or saddr,
            "mail_city":  mcity or scity,
            "mail_state": mstate or "AZ",
            "mail_zip":   mzip or szip,
        }

        if apn:
            self._by_apn[apn] = rec
        if owner:
            for key in self._owner_keys(owner):
                self._by_owner.setdefault(key, []).append(rec)

    @staticmethod
    def _owner_keys(owner: str) -> list[str]:
        """Return normalised lookup variants for an owner name."""
        o = owner.strip().upper()
        keys = [o]
        # "LAST, FIRST" → "FIRST LAST" and "LAST FIRST"
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

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def lookup(self, owner_name: str) -> dict | None:
        if not owner_name:
            return None
        for key in self._owner_keys(owner_name.upper()):
            hits = self._by_owner.get(key)
            if hits:
                return hits[0]
        return None


# ---------------------------------------------------------------------------
# Assessor bulk download
# ---------------------------------------------------------------------------

@retry()
def _fetch_assessor_zip_url(session: requests.Session) -> str | None:
    """
    Try to discover the DBF ZIP URL from the assessor downloads page.
    Falls back to known candidate URLs.
    """
    try:
        resp = session.get(ASSESSOR_DOWNLOAD_PAGE, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"parcel.*\.zip", href, re.I):
                if href.startswith("http"):
                    return href
                return f"https://mcassessor.maricopa.gov/{href.lstrip('/')}"
    except Exception as exc:
        log.warning(f"Could not parse assessor downloads page: {exc}")
    return None


def download_parcel_dbf(parcel_index: ParcelIndex) -> bool:
    """Download assessor ZIP, extract first .dbf, load into parcel_index."""
    if not HAS_DBFREAD:
        log.warning("dbfread unavailable – skipping parcel download.")
        return False

    session = requests.Session()
    session.headers.update(HEADERS)

    zip_url = _fetch_assessor_zip_url(session)
    candidates = ([zip_url] if zip_url else []) + ASSESSOR_ZIP_CANDIDATES

    for url in candidates:
        if not url:
            continue
        log.info(f"Trying assessor ZIP: {url}")
        try:
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    resp = session.get(url, timeout=120, stream=True)
                    resp.raise_for_status()
                    break
                except Exception as exc:
                    if attempt == RETRY_ATTEMPTS:
                        raise
                    log.warning(f"Download attempt {attempt} failed: {exc}")
                    time.sleep(RETRY_DELAY)

            zdata = io.BytesIO(resp.content)
            with zipfile.ZipFile(zdata) as zf:
                dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
                if not dbf_names:
                    log.warning("No .dbf files found in ZIP.")
                    continue
                # Prefer files with "parcel" in name
                dbf_names.sort(key=lambda n: (0 if "parcel" in n.lower() else 1, n))
                chosen = dbf_names[0]
                log.info(f"Extracting {chosen} from ZIP.")
                tmp_path = Path("/tmp/maricopa_parcel.dbf")
                with zf.open(chosen) as src, open(tmp_path, "wb") as dst:
                    dst.write(src.read())

            parcel_index.load_from_dbf(tmp_path)
            if parcel_index.loaded:
                parcel_index.save_cache(PARCEL_CACHE)
                return True
        except Exception as exc:
            log.warning(f"Failed with {url}: {exc}")

    log.error("All assessor download attempts failed.")
    return False


def build_parcel_index() -> ParcelIndex:
    """Return a ParcelIndex, using cache if fresh enough (< 24 h)."""
    idx = ParcelIndex()

    # Try cache first
    if PARCEL_CACHE.exists():
        age_h = (time.time() - PARCEL_CACHE.stat().st_mtime) / 3600
        if age_h < 24:
            log.info(f"Parcel cache is {age_h:.1f}h old – using it.")
            if idx.load_from_cache(PARCEL_CACHE):
                return idx

    # Download fresh data
    download_parcel_dbf(idx)

    # Fall back to stale cache
    if not idx.loaded and PARCEL_CACHE.exists():
        log.warning("Using stale parcel cache as fallback.")
        idx.load_from_cache(PARCEL_CACHE)

    return idx


# ---------------------------------------------------------------------------
# Clerk of Court scraper (Playwright)
# ---------------------------------------------------------------------------

async def scrape_clerk_portal(date_from: str, date_to: str) -> list[dict]:
    """
    Scrape the Maricopa County Clerk of Court public records search.

    The portal at https://www.clerkofcourt.maricopa.gov/records uses a
    JavaScript-heavy interface. We use Playwright to interact with the form,
    select each document type, and collect results.

    Returns a flat list of raw record dicts.
    """
    if not HAS_PLAYWRIGHT:
        log.error("Playwright not available – returning empty records.")
        return []

    records: list[dict] = []

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        page: Page = await context.new_page()
        page.set_default_timeout(60_000)

        try:
            log.info("Navigating to clerk records portal…")
            await page.goto(CLERK_RECORDS_URL, wait_until="networkidle")

            # Discover what the search form looks like
            content = await page.content()
            soup = BeautifulSoup(content, "lxml")

            # The clerk portal may have multiple sub-pages. We try two known
            # entry points: a direct search form and the "Official Records" tab.
            await _navigate_to_official_records(page)

            for doc_code, doc_label in LEAD_TYPES.items():
                log.info(f"Searching doc type: {doc_code} ({doc_label})")
                try:
                    batch = await retry_async(
                        _search_doc_type,
                        page, doc_code, doc_label, date_from, date_to,
                        attempts=RETRY_ATTEMPTS,
                    )
                    records.extend(batch)
                    log.info(f"  → {len(batch)} records for {doc_code}")
                except Exception as exc:
                    log.error(f"Failed to scrape {doc_code}: {exc}\n{traceback.format_exc()}")

        finally:
            await browser.close()

    log.info(f"Clerk scrape complete. Total raw records: {len(records)}")
    return records


async def _navigate_to_official_records(page: Page) -> None:
    """
    Navigate within the clerk portal to the Official Records / document search.
    Handles multiple possible portal layouts.
    """
    try:
        # Try clicking "Official Records" link/tab if present
        locator = page.locator("a, button, li").filter(has_text=re.compile(r"official records", re.I))
        if await locator.count() > 0:
            await locator.first.click()
            await page.wait_for_load_state("networkidle")
            log.info("Clicked 'Official Records' tab.")
            return
    except Exception:
        pass

    # Try direct URL patterns for the search sub-page
    search_paths = [
        "/records/search",
        "/records/official-records",
        "/official-records",
        "/recorder/search",
    ]
    for path in search_paths:
        try:
            url = CLERK_BASE + path
            resp = await page.goto(url, wait_until="networkidle", timeout=20_000)
            if resp and resp.status < 400:
                log.info(f"Navigated to search page: {url}")
                return
        except Exception:
            pass

    log.warning("Could not navigate to a specific search page; using current page.")


async def _search_doc_type(
    page: Page,
    doc_code: str,
    doc_label: str,
    date_from: str,
    date_to: str,
) -> list[dict]:
    """
    Fill the search form for one document type and collect all result pages.
    Handles both standard form submissions and __doPostBack AJAX patterns.
    """
    records: list[dict] = []

    # ------------------------------------------------------------------
    # Fill the search form
    # ------------------------------------------------------------------
    await _fill_date_range(page, date_from, date_to)
    await _fill_doc_type(page, doc_code)

    # Submit
    await _submit_search(page)
    await page.wait_for_load_state("networkidle")

    # ------------------------------------------------------------------
    # Paginate results
    # ------------------------------------------------------------------
    page_num = 1
    while True:
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        batch = _parse_results_table(soup, doc_code, doc_label)
        records.extend(batch)
        log.debug(f"    Page {page_num}: {len(batch)} rows")

        if not batch:
            break  # no results or error

        # Try to go to next page
        next_btn = await _find_next_button(page)
        if not next_btn:
            break
        try:
            await next_btn.click()
            await page.wait_for_load_state("networkidle")
            page_num += 1
        except Exception:
            break

    return records


async def _fill_date_range(page: Page, date_from: str, date_to: str) -> None:
    """Fill date range fields in whatever form is present."""
    date_selectors = [
        ("input[name*='StartDate'], input[id*='StartDate'], input[placeholder*='Start']", date_from),
        ("input[name*='EndDate'],   input[id*='EndDate'],   input[placeholder*='End']",   date_to),
        ("input[name*='FromDate'],  input[id*='FromDate'],  input[placeholder*='From']",  date_from),
        ("input[name*='ToDate'],    input[id*='ToDate'],    input[placeholder*='To']",    date_to),
        ("input[name*='dateFrom'],  input[id*='dateFrom']",                               date_from),
        ("input[name*='dateTo'],    input[id*='dateTo']",                                 date_to),
    ]
    for selector, value in date_selectors:
        for sel in [s.strip() for s in selector.split(",")]:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.triple_click()
                    await el.fill(value)
                    break
            except Exception:
                pass


async def _fill_doc_type(page: Page, doc_code: str) -> None:
    """Select or type the document type code."""
    # Try <select> dropdown first
    select_selectors = [
        "select[name*='DocType']",
        "select[id*='DocType']",
        "select[name*='doctype']",
        "select[name*='type']",
        "select[id*='type']",
    ]
    for sel in select_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.select_option(value=doc_code)
                return
        except Exception:
            pass

    # Try text input
    input_selectors = [
        "input[name*='DocType']",
        "input[id*='DocType']",
        "input[name*='doctype']",
    ]
    for sel in input_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.triple_click()
                await el.fill(doc_code)
                return
        except Exception:
            pass


async def _submit_search(page: Page) -> None:
    """Click the search/submit button."""
    submit_selectors = [
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Search')",
        "input[value='Search']",
        "a:has-text('Search')",
    ]
    for sel in submit_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click()
                return
        except Exception:
            pass
    # Last resort: press Enter
    await page.keyboard.press("Enter")


async def _find_next_button(page: Page):
    """Return the locator for a 'Next page' control, or None."""
    selectors = [
        "a:has-text('Next')",
        "a[rel='next']",
        "input[value='Next']",
        "button:has-text('Next')",
        "a.next",
        "li.next > a",
        "span.next > a",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible() and await el.is_enabled():
                return el
        except Exception:
            pass
    return None


def _parse_results_table(soup: BeautifulSoup, doc_code: str, doc_label: str) -> list[dict]:
    """
    Parse results HTML. Maricopa's clerk portal renders a table of records.
    Handles multiple possible column orderings.
    """
    records: list[dict] = []

    # Find the results table
    tables = soup.find_all("table")
    result_table = None
    for t in tables:
        text = t.get_text(separator=" ").lower()
        if any(k in text for k in ("doc number", "document", "grantor", "filed")):
            result_table = t
            break

    if not result_table:
        return records

    rows = result_table.find_all("tr")
    if not rows:
        return records

    # Detect header row
    headers: list[str] = []
    header_row = rows[0]
    for cell in header_row.find_all(["th", "td"]):
        headers.append(cell.get_text(strip=True).lower())

    def col(row_cells: list, *names: str) -> str:
        for name in names:
            for i, h in enumerate(headers):
                if name in h and i < len(row_cells):
                    return row_cells[i].get_text(strip=True)
        return ""

    def col_link(row_cells: list, *names: str) -> str:
        for name in names:
            for i, h in enumerate(headers):
                if name in h and i < len(row_cells):
                    a = row_cells[i].find("a")
                    if a and a.get("href"):
                        href = a["href"]
                        return href if href.startswith("http") else CLERK_BASE + href
        return ""

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        try:
            doc_num   = col(cells, "doc", "number", "instrument")
            filed     = col(cells, "date", "filed", "record")
            grantor   = col(cells, "grantor", "owner", "from")
            grantee   = col(cells, "grantee", "to")
            legal     = col(cells, "legal", "description")
            amount    = col(cells, "amount", "debt", "value")
            clerk_url = col_link(cells, "doc", "number", "view")

            # Build direct URL fallback
            if not clerk_url and doc_num:
                clerk_url = (
                    f"{CLERK_RECORDS_URL}?docNum={doc_num.replace(' ', '')}"
                )

            if not doc_num and not grantor:
                continue  # skip empty rows

            records.append({
                "doc_num":   doc_num,
                "doc_type":  doc_code,
                "filed":     _normalise_date(filed),
                "owner":     grantor,
                "grantee":   grantee,
                "legal":     legal,
                "amount":    _parse_amount(amount),
                "clerk_url": clerk_url,
                # Parcel fields to be filled by enrichment step
                "prop_address": "",
                "prop_city":    "",
                "prop_state":   "AZ",
                "prop_zip":     "",
                "mail_address": "",
                "mail_city":    "",
                "mail_state":   "",
                "mail_zip":     "",
            })
        except Exception:
            pass  # never crash on a bad row

    return records


# ---------------------------------------------------------------------------
# Alternative static scraping (fallback / supplement)
# ---------------------------------------------------------------------------

def scrape_clerk_static(date_from: str, date_to: str) -> list[dict]:
    """
    Fallback scraper using requests + BeautifulSoup for doc types that may be
    accessible via direct GET requests on the clerk portal.
    """
    records: list[dict] = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for doc_code, doc_label in LEAD_TYPES.items():
        try:
            batch = _scrape_doc_type_static(session, doc_code, doc_label, date_from, date_to)
            records.extend(batch)
        except Exception as exc:
            log.error(f"Static scrape failed for {doc_code}: {exc}")

    return records


def _scrape_doc_type_static(
    session: requests.Session,
    doc_code: str,
    doc_label: str,
    date_from: str,
    date_to: str,
) -> list[dict]:
    records: list[dict] = []

    # Common search endpoint patterns for ASP.NET clerk portals
    base_urls = [
        f"{CLERK_BASE}/records/search",
        f"{CLERK_BASE}/recorder/search",
        f"{CLERK_BASE}/official-records/search",
    ]

    params = {
        "DocType":   doc_code,
        "StartDate": date_from,
        "EndDate":   date_to,
        "PageSize":  "100",
    }

    for base_url in base_urls:
        try:
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    resp = session.get(base_url, params=params, timeout=30)
                    if resp.status_code == 200:
                        break
                except Exception:
                    if attempt == RETRY_ATTEMPTS:
                        raise
                    time.sleep(RETRY_DELAY)

            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            batch = _parse_results_table(soup, doc_code, doc_label)
            if batch:
                records.extend(batch)
                # Handle pagination
                page_num = 2
                while True:
                    params["PageNumber"] = str(page_num)
                    r2 = session.get(base_url, params=params, timeout=30)
                    s2 = BeautifulSoup(r2.text, "lxml")
                    b2 = _parse_results_table(s2, doc_code, doc_label)
                    if not b2:
                        break
                    records.extend(b2)
                    page_num += 1
                break  # successful base_url found
        except Exception as exc:
            log.debug(f"Static {base_url} {doc_code}: {exc}")

    return records


# ---------------------------------------------------------------------------
# Data normalisation helpers
# ---------------------------------------------------------------------------

def _normalise_date(raw: str) -> str:
    """Parse various date formats to YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y", "%B %d, %Y"):
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
        return float(cleaned)
    except ValueError:
        return None


def _split_name(full_name: str) -> tuple[str, str]:
    """Naively split an owner name into first/last for GHL export."""
    name = full_name.strip()
    # If it's a company/LLC leave it as last name, no first name
    if any(kw in name.upper() for kw in ("LLC", "INC", "CORP", "TRUST", "ESTATE", "LP", "LLP")):
        return "", name

    # "LAST, FIRST" format
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return parts[1], parts[0]

    words = name.split()
    if len(words) == 1:
        return "", words[0]
    if len(words) == 2:
        return words[0], words[1]
    # 3+ words: first word = first name, rest = last name
    return words[0], " ".join(words[1:])


# ---------------------------------------------------------------------------
# Enrichment  (parcel lookup)
# ---------------------------------------------------------------------------

def enrich_records(records: list[dict], parcel_index: ParcelIndex) -> list[dict]:
    """Add property/mailing address from parcel index."""
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
# Seller scoring
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
    """
    Compute motivated-seller score (0-100) and flag list.

    Scoring:
      Base:           30
      Per flag:      +10
      LP + NOFC combo:  +20
      Amount > $100k:   +15
      Amount > $50k:    +10 (not cumulative with > $100k)
      New this week:    +5
      Has address:      +5
    """
    flags: list[str] = []
    score = 30

    doc_type = rec.get("doc_type", "")
    amount   = rec.get("amount")
    owner    = rec.get("owner", "")
    filed    = rec.get("filed", "")

    # Flag: Lis Pendens
    if doc_type == "LP":
        flags.append("Lis pendens")

    # Flag: Pre-foreclosure
    if doc_type in ("NOFC", "LP"):
        flags.append("Pre-foreclosure")

    # Flag: Judgment lien
    if doc_type in ("JUD", "CCJ", "DRJUD"):
        flags.append("Judgment lien")

    # Flag: Tax lien
    if doc_type in ("TAXDEED", "LNCORPTX", "LNIRS", "LNFED"):
        flags.append("Tax lien")

    # Flag: Mechanic lien
    if doc_type == "LNMECH":
        flags.append("Mechanic lien")

    # Flag: HOA lien
    if doc_type == "LNHOA":
        flags.append("HOA lien")

    # Flag: Probate / estate
    if doc_type == "PRO":
        flags.append("Probate / estate")

    # Flag: LLC / corp owner
    if owner and any(kw in owner.upper() for kw in ("LLC", "INC", "CORP", "TRUST", "LP ", "LLP")):
        flags.append("LLC / corp owner")

    # Flag: New this week
    if _is_new_this_week(filed):
        flags.append("New this week")
        score += 5

    # +10 per flag (unique)
    score += len(set(flags)) * 10

    # LP + foreclosure combo bonus
    owner_doc_types = {
        r["doc_type"] for r in all_records
        if r.get("owner", "").upper() == owner.upper() and owner
    }
    if "LP" in owner_doc_types and "NOFC" in owner_doc_types:
        score += 20

    # Amount bonuses
    if amount is not None:
        if amount > 100_000:
            score += 15
        elif amount > 50_000:
            score += 10

    # Has address
    if rec.get("prop_address") or rec.get("mail_address"):
        score += 5

    return min(score, 100), list(dict.fromkeys(flags))  # dedupe flags, cap at 100


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def build_output_record(raw: dict, flags: list[str], score: int) -> dict:
    doc_type = raw.get("doc_type", "")
    return {
        "doc_num":      raw.get("doc_num", ""),
        "doc_type":     doc_type,
        "filed":        raw.get("filed", ""),
        "cat":          DOC_TYPE_CATEGORIES.get(doc_type, doc_type),
        "cat_label":    LEAD_TYPES.get(doc_type, doc_type),
        "owner":        raw.get("owner", ""),
        "grantee":      raw.get("grantee", ""),
        "amount":       raw.get("amount"),
        "legal":        raw.get("legal", ""),
        "prop_address": raw.get("prop_address", ""),
        "prop_city":    raw.get("prop_city", ""),
        "prop_state":   raw.get("prop_state", "AZ"),
        "prop_zip":     raw.get("prop_zip", ""),
        "mail_address": raw.get("mail_address", ""),
        "mail_city":    raw.get("mail_city", ""),
        "mail_state":   raw.get("mail_state", "AZ"),
        "mail_zip":     raw.get("mail_zip", ""),
        "clerk_url":    raw.get("clerk_url", ""),
        "flags":        flags,
        "score":        score,
    }


def save_json(records: list[dict], date_from: str, date_to: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with_address = sum(
        1 for r in records if r.get("prop_address") or r.get("mail_address")
    )
    payload = {
        "fetched_at":  now,
        "source":      "Maricopa County Clerk of Court",
        "date_range":  {"from": date_from, "to": date_to},
        "total":       len(records),
        "with_address": with_address,
        "records":     records,
    }
    for path in (DASHBOARD_JSON, DATA_JSON):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str))
        log.info(f"Saved {len(records)} records → {path}")


def export_ghl_csv(records: list[dict], date_from: str, date_to: str) -> Path:
    """
    Export records to a CSV compatible with Go High Level (GHL) import.
    File is saved next to data/records.json as ghl_export_YYYYMMDD.csv.
    """
    out_dir = REPO_ROOT / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"ghl_export_{date_to.replace('-', '')}.csv"
    out_path = out_dir / filename

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
                "First Name":            first,
                "Last Name":             last,
                "Mailing Address":       rec.get("mail_address", ""),
                "Mailing City":          rec.get("mail_city", ""),
                "Mailing State":         rec.get("mail_state", "AZ"),
                "Mailing Zip":           rec.get("mail_zip", ""),
                "Property Address":      rec.get("prop_address", ""),
                "Property City":         rec.get("prop_city", ""),
                "Property State":        rec.get("prop_state", "AZ"),
                "Property Zip":          rec.get("prop_zip", ""),
                "Lead Type":             rec.get("cat_label", ""),
                "Document Type":         rec.get("doc_type", ""),
                "Date Filed":            rec.get("filed", ""),
                "Document Number":       rec.get("doc_num", ""),
                "Amount/Debt Owed":      rec.get("amount", ""),
                "Seller Score":          rec.get("score", ""),
                "Motivated Seller Flags": " | ".join(rec.get("flags", [])),
                "Source":                "Maricopa County Clerk of Court",
                "Public Records URL":    rec.get("clerk_url", ""),
            })

    log.info(f"GHL CSV exported → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def main() -> None:
    today     = datetime.now().date()
    date_to   = today.strftime("%m/%d/%Y")
    date_from = (today - timedelta(days=7)).strftime("%m/%d/%Y")
    date_from_iso = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    date_to_iso   = today.strftime("%Y-%m-%d")

    log.info("=" * 60)
    log.info("Maricopa County Motivated Seller Lead Scraper")
    log.info(f"Date range: {date_from} → {date_to}")
    log.info("=" * 60)

    # ------------------------------------------------------------------ #
    # 1. Build parcel index
    # ------------------------------------------------------------------ #
    log.info("Step 1/4: Building parcel index…")
    parcel_index = build_parcel_index()

    # ------------------------------------------------------------------ #
    # 2. Scrape clerk portal (Playwright primary, static fallback)
    # ------------------------------------------------------------------ #
    log.info("Step 2/4: Scraping Clerk of Court portal…")
    raw_records: list[dict] = []

    if HAS_PLAYWRIGHT:
        try:
            raw_records = await scrape_clerk_portal(date_from, date_to)
        except Exception as exc:
            log.error(f"Playwright scrape failed: {exc}\n{traceback.format_exc()}")

    # If Playwright returned nothing, try static scrape
    if not raw_records:
        log.info("Falling back to static HTTP scrape…")
        raw_records = scrape_clerk_static(date_from, date_to)

    log.info(f"Raw records collected: {len(raw_records)}")

    # ------------------------------------------------------------------ #
    # 3. Enrich with parcel data
    # ------------------------------------------------------------------ #
    log.info("Step 3/4: Enriching with parcel data…")
    if parcel_index.loaded:
        raw_records = enrich_records(raw_records, parcel_index)

    # ------------------------------------------------------------------ #
    # 4. Score, finalise, and save
    # ------------------------------------------------------------------ #
    log.info("Step 4/4: Scoring records and saving output…")
    final_records: list[dict] = []
    for rec in raw_records:
        try:
            score, flags = score_record(rec, raw_records)
            out = build_output_record(rec, flags, score)
            final_records.append(out)
        except Exception as exc:
            log.error(f"Failed to score record {rec.get('doc_num')}: {exc}")

    # Sort by score descending
    final_records.sort(key=lambda r: r.get("score", 0), reverse=True)

    save_json(final_records, date_from_iso, date_to_iso)
    export_ghl_csv(final_records, date_from_iso, date_to_iso)

    log.info("=" * 60)
    log.info(f"DONE. {len(final_records)} leads saved.")
    log.info(
        f"  With address : "
        f"{sum(1 for r in final_records if r.get('prop_address') or r.get('mail_address'))}"
    )
    if final_records:
        log.info(f"  Top score    : {final_records[0]['score']}")
        log.info(f"  Top lead     : {final_records[0]['owner']} ({final_records[0]['doc_type']})")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
