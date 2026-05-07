"""
Draft Message_1 in a strict template from Audit Summary + Intelligence Debrief.

Rules:
- Skip rows where Audit Summary or Intelligence Debrief is missing.
- Keep fixed template blocks unchanged.
- Fill only Message_1 (unless --overwrite is used).

Usage:
  python draft_message1_from_audits.py \
    -i "/Users/rishu/Downloads/Brick Attr - Similarweb+SearchApi.csv"
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from pathlib import Path
from typing import Any

import requests


def _clean(s: str) -> str:
    return " ".join((s or "").split()).strip()


def context_type_from_audit(audit_summary: str) -> str:
    t = (audit_summary or "").lower()
    if "total traffic from ads" in t or "search - paid" in t or "display ads" in t:
        return "similarweb"
    if "meta ad library" in t or "ad object" in t or "active ads" in t:
        return "meta"
    return "generic"


def _build_prompt(
    recipient_name: str,
    merchant_name: str,
    audit_summary: str,
    intelligence_debrief: str,
) -> str:
    context_type = context_type_from_audit(audit_summary)
    if context_type == "similarweb":
        bridge_instruction = (
            "Generate this line in your own words with 'channel' language (not 'active set')."
        )
    elif context_type == "meta":
        bridge_instruction = (
            "Generate this line in your own words with 'active set' language."
        )
    else:
        bridge_instruction = (
            "Generate this line in neutral terms; do not mention numbers."
        )
    return f"""
Use this STRICT format and keep fixed sections exactly:

{recipient_name}:

<Opening sentence from Intelligence Debrief if it starts with 'I went through'; otherwise: I went through {merchant_name}'s ads this week.>

<One observation paragraph grounded ONLY in Audit Summary values/patterns. Never invent numbers. If Audit Summary is missing/no-audit mode, use Intelligence Debrief with no numbers and focus on attribution/channel waste.>

<Generate this transition line naturally: {bridge_instruction}>

1. Either it's deliberate — you've found what resonates and you're staying tight on it. Or there just hasn't been enough room to test whether a different angle, a different entry point into the brand, would convert better.

2. Not a creative problem exactly — more that the signal isn't there yet to know what's worth doubling down on. Most brands I talk to at this stage are somewhere in the second.

We help a few brands with problems like the latter. Happy to compare notes if this is relevant.

Hari, Founder & CEO, NITI AI

Hard constraints:
- Keep the 1. and 2. lines EXACTLY as written above.
- Keep the final closing sentence EXACTLY as written above.
- Use only information present in inputs.
- No invented counts (e.g. never assume 4 ads).
- Keep message natural and human.
- The 1. and 2. lines and the closing line must be exact.

Inputs:
Merchant: {merchant_name}
Intelligence Debrief: {intelligence_debrief}
Audit Summary: {audit_summary}
""".strip()

def build_message_with_gemini(
    recipient_name: str,
    merchant_name: str,
    audit_summary: str,
    intelligence_debrief: str,
    model: str,
    api_key: str,
) -> str:
    prompt = _build_prompt(recipient_name, merchant_name, audit_summary, intelligence_debrief)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4},
        },
        timeout=45,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini response has no candidates.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text_chunks = [str(p.get("text", "")) for p in parts if isinstance(p, dict)]
    text = "\n".join(t for t in text_chunks if t).strip()
    if not text:
        raise ValueError("Gemini response did not contain text.")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Draft Message_1 from Audit Summary + Intelligence Debrief.")
    parser.add_argument("-i", "--input", type=Path, required=True)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing Message_1 values.")
    parser.add_argument("--company-column", default="merchant_name")
    parser.add_argument("--recipient-column", default="contact_name")
    parser.add_argument("--audit-column", default="Audit Summary")
    parser.add_argument("--debrief-column", default="Intelligence Debrief")
    parser.add_argument("--message-column", default="Message_1")
    parser.add_argument("--gemini-model", default="gemini-1.5-flash")
    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise SystemExit(f"Input not found: {input_path}")
    out_path = args.output.expanduser().resolve() if args.output else input_path.with_name(
        f"{input_path.stem}_with_messages{input_path.suffix}"
    )

    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise SystemExit("CSV has no headers.")
        fieldnames = list(reader.fieldnames)
        for col in (args.company_column, args.audit_column, args.debrief_column):
            if col not in fieldnames:
                raise SystemExit(f"Missing required column: {col}")
        if args.message_column not in fieldnames:
            fieldnames.append(args.message_column)
        error_col = f"{args.message_column}_error"
        if error_col not in fieldnames:
            fieldnames.append(error_col)
        rows = list(reader)

    drafted = 0
    skipped_missing = 0
    skipped_existing = 0
    drafted_generic_no_audit = 0
    ai_failed = 0

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
    if not gemini_key:
        raise SystemExit("Set GEMINI_API_KEY or GOOGLE_API_KEY.")

    for row in rows:
        company = _clean(row.get(args.company_column, ""))
        recipient = _clean(row.get(args.recipient_column, "")) or "Bhavya"
        audit = _clean(row.get(args.audit_column, ""))
        debrief = _clean(row.get(args.debrief_column, ""))
        existing = _clean(row.get(args.message_column, ""))
        if not company:
            skipped_missing += 1
            continue
        if existing and not args.overwrite:
            skipped_existing += 1
            continue
        no_audit_mode = (not audit) or audit.lower().startswith("no audit summary")
        no_debrief_mode = not debrief
        # Skip only when BOTH signals are unavailable.
        if no_audit_mode and no_debrief_mode:
            skipped_missing += 1
            continue
        try:
            row[args.message_column] = build_message_with_gemini(
                recipient_name=recipient,
                merchant_name=company,
                audit_summary=audit if not no_audit_mode else "NO_AUDIT_AVAILABLE",
                intelligence_debrief=debrief or f"I went through {company}'s ads this week.",
                model=args.gemini_model,
                api_key=gemini_key,
            )
            row[f"{args.message_column}_error"] = ""
        except Exception as exc:
            ai_failed += 1
            row[args.message_column] = f""
            row[f"{args.message_column}_error"] = str(exc)
        if no_audit_mode:
            drafted_generic_no_audit += 1
        drafted += 1

    with out_path.open("w", encoding="utf-8", newline="") as wf:
        writer = csv.DictWriter(wf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    print(
        f"Wrote {len(rows)} rows -> {out_path}\n"
        f"  Drafted Message_1: {drafted}\n"
        f"  Drafted generic (no-audit, intelligence-only): {drafted_generic_no_audit}\n"
        f"  Skipped (company missing OR both audit+debrief unavailable): {skipped_missing}\n"
        f"  Skipped existing Message_1: {skipped_existing}\n"
        f"  AI errors (left blank): {ai_failed}"
    )


if __name__ == "__main__":
    main()
