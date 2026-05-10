"""
Similarweb lead data collector.

Purpose:
- Open Similarweb traffic overview for a given company/domain.
- Extract channel totals and top traffic sources from the rendered page text.
- Save the result in output/{company}_similarweb.json.

Notes:
- Similarweb Pro requires authentication; run this script while logged in.
- The parser is text-based so it can survive moderate UI changes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.parse import urlparse

from playwright.async_api import async_playwright

OUTPUT_DIR = Path("output")
DEFAULT_PROFILE_DIR = Path(".playwright-chrome-profile")
SYSTEM_CHROME_USER_DATA_DIR = Path.home() / "Library/Application Support/Google/Chrome"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
SIMILARWEB_BASE = (
    "https://pro.similarweb.com/#/digitalsuite/websiteanalysis/traffic-overview/"
    "*/999/3m/?category=no-category&webSource=Total&key={domain}"
)

CHANNEL_LABELS = [
    "Direct",
    "Search - Organic",
    "Search - Paid",
    "Referrals",
    "Display Ads",
    "Social - Organic",
    "Social - Paid",
    "Gen AI",
    "Email",
    "Affiliates",
]


def safe_stem(value: str) -> str:
    return value.strip().replace("/", "_").replace(" ", "_")


def normalize_domain_key(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        parsed = urlparse(s)
        s = parsed.netloc or parsed.path
    s = s.split("/")[0].strip()
    if s.startswith("www."):
        s = s[4:]
    return s


def build_domain_candidates(
    row: dict[str, Any],
    company_column: str,
    domain_column: str | None,
    domain_fallback_column: str | None,
    include_merchant_fallback: bool,
) -> list[str]:
    candidates: list[str] = []

    if domain_column:
        domain_value = normalize_domain_key(str(row.get(domain_column) or ""))
        if domain_value:
            candidates.append(domain_value)

    if domain_fallback_column:
        fallback_value = normalize_domain_key(str(row.get(domain_fallback_column) or ""))
        if fallback_value:
            candidates.append(fallback_value)

    if include_merchant_fallback:
        company = (row.get(company_column) or "").strip().lower()
        if company:
            compact = re.sub(r"[^a-z0-9]+", "", company)
            dashed = re.sub(r"[^a-z0-9]+", "-", company).strip("-")
            for suffix in (".com", ".in"):
                if compact:
                    candidates.append(compact + suffix)
                if dashed:
                    candidates.append(dashed + suffix)
            candidates.append(company)

    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def parse_count(value: str) -> int | None:
    normalized = value.strip().upper().replace(",", "")
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)([KMB])?", normalized)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2)
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(number * multiplier)


def parse_percent(value: str) -> float | None:
    cleaned = value.strip().replace("%", "")
    if cleaned.startswith("<"):
        cleaned = cleaned[1:].strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_total_visits(page_text: str) -> str | None:
    match = re.search(r"Total visits\s+([0-9][0-9\.,]*[KMB]?)", page_text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def parse_channels(page_text: str) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    for label in CHANNEL_LABELS:
        # Works for lines like: "Direct\n3.501M" or "Direct 3.501M".
        pattern = re.compile(rf"{re.escape(label)}\s+([0-9][0-9\.,]*[KMB]?)", re.IGNORECASE)
        match = pattern.search(page_text)
        if not match:
            continue
        raw = match.group(1)
        channels.append(
            {
                "channel": label,
                "visits_raw": raw,
                "visits": parse_count(raw),
            }
        )
    return channels


def parse_top_sources(page_text: str, limit: int = 20) -> list[dict[str, Any]]:
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    sources: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        if line in CHANNEL_LABELS:
            continue
        if not re.search(r"[A-Za-z]", line):
            continue
        # Heuristic: source rows are followed by a percent line.
        if idx + 1 >= len(lines):
            continue
        pct_line = lines[idx + 1]
        if not re.fullmatch(r"<?\s*[0-9]+(?:\.[0-9]+)?\s*%", pct_line):
            continue
        # Avoid obvious non-source headings.
        if line.lower() in {
            "channels",
            "traffic sources",
            "channel traffic",
            "all traffic",
            "worldwide",
            "month-to-date",
        }:
            continue
        sources.append(
            {
                "source": line,
                "share_percent_raw": pct_line,
                "share_percent": parse_percent(pct_line),
            }
        )
        if len(sources) >= limit:
            break
    # Deduplicate while preserving order.
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in sources:
        key = row["source"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def cdp_is_reachable(cdp_url: str) -> bool:
    probe_url = cdp_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(probe_url, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _quit_all_chrome() -> None:
    # Graceful quit first.
    subprocess.run(
        ["osascript", "-e", 'tell application "Google Chrome" to quit'],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    # Force kill any lingering helpers.
    subprocess.run(["pkill", "-f", "Google Chrome"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)


def start_chrome_with_cdp(profile_name: str, port: int, force_restart: bool) -> str:
    cdp_url = f"http://127.0.0.1:{port}"
    if force_restart:
        _quit_all_chrome()
    chrome_bin = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    cmd = [
        chrome_bin,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={SYSTEM_CHROME_USER_DATA_DIR}",
        f"--profile-directory={profile_name}",
    ]
    # Start directly from binary to ensure debugging flags are applied.
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        if cdp_is_reachable(cdp_url):
            return cdp_url
        time.sleep(0.75)
    return ""


def list_chrome_profiles(user_data_dir: Path) -> list[dict[str, str]]:
    local_state_path = user_data_dir / "Local State"
    if not local_state_path.exists():
        return []
    try:
        data = json.loads(local_state_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    info_cache = data.get("profile", {}).get("info_cache", {})
    rows: list[dict[str, str]] = []
    if not isinstance(info_cache, dict):
        return rows
    for directory, details in info_cache.items():
        if not isinstance(details, dict):
            continue
        rows.append(
            {
                "directory": directory,
                "name": str(details.get("name") or ""),
                "email": str(details.get("user_name") or ""),
            }
        )
    return rows


async def collect_similarweb_data(
    domain: str,
    timeout_ms: int,
    profile_dir: Path,
    profile_name: str,
    manual_login: bool,
    cdp_url: str | None,
    auto_start_cdp: bool,
    cdp_port: int,
    force_restart_chrome: bool,
) -> dict[str, Any]:
    url = SIMILARWEB_BASE.format(domain=quote(domain.strip()))
    async with async_playwright() as p:
        browser = None
        connect_url = cdp_url.strip() if cdp_url else None
        if not connect_url and auto_start_cdp:
            started = start_chrome_with_cdp(
                profile_name=profile_name,
                port=cdp_port,
                force_restart=force_restart_chrome,
            )
            if started:
                connect_url = started
            else:
                if force_restart_chrome:
                    print(
                        "CDP did not come up after restart; "
                        "falling back to direct persistent launch for this run."
                    )
                else:
                    raise RuntimeError(
                        "Could not auto-start Chrome with CDP.\n"
                        f"Tried launching profile '{profile_name}' on port {cdp_port}, but "
                        f"{DEFAULT_CDP_URL}/json/version is still unreachable.\n"
                        "Do this once manually, then rerun:\n"
                        '  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
                        f'--remote-debugging-port={cdp_port} --profile-directory="{profile_name}"'
                    )
        if connect_url:
            if not cdp_is_reachable(connect_url):
                raise RuntimeError(
                    "Could not connect to Chrome CDP endpoint.\n"
                    f"Tried: {connect_url}/json/version\n"
                    "Start Chrome with remote debugging enabled, then retry.\n"
                    "Example:\n"
                    '  open -na "Google Chrome" --args --remote-debugging-port=9222 --profile-directory="Rishu"\n'
                    "Then run:\n"
                    "  python similarweb_audit.py --company gonoise.com --connect-cdp-url http://127.0.0.1:9222"
                )
            browser = await p.chromium.connect_over_cdp(connect_url)
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=False,
                    channel="chrome",
                    args=[f"--profile-directory={profile_name}"],
                )
            except Exception as exc:
                msg = str(exc)
                if "SingletonLock" in msg or "in use" in msg.lower() or "locked" in msg.lower():
                    fallback_url = DEFAULT_CDP_URL
                    if auto_start_cdp and not cdp_is_reachable(fallback_url):
                        started = start_chrome_with_cdp(
                            profile_name=profile_name,
                            port=cdp_port,
                            force_restart=force_restart_chrome,
                        )
                        if started:
                            fallback_url = started
                    if cdp_is_reachable(fallback_url):
                        print(
                            "Profile is locked by Chrome. "
                            f"Auto-falling back to CDP attach at {fallback_url}."
                        )
                        browser = await p.chromium.connect_over_cdp(fallback_url)
                        if browser.contexts:
                            context = browser.contexts[0]
                        else:
                            context = await browser.new_context()
                        page = context.pages[0] if context.pages else await context.new_page()
                        connect_url = fallback_url
                        # Skip raising; continue with attached context.
                        pass
                    else:
                        print(
                            "Chrome default profile is locked and CDP attach is unavailable.\n"
                            "Falling back to dedicated Playwright profile for this run: "
                            f"{DEFAULT_PROFILE_DIR}"
                        )
                        context = await p.chromium.launch_persistent_context(
                            user_data_dir=str(DEFAULT_PROFILE_DIR),
                            headless=False,
                            channel="chrome",
                        )
                else:
                    raise
            if not browser:
                page = context.pages[0] if context.pages else await context.new_page()

        print(f"Opening Similarweb for: {domain}")
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(6_000)

        if manual_login:
            print(
                "\nManual step:\n"
                "1) If Similarweb asks login, complete login in this Chrome window.\n"
                "2) Confirm the traffic overview is visible.\n"
                "3) Come back here and press Enter to extract data.\n"
            )
            input("Press Enter when the page is ready for scraping...")
            await page.wait_for_timeout(2_000)
        else:
            await page.wait_for_timeout(10_000)

        page_text = await page.inner_text("body")
        if "Login" in page_text and "Similarweb" in page_text:
            print("Looks like you may not be logged in. Please log in and rerun.")

        result = {
            "company": domain,
            "similarweb_url": url,
            "collected_at_utc": datetime.now(timezone.utc).isoformat(),
            "total_visits_raw": parse_total_visits(page_text),
            "channels": parse_channels(page_text),
            "top_traffic_sources": parse_top_sources(page_text, limit=30),
            "notes": [
                "Data extracted from rendered Similarweb page text.",
                "Values may vary with account permissions, region, and selected date filters.",
            ],
            "raw_page_text": page_text,
        }

        if connect_url:
            if browser:
                await browser.close()
        else:
            await context.close()
        return result


def write_output(company: str, payload: dict[str, Any]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{safe_stem(company)}_similarweb.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _channel_visits(payload: dict[str, Any], label: str) -> int:
    channels = payload.get("channels", [])
    if not isinstance(channels, list):
        return 0
    for ch in channels:
        if isinstance(ch, dict) and str(ch.get("channel", "")).lower() == label.lower():
            try:
                return int(ch.get("visits") or 0)
            except Exception:
                return 0
    return 0


def _fmt_millions(visits: int) -> str:
    if visits <= 0:
        return "0"
    if visits >= 1_000_000:
        return f"{visits / 1_000_000:.2f}M"
    return f"{visits:,}"


def build_ad_focused_summary(payload: dict[str, Any]) -> str:
    company = str(payload.get("company") or "this company")
    total_raw = str(payload.get("total_visits_raw") or "N/A")
    paid_search = _channel_visits(payload, "Search - Paid")
    display_ads = _channel_visits(payload, "Display Ads")
    paid_social = _channel_visits(payload, "Social - Paid")
    organic_search = _channel_visits(payload, "Search - Organic")
    direct = _channel_visits(payload, "Direct")
    ad_total = paid_search + display_ads + paid_social

    lines = [
        "📊 1. Total traffic from ads (big picture)",
        "",
        "The page shows major ad-driven channels:",
        f"- Search - Paid: {_fmt_millions(paid_search)} visits",
        f"- Display Ads: {_fmt_millions(display_ads)} visits",
        f"- Social - Paid: {_fmt_millions(paid_social)} visits",
        "",
        f"👉 Combined, ads are driving ~{_fmt_millions(ad_total)}+ visits out of {total_raw} total traffic.",
        "",
        "🔎 2. Paid Search (Performance Marketing)",
        f"- {_fmt_millions(paid_search)} visits (high intent channel)",
        "- Likely captures bottom-of-funnel demand through Google Ads and branded/non-branded terms.",
        "",
        "🖼️ 3. Display Ads (Awareness + Retargeting)",
        f"- {_fmt_millions(display_ads)} visits",
        "- Suggests active retargeting and broader reach via publisher/programmatic inventory.",
        "",
        "📱 4. Paid Social",
        f"- {_fmt_millions(paid_social)} visits",
        "- Indicates meaningful social ad activity (typically Meta and similar paid social placements).",
        "",
        "📈 5. Ad ecosystem signals",
        "- Mix shows multi-channel paid acquisition rather than single-channel dependence.",
        f"- Non-paid base is also strong (Organic Search: {_fmt_millions(organic_search)}, Direct: {_fmt_millions(direct)}).",
        "",
        "🧠 Strategic takeaway",
        f"{company} appears performance-led: Paid Search + Display form the backbone, with Paid Social as a strong support channel.",
    ]
    return "\n".join(lines).strip()


def has_usable_similarweb_data(payload: dict[str, Any]) -> bool:
    channels = payload.get("channels")
    if not isinstance(channels, list) or not channels:
        return False
    non_zero_channels = 0
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        try:
            if int(ch.get("visits") or 0) > 0:
                non_zero_channels += 1
        except Exception:
            continue
    return non_zero_channels > 0


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Collect Similarweb lead data for one company/domain.")
    parser.add_argument(
        "--company",
        required=False,
        help="Company/domain key used in Similarweb URL, e.g. gonoise.com",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=120_000,
        help="Navigation timeout in milliseconds (default: 120000).",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(
            os.getenv(
                "SIMILARWEB_PROFILE_DIR",
                str(SYSTEM_CHROME_USER_DATA_DIR if SYSTEM_CHROME_USER_DATA_DIR.exists() else DEFAULT_PROFILE_DIR),
            )
        ),
        help="Persistent Chrome profile path for Playwright. Reuses login session between runs.",
    )
    parser.add_argument(
        "--profile-name",
        default=os.getenv("SIMILARWEB_PROFILE_NAME", "Default"),
        help="Chrome profile directory name inside user-data-dir (e.g. Default, Profile 1).",
    )
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="Pause after opening the page so you can log in manually, then press Enter to continue.",
    )
    parser.add_argument(
        "--connect-cdp-url",
        default=os.getenv("SIMILARWEB_CHROME_CDP_URL"),
        help=(
            "Attach to an already-running Chrome via CDP, e.g. http://127.0.0.1:9222. "
            "Use this when your normal Chrome profile is already open/logged in."
        ),
    )
    parser.add_argument(
        "--auto-start-cdp",
        action="store_true",
        help="On profile lock, attempt to start Chrome with remote debugging and auto-attach.",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=9222,
        help="CDP port used for auto-start/attach fallback (default: 9222).",
    )
    parser.add_argument(
        "--force-restart-chrome",
        action="store_true",
        help="Before auto-start CDP, close all running Chrome processes and relaunch with debugging port.",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List available Chrome profiles from --profile-dir and exit.",
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        default=None,
        help="Optional CSV path to process many companies.",
    )
    parser.add_argument(
        "--company-column",
        default="company_name",
        help="CSV column with company/domain values (default: company_name).",
    )
    parser.add_argument(
        "--summary-column",
        default="similarweb_ad_summary",
        help="CSV column to write ad-focused summary into (default: similarweb_ad_summary).",
    )
    parser.add_argument(
        "--domain-column",
        default="domain",
        help="Preferred CSV column for website domain key (default: domain).",
    )
    parser.add_argument(
        "--domain-fallback-column",
        default="domain_tld1",
        help="Secondary domain column used when --domain-column is empty (default: domain_tld1).",
    )
    parser.add_argument(
        "--include-merchant-derived-keys",
        action="store_true",
        help="Also try merchant-name derived keys (.com/.in/plain). Off by default to avoid multiple retries.",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Output CSV path for batch mode (default: <input>_with_similarweb.csv).",
    )
    parser.add_argument(
        "--skip-no-data",
        action="store_true",
        help="In batch mode, if Similarweb data is not visible/usable, write skip note and continue.",
    )
    parser.add_argument(
        "--start-from-company",
        default=None,
        help="In batch mode, skip rows until this company name (case-insensitive exact match).",
    )
    parser.add_argument(
        "--filter-by-audit-summary",
        action="store_true",
        help=(
            "In batch mode, only process rows where --audit-filter-column is empty "
            "or starts with --audit-filter-prefix."
        ),
    )
    parser.add_argument(
        "--audit-filter-column",
        default="Audit Summary",
        help="Column checked by --filter-by-audit-summary (default: Audit Summary).",
    )
    parser.add_argument(
        "--audit-filter-prefix",
        default="No audit summary",
        help="Case-insensitive prefix used by --filter-by-audit-summary (default: No audit summary).",
    )
    args = parser.parse_args()

    if args.list_profiles:
        rows = list_chrome_profiles(args.profile_dir)
        if not rows:
            print(f"No profiles found in: {args.profile_dir}")
            return
        print(f"Chrome profiles in: {args.profile_dir}\n")
        for row in rows:
            email = row["email"] or "(no signed-in email)"
            print(f"- {row['directory']}: {row['name']} | {email}")
        return

    if args.input_csv:
        input_csv = args.input_csv.expanduser().resolve()
        if not input_csv.is_file():
            raise SystemExit(f"CSV not found: {input_csv}")
        with input_csv.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise SystemExit("CSV has no header row.")
            fieldnames = list(reader.fieldnames)
            if args.company_column not in fieldnames:
                raise SystemExit(
                    f"Column {args.company_column!r} not found. Available: {', '.join(fieldnames)}"
                )
            if args.summary_column not in fieldnames:
                fieldnames.append(args.summary_column)
            rows = list(reader)

        if args.filter_by_audit_summary and args.audit_filter_column not in fieldnames:
            raise SystemExit(
                f"Column {args.audit_filter_column!r} not found for filtering. "
                f"Available: {', '.join(fieldnames)}"
            )

        processed = 0
        skipped = 0
        started = args.start_from_company is None
        start_key = (args.start_from_company or "").strip().lower()
        seen_keys: set[str] = set()
        for idx, row in enumerate(rows, start=1):
            company = (row.get(args.company_column) or "").strip()
            if not company:
                row[args.summary_column] = ""
                skipped += 1
                continue

            if not started:
                if company.strip().lower() == start_key:
                    started = True
                else:
                    skipped += 1
                    continue

            if args.filter_by_audit_summary:
                audit_value = (row.get(args.audit_filter_column) or "").strip()
                wanted = (not audit_value) or audit_value.lower().startswith(args.audit_filter_prefix.lower())
                if not wanted:
                    skipped += 1
                    continue

            processed += 1
            print(f"[{processed}] Collecting Similarweb data for {company}")
            try:
                domain_candidates = build_domain_candidates(
                    row=row,
                    company_column=args.company_column,
                    domain_column=args.domain_column if args.domain_column in fieldnames else None,
                    domain_fallback_column=(
                        args.domain_fallback_column if args.domain_fallback_column in fieldnames else None
                    ),
                    include_merchant_fallback=args.include_merchant_derived_keys,
                )
                if not domain_candidates:
                    row[args.summary_column] = "No audit summary: missing domain/domain_tld1."
                    print(f"  ↷ Skipped (no domain key): {company}")
                    continue

                primary_key = domain_candidates[0]
                if primary_key in seen_keys:
                    print(f"  ↷ Skipped duplicate domain key: {primary_key}")
                    skipped += 1
                    continue
                seen_keys.add(primary_key)

                payload = None
                for domain_key in domain_candidates:
                    print(f"  → trying key: {domain_key}")
                    probe = await collect_similarweb_data(
                        domain=domain_key,
                        timeout_ms=args.timeout_ms,
                        profile_dir=args.profile_dir,
                        profile_name=args.profile_name,
                        manual_login=args.manual_login,
                        cdp_url=args.connect_cdp_url,
                        auto_start_cdp=args.auto_start_cdp,
                        cdp_port=args.cdp_port,
                        force_restart_chrome=args.force_restart_chrome,
                    )
                    if has_usable_similarweb_data(probe):
                        payload = probe
                        break
                if payload is None:
                    if args.skip_no_data:
                        row[args.summary_column] = (
                            "No audit summary: Similarweb data not visible/available for this company in current view."
                        )
                        print(f"  ↷ Skipped (no usable data): {company}")
                        continue
                    raise RuntimeError("No usable Similarweb data for all attempted domain keys.")
                if args.skip_no_data and not has_usable_similarweb_data(payload):
                    row[args.summary_column] = (
                        "No audit summary: Similarweb data not visible/available for this company in current view."
                    )
                    print(f"  ↷ Skipped (no usable data): {company}")
                    continue
                write_output(company, payload)
                row[args.summary_column] = build_ad_focused_summary(payload)
            except Exception as exc:
                row[args.summary_column] = f"No audit summary: Similarweb collection failed ({exc})."
                print(f"  ✗ Failed for {company}: {exc}")
                continue

        output_csv = (
            args.output_csv.expanduser().resolve()
            if args.output_csv
            else input_csv.with_name(f"{input_csv.stem}_with_similarweb{input_csv.suffix}")
        )
        with output_csv.open("w", newline="", encoding="utf-8") as wf:
            writer = csv.DictWriter(wf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        print(f"Wrote updated CSV -> {output_csv}")
        print(f"Processed rows: {processed} | Skipped rows: {skipped}")
        return

    if not args.company:
        raise SystemExit("Provide either --company or --input-csv.")

    payload = await collect_similarweb_data(
        domain=args.company,
        timeout_ms=args.timeout_ms,
        profile_dir=args.profile_dir,
        profile_name=args.profile_name,
        manual_login=args.manual_login,
        cdp_url=args.connect_cdp_url,
        auto_start_cdp=args.auto_start_cdp,
        cdp_port=args.cdp_port,
        force_restart_chrome=args.force_restart_chrome,
    )
    output_path = write_output(args.company, payload)
    summary = build_ad_focused_summary(payload)
    summary_path = OUTPUT_DIR / f"{safe_stem(args.company)}_similarweb_summary.md"
    summary_path.write_text(summary + "\n", encoding="utf-8")
    print(f"Saved Similarweb data -> {output_path}")
    print(f"Saved ad-focused summary -> {summary_path}")
    print(
        "Extracted:",
        f"{len(payload.get('channels', []))} channels,",
        f"{len(payload.get('top_traffic_sources', []))} traffic sources.",
    )


if __name__ == "__main__":
    asyncio.run(main_async())
