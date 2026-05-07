"""
Iteratively optimize Message_1 to reduce AI-detection score.

Flow per row:
1) Score current message (0-10; lower is better, human-like).
2) If score > target, rewrite via AI.
3) Re-score and repeat until <= target or max iterations.

This script is AI-only (no deterministic rewrite fallback).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from pathlib import Path
from typing import Any

import requests


def _clean(s: str) -> str:
    return " ".join((s or "").split()).strip()


def _extract_score(text: str) -> int:
    m = re.search(r"\b([0-9]|10)\b", text)
    if not m:
        raise ValueError(f"Could not parse score from: {text!r}")
    n = int(m.group(1))
    return max(0, min(10, n))


def score_with_gemini(message: str, model: str, api_key: str) -> tuple[int, str]:
    prompt = f"""
You are scoring AI-detection likelihood for a sales outreach message.
Return ONLY one integer from 0 to 10.
0 = very human-like, 10 = highly AI-like.

Message:
{message}
""".strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0}},
        timeout=45,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    raw = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
    return _extract_score(raw), raw


def rewrite_with_gemini(message: str, model: str, api_key: str) -> str:
    prompt = f"""
Rewrite this outreach message to sound more human and less AI-like, while keeping intent.
Keep it concise and natural.
If the message includes numbered points 1. and 2., keep them.
If it includes this exact line, keep it exact:
"We help a few brands with problems like the latter. Happy to compare notes if this is relevant."
Return only the rewritten message.

Message:
{message}
""".strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.7}},
        timeout=45,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    out = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
    if not out:
        raise ValueError("Gemini rewrite returned empty output.")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Iteratively optimize Message_1 AI-detection score.")
    parser.add_argument("-i", "--input", type=Path, required=True)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--message-column", default="Message_1")
    parser.add_argument("--overwrite", action="store_true", help="Replace Message_1 with optimized text.")
    parser.add_argument("--target-score", type=int, default=3, help="Stop when score <= target. Default 3.")
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--gemini-model", default="gemini-1.5-flash")
    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise SystemExit(f"Input not found: {input_path}")
    out_path = args.output.expanduser().resolve() if args.output else input_path.with_name(
        f"{input_path.stem}_optimized{input_path.suffix}"
    )

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
    if not gemini_key:
        raise SystemExit("Set GEMINI_API_KEY or GOOGLE_API_KEY.")

    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise SystemExit("CSV has no header.")
        fieldnames = list(reader.fieldnames)
        if args.message_column not in fieldnames:
            raise SystemExit(f"Column {args.message_column!r} not found.")
        for extra in (
            f"{args.message_column}_ai_score_before",
            f"{args.message_column}_ai_score_after",
            f"{args.message_column}_ai_opt_iterations",
            f"{args.message_column}_ai_opt_error",
            f"{args.message_column}_optimized",
        ):
            if extra not in fieldnames:
                fieldnames.append(extra)
        rows = list(reader)

    processed = 0
    optimized = 0
    failed = 0
    skipped_empty = 0

    for row in rows:
        msg = _clean(row.get(args.message_column, ""))
        if not msg:
            skipped_empty += 1
            continue
        processed += 1
        current = msg
        try:
            score, _raw = score_with_gemini(current, args.gemini_model, gemini_key)
            before = score
            iters = 0
            while score > args.target_score and iters < args.max_iterations:
                iters += 1
                current = rewrite_with_gemini(current, args.gemini_model, gemini_key)
                score, _raw = score_with_gemini(current, args.gemini_model, gemini_key)
            after = score
            row[f"{args.message_column}_ai_score_before"] = str(before)
            row[f"{args.message_column}_ai_score_after"] = str(after)
            row[f"{args.message_column}_ai_opt_iterations"] = str(iters)
            row[f"{args.message_column}_ai_opt_error"] = ""
            row[f"{args.message_column}_optimized"] = current
            if args.overwrite:
                row[args.message_column] = current
            if after <= args.target_score and (after < before or iters > 0):
                optimized += 1
        except Exception as exc:
            failed += 1
            row[f"{args.message_column}_ai_opt_error"] = str(exc)

    with out_path.open("w", encoding="utf-8", newline="") as wf:
        w = csv.DictWriter(wf, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})

    print(
        f"Wrote {len(rows)} rows -> {out_path}\n"
        f"  Processed with non-empty message: {processed}\n"
        f"  Reached target score <= {args.target_score}: {optimized}\n"
        f"  Failed rows: {failed}\n"
        f"  Skipped empty message rows: {skipped_empty}"
    )


if __name__ == "__main__":
    main()

