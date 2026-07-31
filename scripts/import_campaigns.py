#!/usr/bin/env python3
"""
Stream conversation_timings.csv files into a SQLite database for the Node-RED dashboard.

Designed for large files (millions of rows) — reads CSV row-by-row and inserts in batches.

Usage:
  python3 scripts/import_campaigns.py --all
  python3 scripts/import_campaigns.py --campaign EXAMPLE_CAMPAIGN
  python3 scripts/import_campaigns.py --campaign EXAMPLE_CAMPAIGN --db data/outbound_dashboard.db
  python3 scripts/import_campaigns.py --list
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from project_paths import DEFAULT_CONFIG, DEFAULT_DB, load_config as _load_config, resolve_data_dir

# CSV columns we persist (must match conversation_timings.csv header)
CSV_COLUMNS = [
    "file",
    "conversation_id",
    "conversation_start",
    "conversation_end",
    "conversation_duration_seconds",
    "customer_participant_name",
    "customer_connected_time",
    "customer_connected_source",
    "speech_detected_start",
    "speech_detected_end",
    "preconnect_duration_seconds",
    "total_ringbacks",
    "line_connected",
    "disposition_name",
    "disposition_analyzer",
    "speech_detected_participant_name",
    "speech_detected_source",
    "ivr_participant_name",
    "ivr_start_time",
    "ivr_start_source",
    "acd_participant_name",
    "acd_offer_time",
    "acd_offer_source",
    "agent_participant_name",
    "agent_alert_time",
    "agent_alert_source",
    "agent_connected_time",
    "agent_connected_source",
    "dial_start_time",
    "dial_start_source",
    "dial_to_customer_seconds",
    "customer_to_speech_seconds",
    "customer_to_ivr_seconds",
    "speech_to_ivr_seconds",
    "ivr_to_acd_seconds",
    "acd_to_alert_seconds",
    "alert_to_agent_seconds",
    "customer_to_acd_seconds",
    "customer_to_agent_seconds",
    "dominant_latency_stage",
    "dominant_latency_seconds",
    "has_conference",
    "notes",
]

DERIVED_COLUMNS = [
    "delay_seconds",
    "delay_band",
    "hour",
    "weekday",
    "date",
]

NUMERIC_COLUMNS = {
    "conversation_duration_seconds",
    "preconnect_duration_seconds",
    "dial_to_customer_seconds",
    "customer_to_speech_seconds",
    "customer_to_ivr_seconds",
    "speech_to_ivr_seconds",
    "ivr_to_acd_seconds",
    "acd_to_alert_seconds",
    "alert_to_agent_seconds",
    "customer_to_acd_seconds",
    "customer_to_agent_seconds",
    "dominant_latency_seconds",
    "delay_seconds",
    "hour",
}

INSERT_COLUMNS = ["campaign_id"] + CSV_COLUMNS + DERIVED_COLUMNS
INSERT_SQL = (
    f"INSERT INTO conversations ({', '.join(INSERT_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in INSERT_COLUMNS)})"
)

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;

CREATE TABLE IF NOT EXISTS campaigns (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    csv_path        TEXT NOT NULL,
    imported_at     TEXT,
    row_count       INTEGER NOT NULL DEFAULT 0,
    file_size_bytes INTEGER,
    file_mtime      TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id                 TEXT NOT NULL REFERENCES campaigns(id),

    file                        TEXT,
    conversation_id             TEXT,
    conversation_start          TEXT,
    conversation_end            TEXT,
    conversation_duration_seconds REAL,
    customer_participant_name   TEXT,
    customer_connected_time     TEXT,
    customer_connected_source   TEXT,
    speech_detected_start       TEXT,
    speech_detected_end         TEXT,
    preconnect_duration_seconds REAL,
    total_ringbacks             TEXT,
    line_connected              TEXT,
    disposition_name            TEXT,
    disposition_analyzer        TEXT,
    speech_detected_participant_name TEXT,
    speech_detected_source      TEXT,
    ivr_participant_name        TEXT,
    ivr_start_time              TEXT,
    ivr_start_source            TEXT,
    acd_participant_name        TEXT,
    acd_offer_time              TEXT,
    acd_offer_source            TEXT,
    agent_participant_name      TEXT,
    agent_alert_time            TEXT,
    agent_alert_source          TEXT,
    agent_connected_time        TEXT,
    agent_connected_source      TEXT,
    dial_start_time             TEXT,
    dial_start_source           TEXT,
    dial_to_customer_seconds    REAL,
    customer_to_speech_seconds  REAL,
    customer_to_ivr_seconds     REAL,
    speech_to_ivr_seconds       REAL,
    ivr_to_acd_seconds          REAL,
    acd_to_alert_seconds        REAL,
    alert_to_agent_seconds      REAL,
    customer_to_acd_seconds     REAL,
    customer_to_agent_seconds   REAL,
    dominant_latency_stage      TEXT,
    dominant_latency_seconds    REAL,
    has_conference              TEXT,
    notes                       TEXT,

    delay_seconds               REAL,
    delay_band                  TEXT,
    hour                        INTEGER,
    weekday                     TEXT,
    date                        TEXT
);

CREATE INDEX IF NOT EXISTS idx_conversations_campaign
    ON conversations(campaign_id);
CREATE INDEX IF NOT EXISTS idx_conversations_campaign_date
    ON conversations(campaign_id, date);
CREATE INDEX IF NOT EXISTS idx_conversations_campaign_disposition
    ON conversations(campaign_id, disposition_analyzer);
CREATE INDEX IF NOT EXISTS idx_conversations_campaign_delay_band
    ON conversations(campaign_id, delay_band);
CREATE INDEX IF NOT EXISTS idx_conversations_campaign_latency_stage
    ON conversations(campaign_id, dominant_latency_stage);
CREATE INDEX IF NOT EXISTS idx_conversations_campaign_start
    ON conversations(campaign_id, conversation_start);
"""


