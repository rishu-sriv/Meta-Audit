"""
Collect Google Ads Transparency data for a company and summarize it.

Source:
https://adstransparency.google.com/?region=anywhere

Modes:
- Real mode: uses Playwright to open the site, search company, scrape visible page text.
- Mock mode: generates deterministic sample payload (for pipeline testing).

Outputs:
- output/{company}_google_ads.json
- output/{company}_google_ads_summary.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

OUTPUT_DIR = Path("output")
BASE_URL = "https://adstransparency.google.com/?region=anywhere"


def safe_stem(value: str) -> str:
    return value.strip().replace("/", "_").replace(" ", "_")


def _clean(s: str) -> str:
    return " ".join((s or "").split()).strip()


def normalize_domain(value: str) -> str:
    v = _clean(value).lower()
    if v.startswith("http://"):
        v = v[len("http://") :]
    if v.startswith("https://"):
        v = v[len("https://") :]
    if v.startswith("www."):
        v = v[4:]
    v = v.split("/")[0]
    return v


def domain_url(domain: str) -> str:
    return f"https://adstransparency.google.com/?region=anywhere&domain={domain}"


def parse_total_ads(page_text: str) -> int | None:
    patterns = [
        r"([0-9][0-9,]*)\s+ads?\b",
        r"showing\s+([0-9][0-9,]*)\b",
        r"results?\s*\(?([0-9][0-9,]*)\)?",
    ]
    t = page_text.lower()
    for p in patterns:
        m = re.search(p, t)
        if m:
            raw = m.group(1).replace(",", "")
            try:
                return int(raw)
            except ValueError:
                continue
    return None


def parse_platform_hints(page_text: str) -> list[str]:
    hints = []
    for token in (
        "Google Search",
        "YouTube",
        "Discover",
        "Display Network",
        "Gmail",
        "Google Maps",
    ):
        if token.lower() in page_text.lower():
            hints.append(token)
    return hints


def parse_date_range_hint(page_text: str) -> str | None:
    m = re.search(r"(Custom\s+[^\n]{3,80})", page_text, flags=re.IGNORECASE)
    if m:
        return _clean(m.group(1))
    m = re.search(r"(Any time)", page_text, flags=re.IGNORECASE)
    if m:
        return "Any time"
    return None


def parse_filter_hints(page_text: str) -> dict[str, str | None]:
    platforms = "All platforms" if "All platforms" in page_text else None
    formats = "All formats" if "All formats" in page_text else None
    return {"platform_filter": platforms, "format_filter": formats}


def parse_advertiser_mentions(page_text: str) -> dict[str, Any]:
    lines = [_clean(x) for x in page_text.splitlines() if _clean(x)]
    counts: dict[str, int] = {}
    for i, line in enumerate(lines):
        # Typical pattern in this UI: "<Advertiser Name>" followed by "Verified"
        if line == "Verified" and i > 0:
            advertiser = lines[i - 1]
            if len(advertiser) > 1 and advertiser not in {
                "PrivacyTermsAd PoliciesFAQs",
                "PrinciplesAds Blog",
            }:
                counts[advertiser] = counts.get(advertiser, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    hidden_ad_notices = page_text.count("Sorry, we're not able to show you this ad at this time")
    return {
        "top_advertisers_by_visible_mentions": [
            {"advertiser": name, "visible_mentions": cnt} for name, cnt in top
        ],
        "hidden_ad_notice_count": hidden_ad_notices,
    }


def _millis_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def extract_ads_from_api_responses(api_json_responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract ad-level records from SearchService/SearchCreatives payload.
    """
    ads: list[dict[str, Any]] = []
    for resp in api_json_responses:
        url = str(resp.get("url") or "")
        if "SearchService/SearchCreatives" not in url:
            continue
        body = resp.get("json")
        if not isinstance(body, dict):
            continue
        rows = body.get("1")
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            creative_payload = r.get("3") if isinstance(r.get("3"), dict) else {}
            image_html = None
            preview_js = None
            if isinstance(creative_payload.get("3"), dict):
                image_html = creative_payload["3"].get("2")
            if isinstance(creative_payload.get("1"), dict):
                preview_js = creative_payload["1"].get("4")

            media_url = None
            media_type = None
            if isinstance(image_html, str):
                m = re.search(r'src="([^"]+)"', image_html)
                if m:
                    media_url = m.group(1)
                    media_type = "image"
            if media_url is None and isinstance(preview_js, str):
                media_url = preview_js
                media_type = "html5_or_video"

            start_obj = r.get("6") if isinstance(r.get("6"), dict) else {}
            end_obj = r.get("7") if isinstance(r.get("7"), dict) else {}
            start_ms = start_obj.get("2") if isinstance(start_obj.get("2"), int) else None
            end_ms = end_obj.get("2") if isinstance(end_obj.get("2"), int) else None

            ads.append(
                {
                    "ad_record_id": r.get("1"),
                    "creative_id": r.get("2"),
                    "advertiser_name": r.get("12"),
                    "domain": r.get("14"),
                    "platform_code": r.get("13"),
                    "format_code": r.get("4"),
                    "start_ms": start_ms,
                    "start_at_utc": _millis_to_iso(start_ms),
                    "end_ms": end_ms,
                    "end_at_utc": _millis_to_iso(end_ms),
                    "media_type": media_type,
                    "media_url": media_url,
                }
            )
    # Deduplicate by creative_id when present.
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in ads:
        key = str(a.get("creative_id") or a.get("ad_record_id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(a)
    return out


def extract_ad_snippets(page_text: str, max_snippets: int = 25) -> list[str]:
    lines = [_clean(x) for x in page_text.splitlines() if _clean(x)]
    out: list[str] = []
    # Heuristic: ad creative lines often sit near these labels.
    trigger_words = (
        "sponsored",
        "ad",
        "learn more",
        "shop now",
        "visit site",
        "apply now",
        "book now",
        "watch now",
    )
    for i, line in enumerate(lines):
        low = line.lower()
        if any(t in low for t in trigger_words):
            start = max(0, i - 1)
            end = min(len(lines), i + 2)
            snippet = " | ".join(lines[start:end])
            if len(snippet) >= 25:
                out.append(snippet[:260])
        if len(out) >= max_snippets:
            break
    # de-dup preserve order
    seen = set()
    dedup = []
    for s in out:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(s)
    return dedup


def extract_ad_card_previews(page_text: str, max_cards: int = 80) -> list[dict[str, Any]]:
    """
    Heuristic extraction from visible text after entering "See all ads" view.
    Captures advertiser name (line before 'Verified') and nearby lines as preview text.
    """
    lines = [_clean(x) for x in page_text.splitlines() if _clean(x)]
    previews: list[dict[str, Any]] = []
    boilerplate = {
        "Ads Transparency Centre",
        "Sign in",
        "FAQs",
        "All topics",
        "Political ads",
        "search",
        "See all ads",
        "Discover more on related sites",
        "Analyse political ad campaigns on Google",
        "Explore political advertising on Google",
        "Go to My Ad Centre",
        "PrivacyTermsAd PoliciesFAQs",
        "PrinciplesAds Blog",
    }
    i = 0
    while i < len(lines):
        if lines[i] != "Verified":
            i += 1
            continue
        advertiser = lines[i - 1] if i > 0 else ""
        if not advertiser or advertiser in boilerplate:
            i += 1
            continue
        # collect a small preview window after "Verified" until next obvious boundary
        details: list[str] = []
        j = i + 1
        while j < len(lines) and len(details) < 6:
            token = lines[j]
            if token == "Verified":
                break
            if token in boilerplate:
                j += 1
                continue
            # stop if another likely advertiser line begins
            if j + 1 < len(lines) and lines[j + 1] == "Verified":
                break
            details.append(token)
            j += 1
        previews.append(
            {
                "advertiser": advertiser,
                "details": details,
                "details_text": " | ".join(details)[:400],
            }
        )
        if len(previews) >= max_cards:
            break
        i = j
    return previews


def build_summary(payload: dict[str, Any]) -> str:
    company = payload.get("company", "Unknown")
    lookback = payload.get("lookback_days", 90)
    total_ads = payload.get("estimated_ads_count")
    platforms = payload.get("platform_hints") or []
    snippets = payload.get("ad_snippets") or []
    platform_text = ", ".join(platforms) if platforms else "Not clearly surfaced"
    ads_text = str(total_ads) if total_ads is not None else "Not clearly surfaced"

    lines = [
        f"## Google Ads Snapshot — {company}",
        "",
        f"- **Lookback window requested:** {lookback} days",
        f"- **Estimated ad count (visible hint):** {ads_text}",
        f"- **Platform hints detected:** {platform_text}",
        "",
        "### Quick Interpretation",
        "- If ad count is high, the brand is likely sustaining active paid demand capture on Google inventory.",
        "- If platform hints include YouTube/Display, campaigns may include upper-mid funnel coverage beyond search intent.",
        "- Use snippets below to validate whether creative variety exists or messaging is repetitive.",
        "",
        "### Representative Visible Snippets",
    ]
    if snippets:
        for s in snippets[:12]:
            lines.append(f"- {s}")
    else:
        lines.append("- No reliable ad snippet lines were extracted from visible page text in this run.")
    return "\n".join(lines).strip() + "\n"


def build_mock_payload(company: str, lookback_days: int, domain: str) -> dict[str, Any]:
    seed = sum(ord(c) for c in company)
    total = 40 + (seed % 140)
    snippets = [
        f"{company} | Sponsored | Shop now | New arrivals",
        f"{company} | Ad | Limited-time offer | Learn more",
        f"{company} | Sponsored | Fast delivery | Visit site",
    ]
    return {
        "company": company,
        "source": "google_ads_transparency",
        "mode": "mock",
        "lookback_days": lookback_days,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "domain_url_used": domain_url(domain),
        "search_url_hint": f"{BASE_URL}&q={quote_plus(company)}",
        "estimated_ads_count": total,
        "platform_hints": ["Google Search", "YouTube"],
        "ad_snippets": snippets,
        "raw_page_text_excerpt": "Mock run: no live page scrape performed.",
    }


async def collect_live_payload(
    company: str,
    domain: str,
    lookback_days: int,
    timeout_ms: int,
    headless: bool,
    scroll_steps: int,
    capture_network_json: bool,
    max_network_json: int,
    ads_only: bool,
) -> dict[str, Any]:
    print(f"[1/5] Launching browser (headless={headless})...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, channel="chrome")
        context = await browser.new_context()
        page = await context.new_page()
        captured_api_json: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        response_tasks: list[asyncio.Task[Any]] = []

        async def _capture_response(resp: Any) -> None:
            if not capture_network_json:
                return
            if len(captured_api_json) >= max_network_json:
                return
            try:
                req = resp.request
                if req.resource_type not in {"xhr", "fetch"}:
                    return
                url = resp.url
                if url in seen_urls:
                    return
                headers = await resp.all_headers()
                ctype = (headers.get("content-type") or "").lower()
                if "json" not in ctype:
                    return
                # Keep only likely data endpoints.
                low_url = url.lower()
                if not (
                    "adstransparency" in low_url
                    or "googleapis" in low_url
                    or "google.com" in low_url
                ):
                    return
                body_json: Any
                try:
                    body_json = await resp.json()
                except Exception:
                    txt = await resp.text()
                    txt = txt[:50000]
                    body_json = {"_raw_text_excerpt": txt}
                captured_api_json.append(
                    {
                        "url": url,
                        "status": resp.status,
                        "method": req.method,
                        "content_type": ctype,
                        "json": body_json,
                    }
                )
                seen_urls.add(url)
            except Exception:
                return

        if capture_network_json:
            page.on("response", lambda r: response_tasks.append(asyncio.create_task(_capture_response(r))))

        domain_target = domain_url(domain)
        print(f"[2/5] Opening Ads Transparency domain URL: {domain_target}")
        await page.goto(domain_target, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(3000)

        # Try to ensure domain results context is loaded.
        print(f"[3/5] Validating domain-results context for: {domain}")
        search_selectors = [
            "input[type='search']",
            "input[aria-label*='Search']",
            "input[placeholder*='Search']",
            "input[placeholder*='advertiser']",
            "input[role='combobox']",
        ]
        typed = False
        for sel in search_selectors:
            locator = page.locator(sel).first
            if await locator.count() > 0:
                try:
                    await locator.fill(domain)
                    await locator.press("Enter")
                    typed = True
                    break
                except Exception:
                    continue

        # Enter ad list view.
        print("[4/6] Trying to open 'See all ads'...")
        opened_ads_view = False
        for label in ("See all ads", "See all ad", "View all ads"):
            btn = page.get_by_text(label, exact=False).first
            if await btn.count() > 0:
                try:
                    await btn.click(timeout=5000)
                    opened_ads_view = True
                    break
                except Exception:
                    continue
        if opened_ads_view:
            await page.wait_for_timeout(2500)

        # Let results render; then slow-scroll to load additional cards.
        print("[4/5] Waiting for results to render...")
        await page.wait_for_timeout(8000)
        if scroll_steps > 0:
            print(f"[4/5] Scrolling to load more ads (steps={scroll_steps})...")
            for _ in range(scroll_steps):
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight * 0.8)")
                await page.wait_for_timeout(1400)
        # Give network listeners a moment to collect trailing XHR/fetch payloads.
        await page.wait_for_timeout(1500)
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        page_text = await page.inner_text("body")
        domain_present = domain.lower() in page_text.lower()
        ads_count = parse_total_ads(page_text)
        if not domain_present and ads_count is None:
            print(
                "[4/5] Warning: domain context not detected in visible text. "
                "Captured page may be generic landing content."
            )

        advertiser_info = parse_advertiser_mentions(page_text)
        filter_hints = parse_filter_hints(page_text)
        card_previews = extract_ad_card_previews(page_text, max_cards=120)
        creative_like = [
            p["details_text"]
            for p in card_previews
            if isinstance(p, dict) and p.get("details_text")
        ][:40]
        ad_level = extract_ads_from_api_responses(captured_api_json)
        payload = {
            "company": company,
            "source": "google_ads_transparency",
            "mode": "live",
            "lookback_days": lookback_days,
            "collected_at_utc": datetime.now(timezone.utc).isoformat(),
            "base_url": BASE_URL,
            "domain": domain,
            "domain_url_used": domain_target,
            "search_url_hint": f"{BASE_URL}&q={quote_plus(domain)}",
            "domain_context_detected": domain_present,
            "estimated_ads_count": ads_count,
            "date_range_hint": parse_date_range_hint(page_text),
            "opened_see_all_ads": opened_ads_view,
            **filter_hints,
            "platform_hints": parse_platform_hints(page_text),
            **advertiser_info,
            "ads_count_extracted": len(ad_level),
            "ads": ad_level,
        }
        if not ads_only:
            payload.update(
                {
                    "ad_snippets": creative_like,
                    "ad_card_previews": card_previews,
                    "page_feature_snippets": extract_ad_snippets(page_text, max_snippets=30),
                    "raw_page_text_excerpt": page_text[:20000],
                    "api_json_responses": captured_api_json,
                }
            )

        print("[6/6] Closing browser and finalizing payload...")
        await context.close()
        await browser.close()
        return payload


def write_outputs(company: str, payload: dict[str, Any]) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(company)
    json_path = OUTPUT_DIR / f"{stem}_google_ads.json"
    summary_path = OUTPUT_DIR / f"{stem}_google_ads_summary.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path.write_text(build_summary(payload), encoding="utf-8")
    return json_path, summary_path


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Collect Google Ads Transparency data + summary.")
    parser.add_argument("--company", required=True, help="Company/advertiser name, e.g. pokonut")
    parser.add_argument(
        "--domain",
        default=None,
        help="Website domain for domain URL mode, e.g. pokonut.com (default: derived from company).",
    )
    parser.add_argument("--lookback-days", type=int, default=90, help="Requested lookback window in days (default: 90).")
    parser.add_argument("--timeout-ms", type=int, default=120_000, help="Navigation timeout in milliseconds.")
    parser.add_argument("--mock", action="store_true", help="Run in mock mode (no live scrape).")
    parser.add_argument("--headless", action="store_true", help="Run browser headless in live mode.")
    parser.add_argument(
        "--scroll-steps",
        type=int,
        default=8,
        help="How many scroll passes to load more ad cards in live mode (default: 8).",
    )
    parser.add_argument(
        "--capture-network-json",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Capture JSON responses from XHR/Fetch requests (default: true).",
    )
    parser.add_argument(
        "--max-network-json",
        type=int,
        default=20,
        help="Max number of API JSON responses to store (default: 20).",
    )
    parser.add_argument(
        "--ads-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep only ad-level extracted records in output (default: true).",
    )
    args = parser.parse_args()
    domain = normalize_domain(args.domain or f"{args.company}.com")
    company_label = args.company if args.company else domain

    try:
        if args.mock:
            print("Running in mock mode...")
            payload = build_mock_payload(company_label, args.lookback_days, domain)
        else:
            payload = await collect_live_payload(
                company_label,
                domain,
                args.lookback_days,
                args.timeout_ms,
                args.headless,
                args.scroll_steps,
                args.capture_network_json,
                args.max_network_json,
                args.ads_only,
            )
    except PlaywrightTimeoutError as exc:
        raise SystemExit(
            f"Timed out while loading Google Ads Transparency. "
            f"Try increasing --timeout-ms (current: {args.timeout_ms}).\nDetails: {exc}"
        ) from exc
    json_path, summary_path = write_outputs(args.company, payload)
    print(f"Saved JSON -> {json_path}")
    print(f"Saved summary -> {summary_path}")
    print(
        "Snapshot:",
        f"estimated_ads_count={payload.get('estimated_ads_count')},",
        f"snippets={len(payload.get('ad_snippets') or [])},",
        f"platform_hints={len(payload.get('platform_hints') or [])}",
    )


if __name__ == "__main__":
    asyncio.run(main_async())

