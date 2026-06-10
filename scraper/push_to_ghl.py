#!/usr/bin/env python3
"""
push_to_ghl.py
Pushes Maricopa County leads to GoHighLevel as contacts.

First run  → pushes ALL records (no marker file present)
Daily runs → pushes only records filed in the last 24 hours

Env vars required:
  GHL_API_KEY   – Private Integration token (pit-...)
"""

import json, os, sys, time
from datetime import datetime, timedelta

import requests

# ── Config ────────────────────────────────────────────────────────
GHL_API_KEY   = os.environ["GHL_API_KEY"]
LOCATION_ID   = "CvMj40VHe3Z9at2VihbU"
BASE_URL      = "https://services.leadconnectorhq.com"
TAGS          = ["Ai Motivated Seller Leads", "Maricopa County"]
RATE_SLEEP    = 0.25        # seconds between API calls (~4 req/s)
MARKER_FILE   = "dashboard/.ghl_initialized"   # created after first run
RECORDS_FILE  = "dashboard/records.json"

HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version":        "2021-07-28",
    "Content-Type":   "application/json",
    "Accept":         "application/json",
}

# ── Helpers ───────────────────────────────────────────────────────
def split_name(full: str):
    full = (full or "").strip()
    if not full:
        return "Unknown", "Lead"
    keywords = ["LLC","INC","CORP","TRUST","ESTATE","LP ","LLP","HOMEOWNERS"]
    if any(k in full.upper() for k in keywords):
        return "", full.title()
    if "," in full:
        last, first = full.split(",", 1)
        return first.strip().title(), last.strip().title()
    parts = full.split()
    return parts[0].title(), " ".join(parts[1:]).title() if len(parts) > 1 else ""

def is_new(rec) -> bool:
    """Filed within the last 24 hours (for daily incremental runs)."""
    filed = (rec.get("filed") or "").strip()
    if not filed:
        return False
    try:
        d = datetime.strptime(filed, "%Y-%m-%d").date()
        return d >= (datetime.utcnow() - timedelta(hours=24)).date()
    except Exception:
        return False

def build_note(rec) -> str:
    lines = [
        f"Maricopa County Lead - {rec.get('cat_label', '')}",
        f"Doc Type   : {rec.get('doc_type', '')}",
        f"Doc Number : {rec.get('doc_num', '')}",
        f"Filed      : {rec.get('filed', '')}",
        f"Score      : {rec.get('score', '')}",
    ]
    if rec.get("amount"):
        lines.append(f"Amount     : ${int(rec['amount']):,}")
    if rec.get("prop_address"):
        lines.append(
            f"Property   : {rec['prop_address']}, "
            f"{rec.get('prop_city','')}, {rec.get('prop_state','AZ')} {rec.get('prop_zip','')}"
        )
    if rec.get("flags"):
        lines.append(f"Flags      : {', '.join(rec['flags'])}")
    if rec.get("clerk_url"):
        lines.append(f"Clerk Link : {rec['clerk_url']}")
    return "\n".join(lines)

# ── API calls ─────────────────────────────────────────────────────
def create_contact(rec) -> tuple:
    first, last = split_name(rec.get("owner", ""))
    payload = {
        "locationId": LOCATION_ID,
        "firstName":  first or "Unknown",
        "lastName":   last  or "Lead",
        "name":       (rec.get("owner", "") or "Unknown Lead").title(),
        "address1":   rec.get("mail_address") or rec.get("prop_address") or "",
        "city":       rec.get("mail_city")    or rec.get("prop_city")    or "",
        "state":      rec.get("mail_state")   or rec.get("prop_state")   or "AZ",
        "postalCode": rec.get("mail_zip")     or rec.get("prop_zip")     or "",
        "tags":       TAGS,
        "source":     "Maricopa County Public Records",
    }
    r = requests.post(
        f"{BASE_URL}/contacts/", headers=HEADERS, json=payload, timeout=15
    )
    if r.status_code in (200, 201):
        body = r.json()
        contact_id = body.get("contact", {}).get("id") or body.get("id")
        return contact_id, "ok"
    return None, f"HTTP {r.status_code}: {r.text[:120]}"

def add_note(contact_id: str, body: str) -> bool:
    r = requests.post(
        f"{BASE_URL}/contacts/{contact_id}/notes",
        headers=HEADERS,
        json={"body": body},
        timeout=15,
    )
    return r.status_code in (200, 201)

def push_batch(records: list) -> tuple:
    pushed, errors = 0, 0
    for i, rec in enumerate(records, 1):
        contact_id, status = create_contact(rec)
        time.sleep(RATE_SLEEP)

        if contact_id:
            note_ok = add_note(contact_id, build_note(rec))
            time.sleep(RATE_SLEEP)
            pushed += 1
            owner = (rec.get("owner") or "?")[:30]
            note_mark = "ok" if note_ok else "note-err"
            print(f"  [{i}/{len(records)}] OK {rec.get('doc_num')} | {owner} | note:{note_mark}")
        else:
            errors += 1
            print(f"  [{i}/{len(records)}] ERR {rec.get('doc_num')} | {status}")

    return pushed, errors

# ── Main ──────────────────────────────────────────────────────────
def main():
    if not os.path.exists(RECORDS_FILE):
        print(f"ERROR: {RECORDS_FILE} not found"); sys.exit(1)

    with open(RECORDS_FILE) as f:
        data = json.load(f)
    all_records = data.get("records", [])

    first_run = not os.path.exists(MARKER_FILE)

    if first_run:
        print(f"FIRST RUN - pushing all {len(all_records)} records to GHL...")
        to_push = all_records
    else:
        to_push = [r for r in all_records if is_new(r)]
        print(f"Daily run - {len(to_push)} new leads (last 24h) of {len(all_records)} total")

    if not to_push:
        print("No leads to push today - done.")
        open(MARKER_FILE, "w").write(datetime.utcnow().isoformat())
        return

    pushed, errors = push_batch(to_push)

    if first_run:
        with open(MARKER_FILE, "w") as f:
            f.write(datetime.utcnow().isoformat())
        print(f"\nMarker file written - future runs will be incremental.")

    print(f"\n{'='*50}")
    print(f"Pushed: {pushed}  Errors: {errors}")

if __name__ == "__main__":
    main()
