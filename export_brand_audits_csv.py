"""
Build a two-column CSV from Meta audit JSON in output/, keyed by brands.csv.

Columns: company_name, audit
- company_name is exactly brand_name from brands.csv.
- audit is a short *summary* of the ad set (not raw per-ad fields).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

from summarize_company_audits import (
    BRANDS_CSV,
    OUTPUT_DIR,
    flight_window,
    json_stem,
    load_brands_in_order,
    meta_library_page_matches_brand,
    summarize_formats,
    summarize_platforms,
)


def snapshot_body_text(snapshot: dict) -> str:
    body = snapshot.get("body")
    if isinstance(body, dict):
        return str(body.get("text") or "")
    if isinstance(body, str):
        return body
    return ""


def _norm(s: str) -> str:
    return " ".join(s.split())


def summarize_ctas(ads: list[dict]) -> str:
    c: Counter[str] = Counter()
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        snap = ad.get("snapshot") if isinstance(ad.get("snapshot"), dict) else {}
        ct = snap.get("cta_text")
        if isinstance(ct, str) and ct.strip():
            c[ct.strip()] += 1
    if not c:
        return "Not consistently surfaced in the sample."
    parts = [f"{k} (~{v} creatives)" for k, v in c.most_common(6)]
    return "; ".join(parts)


def summarize_branded_partners(ads: list[dict], limit: int = 6) -> str:
    c: Counter[str] = Counter()
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        snap = ad.get("snapshot") if isinstance(ad.get("snapshot"), dict) else {}
        bc = snap.get("branded_content")
        if isinstance(bc, dict):
            name = bc.get("page_name")
            if isinstance(name, str) and name.strip():
                c[name.strip()] += 1
    if not c:
        return "No separate branded-content pages tagged in this sample."
    parts = [f"{n} ({cnt} ad(s))" for n, cnt in c.most_common(limit)]
    return "; ".join(parts)


def _is_template_title(s: str) -> bool:
    s = s.strip()
    if not s:
        return True
    if "{{" in s and "}}" in s:
        return True
    if s in ("{{product.name}}", "{{product.brand}}"):
        return True
    return False


def messaging_highlights(ads: list[dict], max_bullets: int = 8) -> list[str]:
    """Distinctive copy lines from titles / bodies / cards, weighted by frequency."""
    c: Counter[str] = Counter()
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        snap = ad.get("snapshot") if isinstance(ad.get("snapshot"), dict) else {}
        t = snap.get("title")
        if isinstance(t, str):
            t = _norm(t)
            if len(t) >= 20 and not _is_template_title(t):
                c[t[:240]] += 1
        b = _norm(snapshot_body_text(snap))
        if len(b) >= 35:
            c[b[:260]] += 1
        for card in snap.get("cards") or []:
            if not isinstance(card, dict):
                continue
            for key in ("title", "body"):
                v = card.get(key)
                if isinstance(v, str):
                    v = _norm(v)
                    if len(v) >= 25:
                        c[v[:240]] += 1
    out: list[str] = []
    for text, count in c.most_common(max_bullets * 3):
        if len(text) < 25:
            continue
        clipped = text if len(text) <= 180 else text[:177] + "…"
        if count > 1:
            out.append(f"• {clipped} (appears ~{count}× among sampled units)")
        else:
            out.append(f"• {clipped}")
        if len(out) >= max_bullets:
            break
    return out


_OFFER_RE = re.compile(
    r"\b(\d+\s*%?\s*off|buy\s*\d|b1g1|free\s+shipping|cod\b|cash\s*on\s*delivery|"
    r"flat\s+\d+|sale|clearance|₹\s*[\d,]+)\b",
    re.I,
)


def tone_line(ads: list[dict]) -> str:
    bodies: list[str] = []
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        snap = ad.get("snapshot") if isinstance(ad.get("snapshot"), dict) else {}
        bodies.append(snapshot_body_text(snap))
        for card in snap.get("cards") or []:
            if isinstance(card, dict) and isinstance(card.get("body"), str):
                bodies.append(card["body"])
    joined = " ".join(bodies)
    hits = len(_OFFER_RE.findall(joined))
    if hits >= max(5, len(ads) * 2):
        return "Offer and urgency language shows up often (discounts, COD, bundles, etc.)."
    if hits == 0:
        return "Promo-style keywords are sparse in the sampled copy; more brand or product-descriptive tone."
    return "Mix of promotional hooks and brand/product storytelling in sampled copy."


def build_audit_summary(company_name: str, payload: dict) -> str:
    info = payload.get("search_information") if isinstance(payload.get("search_information"), dict) else {}
    page = info.get("page") if isinstance(info.get("page"), dict) else {}
    lib_name = page.get("name") or "Unknown"
    lib_id = page.get("id") or "Unknown"
    total_results = info.get("total_results", "")
    ads = payload.get("ads") if isinstance(payload.get("ads"), list) else []
    n = len(ads)
    start, end = flight_window(ads)

    lines: list[str] = [
        f"Meta Ad Library page tied to this file: “{lib_name}” (page_id {lib_id}). "
        f"Your CSV row (brand): “{company_name}”.",
        "",
        f"Scale: the library reports ~{total_results} active ads; this JSON contains {n} ad object(s). "
        f"Approximate flight window across those objects: {start} → {end}.",
        "",
        f"Formats: {summarize_formats(ads)}.",
        f"Platforms: {summarize_platforms(ads)}.",
        "",
        f"Primary CTAs in the sample: {summarize_ctas(ads)}",
        "",
        f"Branded / creator partnerships (if tagged): {summarize_branded_partners(ads)}",
        "",
        f"Creative tone (heuristic from copy): {tone_line(ads)}",
        "",
        "Representative messaging (deduped snippets from titles, bodies, and carousel cards):",
    ]
    bullets = messaging_highlights(ads)
    if bullets:
        lines.extend(bullets)
    else:
        lines.append("• (No long-form copy extracted—likely catalog/DPA shells or sparse text in this pull.)")

    return "\n".join(lines).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output/company_ad_audits.csv"),
        help="Output CSV path (default: output/company_ad_audits.csv)",
    )
    args = parser.parse_args()

    brands = load_brands_in_order(BRANDS_CSV)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company_name", "audit"])
        for company_name in brands:
            path = OUTPUT_DIR / f"{json_stem(company_name)}.json"
            if not path.exists():
                w.writerow(
                    [
                        company_name,
                        "No audit summary: no matching JSON in output/ for this brands.csv row "
                        f"(expected file: {json_stem(company_name)}.json).",
                    ]
                )
                continue
            with path.open("r", encoding="utf-8") as jf:
                payload = json.load(jf)
            if not meta_library_page_matches_brand(company_name, payload):
                w.writerow([company_name, ""])
                continue
            audit = build_audit_summary(company_name, payload)
            w.writerow([company_name, audit])

    print(f"Wrote {len(brands)} rows -> {args.output}")


if __name__ == "__main__":
    main()
