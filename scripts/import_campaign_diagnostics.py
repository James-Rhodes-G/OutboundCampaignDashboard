#!/usr/bin/env python3
"""Import Genesys campaign diagnostic summaries into SQLite."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from project_paths import DEFAULT_CONFIG, DEFAULT_DB, load_config, resolve_data_dir

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS campaign_diagnostics (
    campaign_id     TEXT PRIMARY KEY,
    genesys_guid    TEXT,
    fetched_at      TEXT,
    health_state    TEXT,
    campaign_state  TEXT,
    summary_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaign_diagnostics_fetched
    ON campaign_diagnostics(fetched_at);
"""


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    if table_exists(conn, "campaigns"):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)")}
        if "genesys_guid" not in columns:
            conn.execute("ALTER TABLE campaigns ADD COLUMN genesys_guid TEXT")
    conn.commit()


def import_file(conn: sqlite3.Connection, path: Path) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    campaign_id = record.get("campaign_id") or path.stem
    genesys_guid = record.get("genesys_guid")
    fetched_at = record.get("fetched_at")
    health_state = record.get("health_state")
    campaign_state = record.get("campaign_state")
    summary = record.get("summary", record)
    summary_json = json.dumps(summary, sort_keys=True)

    conn.execute(
        """
        INSERT INTO campaign_diagnostics (
            campaign_id, genesys_guid, fetched_at, health_state, campaign_state, summary_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(campaign_id) DO UPDATE SET
            genesys_guid = excluded.genesys_guid,
            fetched_at = excluded.fetched_at,
            health_state = excluded.health_state,
            campaign_state = excluded.campaign_state,
            summary_json = excluded.summary_json
        """,
        (campaign_id, genesys_guid, fetched_at, health_state, campaign_state, summary_json),
    )
    if genesys_guid and table_exists(conn, "campaigns"):
        conn.execute(
            "UPDATE campaigns SET genesys_guid = ? WHERE id = ?",
            (genesys_guid, campaign_id),
        )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import campaign diagnostic JSON into SQLite.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--campaign", action="append")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--skip-missing", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    data_dir = resolve_data_dir(config)
    diag_dir = data_dir / "campaign_diagnostics"
    if not diag_dir.is_dir():
        print(f"Diagnostics directory not found: {diag_dir}", file=sys.stderr)
        return 1

    if args.all:
        selected = [item["id"] for item in config.get("campaigns", [])]
    elif args.campaign:
        selected = args.campaign
    else:
        selected = [item["id"] for item in config.get("campaigns", [])]

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        imported = 0
        for campaign_id in selected:
            path = diag_dir / f"{campaign_id}.json"
            if not path.is_file():
                if args.skip_missing:
                    print(f"SKIP: {campaign_id} — {path.name} not found")
                    continue
                print(f"ERROR: missing {path}", file=sys.stderr)
                return 1
            import_file(conn, path)
            imported += 1
        conn.commit()
        print(f"Imported {imported} campaign diagnostic summary(ies) into {args.db}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
