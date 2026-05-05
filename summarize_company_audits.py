import argparse
import json
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
import re


OUTPUT_DIR = Path("output")
BRANDS_CSV = Path("brands.csv")


def json_stem(brand_name: str) -> str:
    """Must match `save_json` in audit.py (filename stem)."""
    return brand_name.replace("/", "_").replace(" ", "_")


# Words too generic to prove a Meta page belongs to this CSV row.
_PAGE_MATCH_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "llc",
        "ltd",
        "inc",
        "pvt",
        "com",
        "co",
        "online",
        "shop",
        "store",
        "official",
        "india",
        "global",
    }
)


def _alphanumeric_compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def meta_library_page_matches_brand(company_name: str, payload: dict) -> bool:
    """
    True if search_information.page looks like the same brand as company_name.
    Prevents writing audits when Playwright resolved the wrong Meta page (e.g. Noise → Spotify).
    """
    info = payload.get("search_information")
    if not isinstance(info, dict):
        return False
    page = info.get("page")
    if not isinstance(page, dict):
        return False
    lib_name = page.get("name")
    lib_id = page.get("id")
    if not isinstance(lib_name, str) or not lib_name.strip():
        return False
    if lib_name.strip().lower() == "unknown":
        return False
    if lib_id is None or str(lib_id).strip() == "" or str(lib_id).strip().lower() == "unknown":
        return False

    c_compact = _alphanumeric_compact(company_name)
    p_compact = _alphanumeric_compact(lib_name)
    if len(c_compact) < 2:
        return False

    if len(c_compact) >= 3 and (c_compact in p_compact or p_compact in c_compact):
        return True

    p_lower = lib_name.lower()
    tokens = [
        t.lower()
        for t in re.findall(r"[A-Za-z]{3,}", company_name or "")
        if t.lower() not in _PAGE_MATCH_STOPWORDS
    ]
    if not tokens:
        return len(c_compact) >= 2 and c_compact in p_compact

    hits = sum(1 for t in tokens if t in p_lower or _alphanumeric_compact(t) in p_compact)
    need = max(1, (len(tokens) + 1) // 2)
    return hits >= need


def company_name_for_json_path(path: Path, ordered_brands: list[str]) -> str:
    stem = path.stem
    for b in ordered_brands:
        if json_stem(b) == stem:
            return b
    return stem.replace("_", " ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def flight_window(ads: list[dict]) -> tuple[str, str]:
    starts = [parse_iso(ad.get("start_date")) for ad in ads]
    ends = [parse_iso(ad.get("end_date")) for ad in ads]
    starts = [dt for dt in starts if dt]
    ends = [dt for dt in ends if dt]
    if not starts or not ends:
        return ("N/A", "N/A")
    return (min(starts).strftime("%b %d"), max(ends).strftime("%b %d"))


def summarize_formats(ads: list[dict]) -> str:
    if not ads:
        return "No creatives"
    format_counter = Counter()
    total_videos = 0
    for ad in ads:
        snapshot = ad.get("snapshot", {})
        display_format = snapshot.get("display_format", "UNKNOWN")
        format_counter[display_format] += 1
        total_videos += len(snapshot.get("videos", []) or [])

    top = [name for name, _ in format_counter.most_common(3)]
    has_video = "near-zero video" if total_videos == 0 else f"{total_videos} video assets"
    return f"Heavy {'/'.join(top)} + {has_video}"


def summarize_platforms(ads: list[dict]) -> str:
    platforms = set()
    for ad in ads:
        for platform in ad.get("publisher_platform", []) or []:
            platforms.add(platform)
    if not platforms:
        return "Unknown"
    order = ["FACEBOOK", "INSTAGRAM", "MESSENGER", "THREADS", "AUDIENCE_NETWORK"]
    sorted_platforms = [p for p in order if p in platforms] + sorted(platforms - set(order))
    mapping = {
        "FACEBOOK": "FB",
        "INSTAGRAM": "IG",
        "MESSENGER": "Messenger",
        "THREADS": "Threads",
        "AUDIENCE_NETWORK": "Audience Network",
    }
    return ", ".join(mapping.get(p, p.title()) for p in sorted_platforms)


OFFER_RE = re.compile(r"(off|discount|sale|deal|free shipping|cod|cash on delivery|b1g1|buy\s*\d)", re.I)


def _snapshot(ad: dict) -> dict:
    snap = ad.get("snapshot")
    return snap if isinstance(snap, dict) else {}


def _body_text(snap: dict) -> str:
    body = snap.get("body")
    if isinstance(body, dict):
        return str(body.get("text") or "")
    if isinstance(body, str):
        return body
    return ""


def dynamic_findings(company: str, ads: list[dict]) -> tuple[list[tuple[str, list[str], str]], str]:
    if not ads:
        return (
            [
                (
                    "Limited evidence from current ad pull",
                    [
                        "Very low or no active creatives in this payload.",
                        "Need a larger sample before diagnosing messaging and channel strategy.",
                        "Re-run after campaigns refresh for a stronger readout.",
                    ],
                    "insufficient signal, so prioritization is unclear",
                )
            ],
            "\"Your current Meta sample is too thin to optimize confidently. Let's first improve signal quality, then tune strategy.\"",
        )

    findings: list[tuple[str, list[str], str]] = []
    snaps = [_snapshot(ad) for ad in ads]

    # 1) Offer intensity
    offer_hits = 0
    for snap in snaps:
        text = f"{_body_text(snap)} {snap.get('title') or ''}"
        if OFFER_RE.search(text):
            offer_hits += 1
    offer_ratio = offer_hits / max(len(snaps), 1)
    if offer_ratio >= 0.4:
        findings.append(
            (
                "High dependence on offer-led hooks",
                [
                    f"~{round(offer_ratio * 100)}% of creatives include discount/COD/sale language.",
                    "Offer-first framing attracts price-sensitive traffic by default.",
                    "Brand and problem-solution storytelling gets less airtime.",
                ],
                "higher margin pressure and weaker full-price demand quality",
            )
        )
    else:
        findings.append(
            (
                "Offer pressure is present but not dominant",
                [
                    f"Only ~{round(offer_ratio * 100)}% of creatives are explicitly offer-led.",
                    "There is room to scale narrative/benefit-led variants.",
                    "A clearer testing plan can raise message quality without over-discounting.",
                ],
                "inconsistent value communication across campaigns",
            )
        )

    # 2) Creative repetition
    body_counter = Counter()
    for snap in snaps:
        body = _body_text(snap).strip()
        if body:
            body_counter[body] += 1
    repeated = body_counter.most_common(1)[0][1] if body_counter else 1
    repeated_ratio = repeated / max(len(snaps), 1)
    if repeated_ratio >= 0.35:
        findings.append(
            (
                "Creative memory loop looks shallow",
                [
                    f"Top body line repeats across ~{round(repeated_ratio * 100)}% of ads.",
                    "High repetition signals limited hook experimentation.",
                    "Variation likely happens in format more than message.",
                ],
                "learning slows while fatigue risk rises",
            )
        )
    else:
        findings.append(
            (
                "Some creative variation exists, but structure is unclear",
                [
                    "No single body line dominates heavily.",
                    "Variation should be tied to a planned sequence, not random refreshes.",
                    "Track winners by hook and audience stage, not just CPM/CTR.",
                ],
                "fragmented learnings across creatives",
            )
        )

    # 3) CTA concentration
    cta_counter = Counter()
    for snap in snaps:
        cta = str(snap.get("cta_text") or "").strip()
        if cta:
            cta_counter[cta] += 1
    if cta_counter:
        top_cta, top_cta_count = cta_counter.most_common(1)[0]
        top_cta_ratio = top_cta_count / max(len(snaps), 1)
        if top_cta_ratio >= 0.65:
            findings.append(
                (
                    "Journey orchestration is mostly single-step",
                    [
                        f"CTA '{top_cta}' appears in ~{round(top_cta_ratio * 100)}% of creatives.",
                        "Most ads push the same immediate action.",
                        "Little evidence of stage-based CTA progression.",
                    ],
                    "flat funnel behavior instead of guided progression",
                )
            )
        else:
            findings.append(
                (
                    "CTA mix is diversified",
                    [
                        f"Top CTA '{top_cta}' appears in ~{round(top_cta_ratio * 100)}% of creatives.",
                        "Multiple CTAs indicate some stage segmentation.",
                        "Next step is validating if CTA mix maps to audience intent.",
                    ],
                    "potential mismatch between CTA choice and user stage",
                )
            )

    # 4) Channel spread
    platform_set = set()
    for ad in ads:
        for p in ad.get("publisher_platform", []) or []:
            platform_set.add(p)
    if len(platform_set) >= 4:
        findings.append(
            (
                "Wide platform spread needs channel-specific positioning",
                [
                    f"Creatives are running across {len(platform_set)} Meta placements.",
                    "One generic message across all placements usually underperforms.",
                    "Each channel should carry tailored creative context and proof.",
                ],
                "reach scales faster than message-fit quality",
            )
        )
    else:
        findings.append(
            (
                "Narrow placement footprint",
                [
                    f"Active footprint appears concentrated to {len(platform_set)} placements.",
                    "Depth can be good, but dependence increases auction volatility.",
                    "Controlled expansion tests can improve resilience.",
                ],
                "media dependency on a small placement mix",
            )
        )

    # 5) Format dominance
    format_counter = Counter(str(s.get("display_format") or "UNKNOWN") for s in snaps)
    if format_counter:
        top_format, top_format_count = format_counter.most_common(1)[0]
        top_format_ratio = top_format_count / max(len(snaps), 1)
        if top_format_ratio >= 0.6:
            findings.append(
                (
                    "Format diversification is low",
                    [
                        f"'{top_format}' drives ~{round(top_format_ratio * 100)}% of sampled creatives.",
                        "Heavy single-format dependence can limit message depth.",
                        "Balanced format mix improves testing surface and recall.",
                    ],
                    "creative strategy over-indexes on one delivery format",
                )
            )
        else:
            findings.append(
                (
                    "Format mix is relatively balanced",
                    [
                        f"Top format '{top_format}' is ~{round(top_format_ratio * 100)}% of sample.",
                        "Good base for testing hooks by format-intent pair.",
                        "Main gap is likely in sequencing and measurement, not inventory breadth.",
                    ],
                    "experimentation exists but lacks structured memory",
                )
            )

    pitch = (
        "\"You're generating delivery across Meta, but strategy memory is still weak. "
        "Let's connect creative, CTA, and offer signals into one learning loop so each campaign compounds.\""
    )
    return findings[:5], pitch


def build_summary(company: str, payload: dict) -> str:
    ads = payload.get("ads", []) if isinstance(payload.get("ads", []), list) else []
    total_results = payload.get("search_information", {}).get("total_results", len(ads))
    start, end = flight_window(ads)
    platforms = summarize_platforms(ads)
    formats = summarize_formats(ads)
    findings, pitch = dynamic_findings(company, ads)

    findings_lines: list[str] = []
    for idx, (title, bullets, result) in enumerate(findings, 1):
        findings_lines.append(f"{idx}. ❌ {title}")
        findings_lines.extend(bullets)
        findings_lines.append("")
        findings_lines.append(f"👉 Result: {result}")
        findings_lines.append("")
    findings_block = "\n".join(findings_lines).rstrip()

    return f"""## {company}

🧾 Snapshot
Active creatives: ~{total_results} ads
Flight window: {start} -> {end} (continuous refresh)
Platforms: {platforms}
Formats: {formats}

🧠 What {company} is missing
{findings_block}

Pitch Angle
One-liner

{pitch}
"""


def load_brands_in_order(path: Path) -> list[str]:
    if not path.exists():
        return []
    brands: list[str] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            brand = (row.get("brand_name") or "").strip()
            if brand:
                brands.append(brand)
    return brands


def compile_ordered_sections(ordered_brands: list[str]) -> tuple[list[str], int]:
    compiled: list[str] = []
    missing_count = 0
    for brand in ordered_brands:
        path = OUTPUT_DIR / f"{json_stem(brand)}.json"
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            compiled.append(build_summary(brand, payload).strip())
        else:
            missing_count += 1
            compiled.append(
                f"""## {brand}

⚠️ No audit data available yet for this brand in `output/`.
"""
            )
    return compiled, missing_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Build per-brand summaries from Meta audit JSON.")
    parser.add_argument(
        "--append-to-combined",
        action="store_true",
        help="Append brands.csv-ordered summaries to output/all_company_summaries.md (does not replace file).",
    )
    args = parser.parse_args()

    json_files = sorted(OUTPUT_DIR.glob("*.json"))
    ordered_brands = load_brands_in_order(BRANDS_CSV)

    if json_files:
        for path in json_files:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            company = company_name_for_json_path(path, ordered_brands)
            summary = build_summary(company, payload)
            out_path = OUTPUT_DIR / f"{path.stem}_summary.md"
            out_path.write_text(summary, encoding="utf-8")
            print(f"Saved {out_path}")
    elif not args.append_to_combined:
        print("No JSON files found in output/.")
        return

    if not ordered_brands and json_files:
        ordered_brands = sorted(p.stem.replace("_", " ") for p in json_files)

    compiled, missing_count = compile_ordered_sections(ordered_brands)
    combined_path = OUTPUT_DIR / "all_company_summaries.md"
    body = "\n\n---\n\n".join(compiled) + "\n"

    if args.append_to_combined:
        if combined_path.exists():
            prev = combined_path.read_text(encoding="utf-8").rstrip()
            combined_path.write_text(prev + "\n\n---\n\n" + body, encoding="utf-8")
            print(f"Appended to {combined_path}")
        else:
            combined_path.write_text(body, encoding="utf-8")
            print(f"Created {combined_path}")
    else:
        combined_path.write_text(body, encoding="utf-8")
        print(f"Saved {combined_path}")

    print(f"Brands listed: {len(ordered_brands)} | Missing summaries: {missing_count}")


if __name__ == "__main__":
    main()
