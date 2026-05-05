"""
Companies House angel extraction via confirmation statement parsing.
Extracts named shareholders from CS01 PDFs, filters out founders/directors/funds,
deduplicates across companies.

Usage:
    pip install requests pdfplumber rapidfuzz
    export COMPANIES_HOUSE_API_KEY="your_key_here"
    python ch_angel_extract.py --input companies.csv --output angels.csv

Note: PDF parsing is imperfect. Expect 60-80% clean extraction; manually review
the output for the top-priority companies.
"""

import argparse
import csv
import io
import os
import re
import sys
import time
from collections import defaultdict

import pdfplumber
import requests
from rapidfuzz import fuzz

API_BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"
RATE_LIMIT_DELAY = 0.6


# --- API helpers --------------------------------------------------------

def ch_get(url, api_key, params=None, accept_json=True):
    headers = {"Accept": "application/json"} if accept_json else {}
    resp = requests.get(url, auth=(api_key, ""), params=params,
                        headers=headers, timeout=60, allow_redirects=True)
    time.sleep(RATE_LIMIT_DELAY)
    if resp.status_code == 429:
        print("  ! rate limited, sleeping 60s")
        time.sleep(60)
        resp = requests.get(url, auth=(api_key, ""), params=params,
                            headers=headers, timeout=60, allow_redirects=True)
        time.sleep(RATE_LIMIT_DELAY)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp


def search_company_by_name(name, api_key):
    resp = ch_get(f"{API_BASE}/search/companies", api_key,
                  params={"q": name, "items_per_page": 5})
    if not resp:
        return None
    items = resp.json().get("items", [])
    if not items:
        return None
    for item in items:
        if item.get("company_status") == "active":
            a = re.sub(r"[^a-z0-9]", "", name.lower())
            b = re.sub(r"[^a-z0-9]", "", item.get("title", "").lower())
            if a == b or b.startswith(a):
                return item["company_number"]
    return items[0]["company_number"]


def get_filing_history(company_number, api_key):
    resp = ch_get(f"{API_BASE}/company/{company_number}/filing-history",
                  api_key, params={"items_per_page": 100, "category": "confirmation-statement"})
    if not resp:
        return []
    return resp.json().get("items", [])


def get_officers_and_pscs(company_number, api_key):
    """Used as an exclusion list — anyone here is NOT an angel candidate."""
    excluded_names = set()
    resp = ch_get(f"{API_BASE}/company/{company_number}/officers", api_key,
                  params={"items_per_page": 50})
    if resp:
        for o in resp.json().get("items", []):
            excluded_names.add(normalize_name(o.get("name", "")))
    resp = ch_get(f"{API_BASE}/company/{company_number}/persons-with-significant-control",
                  api_key, params={"items_per_page": 50})
    if resp:
        for p in resp.json().get("items", []):
            excluded_names.add(normalize_name(p.get("name", "")))
    return excluded_names


def download_filing_pdf(filing_item, api_key):
    """Download a filing PDF and return raw bytes, or None."""
    doc_meta = filing_item.get("links", {}).get("document_metadata")
    if not doc_meta:
        return None
    # document_metadata is a URL like
    # https://frontend-doc-api.company-information.service.gov.uk/document/{id}
    # We append /content and request PDF
    content_url = doc_meta.rstrip("/") + "/content"
    resp = requests.get(content_url, auth=(api_key, ""),
                        headers={"Accept": "application/pdf"},
                        timeout=120, allow_redirects=True)
    time.sleep(RATE_LIMIT_DELAY)
    if resp.status_code != 200:
        return None
    if not resp.content[:4] == b"%PDF":
        return None
    return resp.content


# --- PDF parsing --------------------------------------------------------

