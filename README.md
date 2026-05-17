# Maricopa County Motivated Seller Lead Scraper

Automated daily scraper for distressed-property public records in Maricopa County, AZ.  
Runs via GitHub Actions, publishes leads to a self-hosted GitHub Pages dashboard, and exports GHL-ready CSV.

---

## Lead Types Collected

| Code | Label |
|------|-------|
| LP | Lis Pendens |
| NOFC | Notice of Foreclosure |
| TAXDEED | Tax Deed |
| JUD / CCJ / DRJUD | Judgment / Certified Judgment / Domestic Judgment |
| LNCORPTX / LNIRS / LNFED | Corp Tax Lien / IRS Lien / Federal Lien |
| LN / LNMECH / LNHOA | Lien / Mechanic Lien / HOA Lien |
| MEDLN | Medicaid Lien |
| PRO | Probate |
| NOC | Notice of Commencement |
| RELLP | Release Lis Pendens |

---

## Seller Score (0–100)

| Condition | Points |
|-----------|--------|
| Base | 30 |
| Per flag | +10 |
| LP + NOFC combo (same owner) | +20 |
| Amount > $100k | +15 |
| Amount > $50k | +10 |
| Filed this week | +5 |
| Has address | +5 |

**Flags:** Lis pendens · Pre-foreclosure · Judgment lien · Tax lien · Mechanic lien · HOA lien · Probate / estate · LLC / corp owner · New this week

---

## File Structure

```
.github/workflows/scrape.yml   GitHub Actions schedule + deploy
scraper/
  fetch.py                     Main scraper (Playwright + requests)
  requirements.txt             Python dependencies
dashboard/
  index.html                   Interactive lead dashboard
  records.json                 Latest leads (served via GitHub Pages)
data/
  records.json                 Mirror of dashboard/records.json
  ghl_export_YYYYMMDD.csv      GHL-ready CSV (generated each run)
  parcel_cache.json            Cached assessor parcel index (< 24h)
```

---

## Setup

### 1. Fork / clone this repo

### 2. Enable GitHub Pages
- Go to **Settings → Pages**
- Set **Source** to `GitHub Actions`

### 3. Enable workflow permissions
- **Settings → Actions → General → Workflow permissions**
- Select **Read and write permissions**

### 4. Run manually (first time)
- **Actions → Scrape Maricopa Motivated Seller Leads → Run workflow**

---

## Local Development

```bash
# Install deps
pip install -r scraper/requirements.txt
python -m playwright install chromium

# Run scraper
python scraper/fetch.py

# View dashboard locally
cd dashboard && python -m http.server 8080
# Open http://localhost:8080
```

---

## Data Sources

- **Clerk of Court:** https://www.clerkofcourt.maricopa.gov/records
- **Assessor parcel data:** https://mcassessor.maricopa.gov/downloads.php

---

## GHL Export

Each run generates `data/ghl_export_YYYYMMDD.csv` with columns:

`First Name · Last Name · Mailing Address · Mailing City · Mailing State · Mailing Zip · Property Address · Property City · Property State · Property Zip · Lead Type · Document Type · Date Filed · Document Number · Amount/Debt Owed · Seller Score · Motivated Seller Flags · Source · Public Records URL`

The dashboard **Export CSV** button generates the same format from filtered/sorted results in-browser.
