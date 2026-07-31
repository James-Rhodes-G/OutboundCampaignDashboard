#!/usr/bin/env python3
"""Sync scripts/campaigns.json from {dataDir}/outbound_campaigns.csv."""

from __future__ import annotations

import csv
import json

from project_paths import (
    DEFAULT_CONFIG,
    OUTBOUND_CSV_NAME,
    load_config,
    relative_data_dir,
    resolve_data_dir,
)


def display_name(campaign_id: str) -> str:
    return campaign_id.replace("_", " ")


def strip_field(value: str) -> str:
    return value.replace("\r", "").strip()


def main() -> None:
    config = load_config()
    data_dir = resolve_data_dir(config)
    csv_path = data_dir / OUTBOUND_CSV_NAME
    if not csv_path.is_file():
        raise SystemExit(f"CSV not found: {csv_path}")

    campaigns = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            campaign_id = strip_field(row.get("name", ""))
            if not campaign_id or campaign_id == "name":
                continue
            guid = strip_field(row.get("guid", ""))
            entry = {
                "id": campaign_id,
                "name": display_name(campaign_id),
                "csv": f"{campaign_id}/conversation_timings.csv",
            }
            if guid:
                entry["genesys_guid"] = guid
            campaigns.append(entry)

    config["dataDir"] = relative_data_dir(data_dir)
    config["campaigns"] = campaigns
    DEFAULT_CONFIG.write_text(json.dumps(config, indent=4) + "\n")
    print(f"Updated {DEFAULT_CONFIG} with {len(campaigns)} campaign(s)")


if __name__ == "__main__":
    main()