def extract_shareholders_from_pdf(pdf_bytes):
    """Parse a CS01 PDF and return list of {'name': str, 'shares': str} dicts.
    
    CS01 shareholder sections vary in format. Common patterns:
    - Tabular: Name | Class | Number of shares | Currency
    - List: 'SHAREHOLDING 1: NAME ...'
    - Sometimes split across multiple pages
    
    This function uses several heuristics. Inspect the output for accuracy.
    """
    shareholders = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        print(f"  ! pdf parse error: {e}")
        return shareholders

    # Pattern 1: 'SHAREHOLDINGS' or 'SHAREHOLDING N: ' marker, common in CS01
    pattern1 = re.compile(
        r"SHAREHOLDING\s+\d+\s*:?\s*"
        r"([\d,]+)\s+([A-Z\s]+?)\s+SHARES?"  # number + class
        r".*?Name:\s*([A-Z][A-Z\s\-'.,]+?)(?=\n[A-Z]|\nAddress|\nShareholder|$)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern1.finditer(full_text):
        shareholders.append({
            "name": m.group(3).strip(),
            "shares": m.group(1),
            "share_class": m.group(2).strip(),
            "extraction_method": "pattern1",
        })

    # Pattern 2: 'Name: X' followed by share count nearby
    if not shareholders:
        pattern2 = re.compile(
            r"Name[:\s]+([A-Z][A-Z\s\-'.,]{4,60})\s*\n"
            r".*?([\d,]+)\s+(?:Ordinary|ORDINARY|Preference|A Ordinary|B Ordinary)",
            re.IGNORECASE | re.DOTALL,
        )
        for m in pattern2.finditer(full_text):
            shareholders.append({
                "name": m.group(1).strip(),
                "shares": m.group(2),
                "share_class": "unknown",
                "extraction_method": "pattern2",
            })

    # Pattern 3: tabular data — pdfplumber tables (more reliable when it works)
    if not shareholders:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    for table in page.extract_tables() or []:
                        for row in table:
                            if not row or not any(row):
                                continue
                            # Look for rows with a name-like cell and a number-like cell
                            cells = [c.strip() if c else "" for c in row]
                            name_candidates = [c for c in cells
                                              if c and re.match(r"^[A-Z][A-Za-z\s\-'.,]{4,}$", c)
                                              and not c.upper() in ("NAME", "CLASS", "SHARES")]
                            num_candidates = [c for c in cells
                                             if c and re.match(r"^[\d,]+$", c)]
                            if name_candidates and num_candidates:
                                shareholders.append({
                                    "name": name_candidates[0],
                                    "shares": num_candidates[0],
                                    "share_class": "unknown",
                                    "extraction_method": "table",
                                })
        except Exception:
            pass

    return shareholders


# --- Filtering and dedup ------------------------------------------------

def normalize_name(name):
    """Lower, strip titles, collapse whitespace, remove punctuation."""
    titles = ["mr ", "mrs ", "ms ", "miss ", "dr ", "prof ", "sir ", "lord ", "lady "]
    n = name.lower().strip()
    for t in titles:
        if n.startswith(t):
            n = n[len(t):]
    n = re.sub(r"[^a-z\s\-]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def is_likely_corporate(name):
    """Returns True if name looks like a company/fund, not an individual."""
    upper = name.upper()
    markers = ["LTD", "LIMITED", "LLP", "PLC", "INC", "GROUP", "HOLDINGS",
               "CAPITAL", "VENTURES", "PARTNERS", "FUND", "TRUSTEES", "NOMINEES",
               "INVESTMENTS", "ASSOCIATES", "MANAGEMENT", "ADVISORS", "ENTERPRISES",
               "BANK", "CORPORATION", "CORP"]
    return any(m in upper.split() or f" {m}" in f" {upper}" for m in markers)


def fuzzy_match_excluded(candidate_name_normalized, excluded_set, threshold=85):
    """Check if a name fuzzily matches any in the excluded set (founders/directors)."""
    for excluded in excluded_set:
        if fuzz.token_set_ratio(candidate_name_normalized, excluded) >= threshold:
            return True
    return False


def looks_like_angel(name, shares_str):
    """Heuristic check that a row plausibly represents an angel investor."""
    if is_likely_corporate(name):
        return False
    # Very small shareholdings (1-100 shares) are often founders' nominal shares
    # Very large shareholdings (>20% of typical SEIS round volumes) could be founders
    # Mid-range is the angel sweet spot — but without knowing total share count,
    # we can't reliably filter on this. Leave it to the user to inspect.
    if not re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", name):
        # Doesn't look like a person's name (no two word-tokens of letters)
        return False
    return True


# --- Main pipeline ------------------------------------------------------

def process_company(row, api_key, max_filings=2):
    """Returns list of shareholder candidates (excluding founders/directors)."""
    name = row["company_name"]
    number = row.get("companies_house_number", "").strip()

    if not number:
        print(f"  searching CH for: {name}")
        number = search_company_by_name(name, api_key)
        if not number:
            print(f"  ! no CH match")
            return []
        row["companies_house_number"] = number

    print(f"  fetching {name} ({number})")
    excluded = get_officers_and_pscs(number, api_key)

    filings = get_filing_history(number, api_key)
    if not filings:
        print(f"  ! no confirmation statements found")
        return []

    # Most recent first; take latest few
    filings = sorted(filings, key=lambda x: x.get("date", ""), reverse=True)[:max_filings]

    candidates = []
    for filing in filings:
        filing_date = filing.get("date", "")
        print(f"  downloading CS01 dated {filing_date}")
        pdf_bytes = download_filing_pdf(filing, api_key)
        if not pdf_bytes:
            print(f"    ! could not download")
            continue

        shareholders = extract_shareholders_from_pdf(pdf_bytes)
        print(f"    extracted {len(shareholders)} raw shareholders")

        for sh in shareholders:
            sh_name = sh["name"]
            sh_name_norm = normalize_name(sh_name)
            if not sh_name_norm:
                continue
            if not looks_like_angel(sh_name, sh.get("shares", "")):
                continue
            if fuzzy_match_excluded(sh_name_norm, excluded):
                continue
            candidates.append({
                "name": sh_name,
                "name_key": sh_name_norm,
                "company_invested_in": name,
                "ch_number": number,
                "filing_date": filing_date,
                "shares": sh.get("shares", ""),
                "share_class": sh.get("share_class", ""),
                "extraction_method": sh.get("extraction_method", ""),
            })
    return candidates


def aggregate_by_person(all_candidates):
    """Dedup by normalized name. Note: CS01 doesn't include DOB, so dedup is name-only.
    Same name across different companies is plausibly the same person; flag accordingly."""
    by_key = defaultdict(list)
    for c in all_candidates:
        by_key[c["name_key"]].append(c)

    angels = []
    for key, records in by_key.items():
        display_name = max((r["name"] for r in records), key=len)
        companies = sorted(set(r["company_invested_in"] for r in records))
        share_summary = "; ".join(
            f"{r['company_invested_in']}:{r['shares']}({r['share_class']})"
            for r in records
        )
        angels.append({
            "angel_name": display_name,
            "name_key": key,
            "companies_invested_in": "; ".join(companies),
            "investment_count": len(companies),
            "share_summary": share_summary,
            "extraction_methods": "; ".join(sorted(set(r["extraction_method"] for r in records))),
            # Quality flag: name-only dedup is risky for common names
            "dedup_note": "name_only_no_dob_in_cs01",
        })
    return sorted(angels, key=lambda a: -a["investment_count"])


def classify_professional_active(angels):
    """Add professional/active flag based on multi-company appearance."""
    for a in angels:
        n = a["investment_count"]
        if n >= 4:
            a["angel_tier"] = "professional_high_conviction"
        elif n >= 2:
            a["angel_tier"] = "professional_active"
        else:
            a["angel_tier"] = "single_investment"
    return angels


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-filings-per-company", type=int, default=2,
                        help="How many recent confirmation statements to parse per company")
    args = parser.parse_args()

    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        sys.exit("Set COMPANIES_HOUSE_API_KEY environment variable.")

    with open(args.input, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    all_candidates = []
    for i, row in enumerate(rows, 1):
        print(f"\n[{i}/{len(rows)}] {row['company_name']}")
        try:
            cands = process_company(row, api_key, max_filings=args.max_filings_per_company)
            all_candidates.extend(cands)
        except Exception as e:
            print(f"  ! error: {e}")

    print(f"\n{len(all_candidates)} raw shareholder records extracted")
    angels = aggregate_by_person(all_candidates)
    angels = classify_professional_active(angels)
    print(f"{len(angels)} unique individuals after dedup")
    pro = sum(1 for a in angels if a["angel_tier"].startswith("professional"))
    print(f"{pro} flagged as professional/active angels")

    fieldnames = ["angel_name", "investment_count", "angel_tier",
                  "companies_invested_in", "share_summary",
                  "extraction_methods", "dedup_note", "name_key"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for a in angels:
            writer.writerow({k: a.get(k, "") for k in fieldnames})

    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()