#!/usr/bin/env python3
"""
push_to_ghl.py
Pushes Maricopa County leads to GoHighLevel as contacts.

Checks for duplicates by doc number before creating new contacts.

IMPORTANT: Maricopa County's recorder API attaches grantor/grantee names to
Notice of Trustee's Sale (NS) and Liens (LN) documents 1-3 days AFTER the
document is first recorded. A record that shows up with an empty "owner"
today may have a real name in 1-3 days once fetch.py re-scrapes it on a
later run (fetch.py always re-checks a rolling 7-day window).

So instead of only looking at "new in the last 24h," this script re-checks
every record filed in the last RECHECK_WINDOW_DAYS days. If a contact
already exists in GHL for that doc_num but is still a placeholder ("Unknown
Lead"), and the record now has a real name, the contact gets updated in
place rather than skipped.
"""

import json, os, sys, time
from datetime import datetime, timedelta

import requests

GHL_API_KEY   = os.environ["GHL_API_KEY"]
LOCATION_ID   = "CvMj40VHe3Z9at2VihbU"
BASE_URL      = "https://services.leadconnectorhq.com"
TAGS          = ["Ai Motivated Seller Leads", "Maricopa County"]
RATE_SLEEP    = 0.25
MARKER_FILE   = "dashboard/.ghl_initialized"
RECORDS_FILE  = "dashboard/records.json"

# How many days back to keep re-checking a record for a resolved name.
# Observed lag on N/TR SALE and Liens docs is 1-3 days; 5 gives buffer.
RECHECK_WINDOW_DAYS = 5

HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version":        "2021-07-28",
    "Content-Type":   "application/json",
    "Accept":         "application/json",
}

PLACEHOLDER_NAMES = {"", "unknown", "unknown lead"}


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


def is_in_window(rec, days: int) -> bool:
    filed = (rec.get("filed") or "").strip()
    if not filed:
        return False
    try:
        d = datetime.strptime(filed, "%Y-%m-%d").date()
        return d >= (datetime.utcnow() - timedelta(days=days)).date()
    except Exception:
        return False


def build_note(rec, resolved: bool = False) -> str:
    lines = []
    if resolved:
        lines.append(f"[Name resolved {datetime.utcnow().strftime('%Y-%m-%d')}]")
    lines += [
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
    if rec.get("mail_address"):
        lines.append(
            f"Mailing    : {rec['mail_address']}, "
            f"{rec.get('mail_city','')}, {rec.get('mail_state','AZ')} {rec.get('mail_zip','')}"
        )
    if rec.get("flags"):
        lines.append(f"Flags      : {', '.join(rec['flags'])}")
    if rec.get("clerk_url"):
        lines.append(f"Clerk Link : {rec['clerk_url']}")
    return "\n".join(lines)


def search_contact_by_doc(doc_num: str):
    """Search GHL for existing contact with this doc number in notes.
    Returns (contact_id, current_name) or (None, None)."""
    try:
        r = requests.get(
            f"{BASE_URL}/contacts/search",
            headers=HEADERS,
            params={"locationId": LOCATION_ID, "query": doc_num, "limit": 1},
            timeout=15,
        )
        if r.status_code == 200:
            contacts = r.json().get("contacts", [])
            if contacts:
                c = contacts[0]
                return c.get("id"), (c.get("name") or c.get("firstName") or "")
    except Exception:
        pass
    return None, None


def create_contact(rec) -> tuple:
    first, last = split_name(rec.get("owner", ""))
    payload = {
        "locationId": LOCATION_ID,
        "firstName":  first or last or "Unknown",
        "lastName":   last if first else "Lead",
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


def update_contact(contact_id: str, rec) -> bool:
    """Rename a placeholder contact once a real owner name has resolved."""
    first, last = split_name(rec.get("owner", ""))
    payload = {
        "firstName":  first or last or "Unknown",
        "lastName":   last if first else "Lead",
        "name":       (rec.get("owner", "") or "Unknown Lead").title(),
        "address1":   rec.get("mail_address") or rec.get("prop_address") or "",
        "city":       rec.get("mail_city")    or rec.get("prop_city")    or "",
        "state":      rec.get("mail_state")   or rec.get("prop_state")   or "AZ",
        "postalCode": rec.get("mail_zip")     or rec.get("prop_zip")     or "",
    }
    r = requests.put(
        f"{BASE_URL}/contacts/{contact_id}", headers=HEADERS, json=payload, timeout=15
    )
    return r.status_code in (200, 201)


def add_note(contact_id: str, body: str) -> bool:
    r = requests.post(
        f"{BASE_URL}/contacts/{contact_id}/notes",
        headers=HEADERS,
        json={"body": body},
        timeout=15,
    )
    return r.status_code in (200, 201)


def push_batch(records: list) -> tuple:
    pushed, updated, skipped, errors = 0, 0, 0, 0
    for i, rec in enumerate(records, 1):
        doc_num = rec.get("doc_num", "")
        owner = (rec.get("owner") or "").strip()

        existing_id, existing_name = search_contact_by_doc(doc_num)
        time.sleep(RATE_SLEEP)

        if existing_id:
            is_placeholder = (existing_name or "").strip().lower() in PLACEHOLDER_NAMES
            if is_placeholder and owner:
                # Name has resolved since we last pushed this one - update it.
                ok = update_contact(existing_id, rec)
                time.sleep(RATE_SLEEP)
                if ok:
                    add_note(existing_id, build_note(rec, resolved=True))
                    time.sleep(RATE_SLEEP)
                    updated += 1
                    print(f"  [{i}/{len(records)}] UPDATED {doc_num} | {owner[:30]}")
                else:
                    errors += 1
                    print(f"  [{i}/{len(records)}] ERR (update) {doc_num}")
            else:
                skipped += 1
                print(f"  [{i}/{len(records)}] SKIP {doc_num} | already exists")
            continue

        contact_id, status = create_contact(rec)
        time.sleep(RATE_SLEEP)

        if contact_id:
            note_ok = add_note(contact_id, build_note(rec))
            time.sleep(RATE_SLEEP)
            pushed += 1
            note_mark = "ok" if note_ok else "note-err"
            print(f"  [{i}/{len(records)}] OK {doc_num} | {owner or '?'} | note:{note_mark}")
        else:
            errors += 1
            print(f"  [{i}/{len(records)}] ERR {doc_num} | {status}")

    return pushed, updated, skipped, errors


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
        to_push = [r for r in all_records if is_in_window(r, RECHECK_WINDOW_DAYS)]
        print(
            f"Daily run - {len(to_push)} records filed in the last "
            f"{RECHECK_WINDOW_DAYS} days (of {len(all_records)} total) "
            f"being pushed/rechecked"
        )

    if not to_push:
        print("No leads to push today - done.")
        open(MARKER_FILE, "w").write(datetime.utcnow().isoformat())
        return

    pushed, updated, skipped, errors = push_batch(to_push)

    if first_run:
        with open(MARKER_FILE, "w") as f:
            f.write(datetime.utcnow().isoformat())
        print(f"\nMarker file written - future runs will be incremental.")

    print(f"\n{'='*50}")
    print(f"Pushed: {pushed}  Updated: {updated}  Skipped: {skipped}  Errors: {errors}")


if __name__ == "__main__":
    main()
