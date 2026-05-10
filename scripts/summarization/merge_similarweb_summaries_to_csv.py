"""
Push Similarweb ad summaries from output/*_similarweb.json into a leads CSV.

Matches CSV rows to JSON by merchant_name stem (same rule as audit.py filenames).

Usage:
  python merge_similarweb_summaries_to_csv.py \\
    -i "/Users/rishu/Downloads/Brick Attr - Sheet3_with_similarweb.csv" \\
    -o "/Users/rishu/Downloads/Brick Attr - Sheet3_with_similarweb.csv"

  # Only fill rows where Audit Summary is empty or starts with "No audit summary"
  python merge_similarweb_summaries_to_csv.py -i leads.csv --only-missing

  # Write to a different column
  python merge_similarweb_summaries_to_csv.py -i leads.csv --column "Similarweb Summary"
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path

from similarweb_audit import build_ad_focused_summary, has_usable_similarweb_data

_REPO_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = _REPO_ROOT / "output"


def json_stem(merchant_name: str) -> str:
    return merchant_name.replace("/", "_").replace(" ", "_")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge Similarweb JSON summaries into CSV.")
    parser.add_argument("-i", "--input", type=Path, required=True)
    parser.add_argument("-o", "--output", type=Path, default=None, help="Default: overwrite -i")
    parser.add_argument("--backup", action="store_true", help="Copy input to .bak before write")
    parser.add_argument(
        "--company-column",
        default="merchant_name",
        help="Column with brand name (must match JSON filename stem). Default: merchant_name",
    )
    parser.add_argument(
        "--column",
        default="Audit Summary",
        help="Column to write summaries into. Default: Audit Summary",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Only update when column is empty or starts with 'No audit summary' (case-insensitive)",
    )
    parser.add_argument(
        "--skip-no-json",
        action="store_true",
        help="Skip rows with no matching *_similarweb.json (default: skip silently)",
    )
    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise SystemExit(f"Input not found: {input_path}")

    out_path = args.output.expanduser().resolve() if args.output else input_path

    with input_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise SystemExit("CSV has no header.")
        fieldnames = list(reader.fieldnames)
        company_col = args.company_column
        summary_col = args.column
        if company_col not in fieldnames:
            raise SystemExit(f"Missing column {company_col!r}. Have: {fieldnames}")
        if summary_col not in fieldnames:
            fieldnames.append(summary_col)
        rows = list(reader)

    filled = 0
    skipped_no_json = 0
    skipped_only_missing = 0
    skipped_no_data = 0

    for row in rows:
        company = (row.get(company_col) or "").strip()
        if not company:
            continue

        json_path = OUTPUT_DIR / f"{json_stem(company)}_similarweb.json"
        if not json_path.is_file():
            skipped_no_json += 1
            continue

        existing = (row.get(summary_col) or "").strip()
        if args.only_missing:
            low = existing.lower()
            if existing and not low.startswith("no audit summary"):
                skipped_only_missing += 1
                continue

        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not has_usable_similarweb_data(payload):
            skipped_no_data += 1
            continue

        row[summary_col] = build_ad_focused_summary(payload)
        filled += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.backup:
        bak = input_path.with_suffix(input_path.suffix + ".bak")
        shutil.copy2(input_path, bak)
        print(f"Backup -> {bak}")

    with out_path.open("w", newline="", encoding="utf-8") as wf:
        w = csv.DictWriter(wf, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})

    print(
        f"Wrote {len(rows)} rows -> {out_path}\n"
        f"  Filled Similarweb summaries: {filled}\n"
        f"  No matching JSON in output/: {skipped_no_json}\n"
        f"  Skipped (--only-missing, already had summary): {skipped_only_missing}\n"
        f"  Skipped (JSON had no usable channel data): {skipped_no_data}"
    )


if __name__ == "__main__":
    main()