def load_config(config_path: Path) -> Dict[str, Any]:
    config = _load_config(config_path)
    if "campaigns" not in config:
        raise ValueError(f"Invalid config: missing 'campaigns' in {config_path}")
    return config


def parse_num(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number else None  # reject NaN


def clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def parse_start(value: Any) -> Optional[datetime]:
    text = clean(value)
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def delay_band_for(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    if seconds < 2:
        return "0-2"
    if seconds < 10:
        return "2-10"
    if seconds < 20:
        return "10-20"
    return "20+"


def has_timestamp(value: Any) -> bool:
    return clean(value) is not None


def seconds_between(later: Any, earlier: Any) -> Optional[float]:
    later_dt = parse_start(later)
    earlier_dt = parse_start(earlier)
    if later_dt is None or earlier_dt is None:
        return None
    return round((later_dt - earlier_dt).total_seconds(), 6)


def customer_to_agent_seconds_for(row: Dict[str, Any]) -> Optional[float]:
    cta = row.get("customer_to_agent_seconds")
    if cta is not None:
        return cta
    if has_timestamp(row.get("agent_connected_time")) and has_timestamp(row.get("customer_connected_time")):
        return seconds_between(row.get("agent_connected_time"), row.get("customer_connected_time"))
    return None


def disposition_to_agent_seconds_for(row: Dict[str, Any]) -> Optional[float]:
    """Compliance path when an agent connects: speech_detected_end → agent_connected_time."""
    if not has_timestamp(row.get("speech_detected_end")):
        return None
    if not has_timestamp(row.get("agent_connected_time")):
        return None
    return seconds_between(row.get("agent_connected_time"), row.get("speech_detected_end"))


def is_queue_abandon(row: Dict[str, Any]) -> bool:
    return (
        has_timestamp(row.get("speech_detected_end"))
        and has_timestamp(row.get("acd_offer_time"))
        and not has_timestamp(row.get("agent_connected_time"))
    )


def disposition_to_abandoned_seconds_for(row: Dict[str, Any]) -> Optional[float]:
    """Compliance path when offered to queue but no agent: speech_detected_end → conversation_end."""
    if not is_queue_abandon(row):
        return None
    if not has_timestamp(row.get("conversation_end")):
        return None
    return seconds_between(row.get("conversation_end"), row.get("speech_detected_end"))


def customer_to_abandoned_seconds_for(row: Dict[str, Any]) -> Optional[float]:
    if not (
        has_timestamp(row.get("customer_connected_time"))
        and has_timestamp(row.get("acd_offer_time"))
        and not has_timestamp(row.get("agent_connected_time"))
        and has_timestamp(row.get("conversation_end"))
    ):
        return None
    return seconds_between(row.get("conversation_end"), row.get("customer_connected_time"))


def compliance_delay_seconds_for(row: Dict[str, Any]) -> Optional[float]:
    """Disposition→agent when connected; disposition→abandon when offered to queue but no agent."""
    dta = disposition_to_agent_seconds_for(row)
    if dta is not None:
        return dta if dta >= 0 else None
    dtab = disposition_to_abandoned_seconds_for(row)
    if dtab is not None:
        return dtab if dtab >= 0 else None
    return None


def normalize_row(row: Dict[str, str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for column in CSV_COLUMNS:
        raw = row.get(column)
        if column in NUMERIC_COLUMNS:
            out[column] = parse_num(raw)
        else:
            out[column] = clean(raw)

    start = parse_start(out.get("conversation_start"))
    delay_seconds = compliance_delay_seconds_for(out)

    out["delay_seconds"] = delay_seconds
    out["delay_band"] = delay_band_for(delay_seconds)
    out["hour"] = start.hour if start else None
    out["weekday"] = start.strftime("%a") if start else None
    out["date"] = start.date().isoformat() if start else None
    return out


def row_to_tuple(campaign_id: str, row: Dict[str, Any]) -> Tuple[Any, ...]:
    return tuple([campaign_id] + [row.get(column) for column in CSV_COLUMNS + DERIVED_COLUMNS])


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)")}
    if "genesys_guid" not in columns:
        conn.execute("ALTER TABLE campaigns ADD COLUMN genesys_guid TEXT")
    conn.commit()


def resolve_csv_path(data_dir: Path, csv_rel: str) -> Path:
    path = (data_dir / csv_rel).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"CSV not found: {path}")
    return path


def import_campaign(
    conn: sqlite3.Connection,
    campaign_id: str,
    campaign_name: str,
    csv_path: Path,
    batch_size: int,
    quiet: bool,
    genesys_guid: Optional[str] = None,
) -> int:
    file_stat = csv_path.stat()
    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    file_mtime = datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat()

    conn.execute("DELETE FROM conversations WHERE campaign_id = ?", (campaign_id,))
    conn.execute(
        """
        INSERT INTO campaigns (id, name, csv_path, imported_at, row_count, file_size_bytes, file_mtime, genesys_guid)
        VALUES (?, ?, ?, ?, 0, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            csv_path = excluded.csv_path,
            imported_at = excluded.imported_at,
            row_count = 0,
            file_size_bytes = excluded.file_size_bytes,
            file_mtime = excluded.file_mtime,
            genesys_guid = COALESCE(excluded.genesys_guid, campaigns.genesys_guid)
        """,
        (
            campaign_id,
            campaign_name,
            str(csv_path),
            imported_at,
            file_stat.st_size,
            file_mtime,
            genesys_guid,
        ),
    )
    conn.commit()

    batch: List[Tuple[Any, ...]] = []
    row_count = 0
    started = time.time()

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in CSV_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{csv_path.name} is missing columns: {', '.join(missing)}")

        for raw_row in reader:
            normalized = normalize_row(raw_row)
            batch.append(row_to_tuple(campaign_id, normalized))
            row_count += 1

            if len(batch) >= batch_size:
                conn.executemany(INSERT_SQL, batch)
                conn.commit()
                batch.clear()
                if not quiet and row_count % (batch_size * 10) == 0:
                    elapsed = max(time.time() - started, 0.001)
                    rate = row_count / elapsed
                    print(f"  {campaign_id}: {row_count:,} rows ({rate:,.0f} rows/s)")

    if batch:
        conn.executemany(INSERT_SQL, batch)
        conn.commit()

    conn.execute(
        "UPDATE campaigns SET row_count = ?, imported_at = ? WHERE id = ?",
        (row_count, imported_at, campaign_id),
    )
    conn.commit()

    elapsed = max(time.time() - started, 0.001)
    if not quiet:
        print(
            f"  {campaign_id}: imported {row_count:,} rows in {elapsed:.1f}s "
            f"({row_count / elapsed:,.0f} rows/s)"
        )
    return row_count


RECOMPUTE_SELECT_SQL = """
SELECT
    id,
    speech_detected_end,
    acd_offer_time,
    agent_connected_time,
    conversation_end
FROM conversations
{where}
ORDER BY id
"""

RECOMPUTE_UPDATE_SQL = """
UPDATE conversations
SET delay_seconds = ?, delay_band = ?
WHERE id = ?
"""


def recompute_delays(
    conn: sqlite3.Connection,
    campaign_id: Optional[str] = None,
    batch_size: int = 5000,
    quiet: bool = False,
) -> int:
    where = "WHERE campaign_id = ?" if campaign_id else ""
    params: Tuple[Any, ...] = (campaign_id,) if campaign_id else ()
    cursor = conn.execute(RECOMPUTE_SELECT_SQL.format(where=where), params)

    updated = 0
    batch: List[Tuple[Any, ...]] = []
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row_id, speech_detected_end, acd_offer, agent_connected, conversation_end in rows:
            payload = {
                "speech_detected_end": speech_detected_end,
                "acd_offer_time": acd_offer,
                "agent_connected_time": agent_connected,
                "conversation_end": conversation_end,
            }
            delay_seconds = compliance_delay_seconds_for(payload)
            batch.append((delay_seconds, delay_band_for(delay_seconds), row_id))
            updated += 1
        conn.executemany(RECOMPUTE_UPDATE_SQL, batch)
        conn.commit()
        batch.clear()
        if not quiet and updated % (batch_size * 10) == 0:
            print(f"  recomputed {updated:,} rows...")

    if not quiet:
        scope = campaign_id or "all campaigns"
        print(f"Recomputed delay bands for {updated:,} rows ({scope}).")
    return updated


def list_campaigns(config: Dict[str, Any], conn: Optional[sqlite3.Connection]) -> None:
    configured = config.get("campaigns", [])
    print(f"Configured campaigns ({len(configured)}):")
    for campaign in configured:
        print(f"  - {campaign['id']}: {campaign.get('name', campaign['id'])}")

    if conn is None:
        return

    rows = conn.execute(
        "SELECT id, name, row_count, imported_at, file_size_bytes FROM campaigns ORDER BY id"
    ).fetchall()
    if not rows:
        print("\nDatabase: no campaigns imported yet.")
        return

    print(f"\nImported campaigns ({len(rows)}):")
    for campaign_id, name, row_count, imported_at, file_size in rows:
        size_mb = (file_size or 0) / (1024 * 1024)
        print(
            f"  - {campaign_id}: {name} | {row_count:,} rows | "
            f"{size_mb:.1f} MB | {imported_at or 'n/a'}"
        )


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import outbound campaign CSV files into SQLite.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to campaigns.json")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database output path")
    parser.add_argument("--campaign", action="append", help="Campaign id to import (repeatable)")
    parser.add_argument("--all", action="store_true", help="Import all configured campaigns")
    parser.add_argument("--list", action="store_true", help="List configured/imported campaigns")
    parser.add_argument("--batch-size", type=int, default=5000, help="Rows per insert batch")
    parser.add_argument("--quiet", action="store_true", help="Reduce progress output")
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="Skip campaigns whose conversation_timings.csv is not present yet",
    )
    parser.add_argument(
        "--recompute-delays",
        action="store_true",
        help="Recompute delay_seconds/delay_band from stored timestamps (no CSV re-read)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    data_dir = resolve_data_dir(config)
    if not data_dir.is_dir():
        print(f"Data directory not found: {data_dir}", file=sys.stderr)
        return 1

    campaigns_by_id = {item["id"]: item for item in config["campaigns"]}

    if args.list:
        conn = sqlite3.connect(args.db) if args.db.exists() else None
        try:
            list_campaigns(config, conn)
        finally:
            if conn is not None:
                conn.close()
        return 0

    if args.recompute_delays:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(args.db)
        try:
            init_db(conn)
            if args.all:
                recompute_delays(conn, quiet=args.quiet)
            elif args.campaign:
                unknown = [campaign_id for campaign_id in args.campaign if campaign_id not in campaigns_by_id]
                if unknown:
                    print(f"Unknown campaign id(s): {', '.join(unknown)}", file=sys.stderr)
                    return 1
                for campaign_id in args.campaign:
                    recompute_delays(conn, campaign_id=campaign_id, quiet=args.quiet)
            else:
                recompute_delays(conn, quiet=args.quiet)
        finally:
            conn.close()
        return 0

    if args.all:
        selected = list(campaigns_by_id.keys())
    elif args.campaign:
        selected = args.campaign
        unknown = [campaign_id for campaign_id in selected if campaign_id not in campaigns_by_id]
        if unknown:
            print(f"Unknown campaign id(s): {', '.join(unknown)}", file=sys.stderr)
            return 1
    else:
        print("Specify --campaign <id>, --all, or --list", file=sys.stderr)
        return 1

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        print(f"Database: {args.db}")
        total_rows = 0
        for campaign_id in selected:
            campaign = campaigns_by_id[campaign_id]
            csv_path = data_dir / campaign["csv"]
            if not csv_path.is_file():
                if args.skip_missing:
                    print(f"SKIP: {campaign_id} — CSV not found at {csv_path}")
                    continue
                raise FileNotFoundError(f"CSV not found: {csv_path.resolve()}")
            print(f"Importing {campaign_id} from {csv_path}")
            total_rows += import_campaign(
                conn,
                campaign_id=campaign_id,
                campaign_name=campaign.get("name", campaign_id),
                csv_path=csv_path,
                batch_size=max(1, args.batch_size),
                quiet=args.quiet,
                genesys_guid=campaign.get("genesys_guid"),
            )
        print(f"Done. Imported {total_rows:,} total rows across {len(selected)} campaign(s).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
