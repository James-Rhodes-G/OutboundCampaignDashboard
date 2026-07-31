#!/usr/bin/env python3
"""Build conversation_timings.csv for each configured campaign from conversations.jsonl."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from project_paths import ANALYSIS_SCRIPT, DEFAULT_CONFIG, load_config, resolve_data_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate conversation_timings.csv for all configured campaigns."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign", action="append", help="Campaign id (repeatable)")
    parser.add_argument("--all", action="store_true", help="Process all configured campaigns")
    parser.add_argument("--skip-missing", action="store_true", help="Skip campaigns with no jsonl")
    args = parser.parse_args()

    config = load_config(args.config)
    data_dir = resolve_data_dir(config)
    if not ANALYSIS_SCRIPT.is_file():
        print(f"Analysis script not found: {ANALYSIS_SCRIPT}", file=sys.stderr)
        return 1

    campaigns = config.get("campaigns", [])
    if args.all:
        selected = campaigns
    elif args.campaign:
        by_id = {item["id"]: item for item in campaigns}
        unknown = [campaign_id for campaign_id in args.campaign if campaign_id not in by_id]
        if unknown:
            print(f"Unknown campaign id(s): {', '.join(unknown)}", file=sys.stderr)
            return 1
        selected = [by_id[campaign_id] for campaign_id in args.campaign]
    else:
        print("Specify --all or --campaign <id>", file=sys.stderr)
        return 1

    built = 0
    for campaign in selected:
        campaign_id = campaign["id"]
        campaign_dir = data_dir / campaign_id
        jsonl_path = campaign_dir / "conversations.jsonl"
        if not jsonl_path.is_file():
            message = f"SKIP: {campaign_id} — no conversations.jsonl"
            if args.skip_missing:
                print(message)
                continue
            print(message, file=sys.stderr)
            return 1

        print(f"Building timings for {campaign_id}...")
        subprocess.run(
            [
                sys.executable,
                str(ANALYSIS_SCRIPT),
                "--input",
                str(jsonl_path),
                "--output-dir",
                str(campaign_dir),
            ],
            check=True,
        )
        built += 1

    print(f"Done. Built conversation_timings.csv for {built} campaign(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
