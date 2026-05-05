"""
Append a Google search URL to each angel that surfaces their LinkedIn profile.
Query format: [name] angel investor [first company invested in] linkedin

Usage:
    python add_linkedin_urls.py --input angels.csv --output angels_with_links.csv
"""

import argparse
import csv
from urllib.parse import quote_plus


def google_search_url(name, context_company=None):
    """Builds a Google search URL: '[name] angel investor [company] linkedin'."""
    parts = [name, "angel investor"]
    if context_company:
        parts.append(context_company)
    parts.append("linkedin")
    query = " ".join(parts)
    return f"https://www.google.com/search?q={quote_plus(query)}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        original_fieldnames = list(rows[0].keys()) if rows else []

    for row in rows:
        name = row.get("angel_name", "").strip()
        if not name:
            row["google_search_url"] = ""
            continue

        companies = row.get("companies_invested_in", "").split(";")
        context_company = companies[0].strip() if companies else None

        row["google_search_url"] = google_search_url(name, context_company)

    new_fieldnames = original_fieldnames + ["google_search_url"]
    new_fieldnames = list(dict.fromkeys(new_fieldnames))

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=new_fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {args.output} with Google search URLs added.")


if __name__ == "__main__":
    main()