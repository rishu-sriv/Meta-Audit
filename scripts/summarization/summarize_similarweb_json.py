"""
Build ad-focused markdown summaries from existing output/*_similarweb.json files
using the same template as similarweb_audit.build_ad_focused_summary.

Usage:
  python summarize_similarweb_json.py
  python summarize_similarweb_json.py --output-dir output
  python summarize_similarweb_json.py --only-usable
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from similarweb_audit import build_ad_focused_summary, has_usable_similarweb_data

_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = _REPO_ROOT / "output"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create ad-focused summaries from Similarweb JSON snapshots."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory containing *_similarweb.json (default: output/)",
    )
    parser.add_argument(
        "--only-usable",
        action="store_true",
        help="Skip JSON files with no usable channel data.",
    )
    parser.add_argument(
        "--combined-md",
        type=Path,
        default=None,
        help="Optional path to write one markdown file with all summaries (--- separated).",
    )
    args = parser.parse_args()

    out_dir = args.output_dir.expanduser().resolve()
    if not out_dir.is_dir():
        raise SystemExit(f"Not a directory: {out_dir}")

    paths = sorted(out_dir.glob("*_similarweb.json"))
    written = 0
    skipped = 0
    combined: list[str] = []

    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Skip (invalid JSON) {path.name}: {exc}")
            skipped += 1
            continue
        if args.only_usable and not has_usable_similarweb_data(payload):
            print(f"Skip (no usable data) {path.name}")
            skipped += 1
            continue

        summary = build_ad_focused_summary(payload)
        stem = path.name[: -len("_similarweb.json")]
        md_path = out_dir / f"{stem}_similarweb_summary.md"
        md_path.write_text(summary + "\n", encoding="utf-8")
        written += 1
        combined.append(f"## {payload.get('company', stem)}\n\n{summary}")

    if args.combined_md:
        args.combined_md.parent.mkdir(parents=True, exist_ok=True)
        args.combined_md.write_text("\n\n---\n\n".join(combined) + "\n", encoding="utf-8")
        print(f"Combined -> {args.combined_md}")

    print(f"Wrote {written} summary file(s) -> {out_dir}")
    print(f"Skipped: {skipped} | Input JSON files: {len(paths)}")


if __name__ == "__main__":
    main()
