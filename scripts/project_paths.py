"""Shared paths for OutboundDashboard pipeline scripts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = SCRIPT_DIR / "campaigns.json"
DEFAULT_DATA_DIR = PROJECT_DIR / "data" / "campaigns"
DEFAULT_DB = PROJECT_DIR / "data" / "outbound_dashboard.db"
OUTBOUND_CSV_NAME = "outbound_campaigns.csv"
ANALYSIS_SCRIPT = SCRIPT_DIR / "genesys_delivery_analysis_speechfix_v4.py"
COLLECT_SCRIPT = SCRIPT_DIR / "collect_genesys_conversations_access_token.sh"


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or DEFAULT_CONFIG
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    return {"dataDir": "data/campaigns", "campaigns": []}


def resolve_data_dir(config: Mapping[str, Any] | None = None) -> Path:
    if config is None:
        config = load_config()
    raw = config.get("dataDir") or "data/campaigns"
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (PROJECT_DIR / path).resolve()
    return path


def relative_data_dir(data_dir: Path) -> str:
    try:
        return str(data_dir.relative_to(PROJECT_DIR))
    except ValueError:
        return str(data_dir)
