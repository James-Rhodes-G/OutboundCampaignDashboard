#!/usr/bin/env python3
"""Analyze Genesys Cloud outbound delivery timing from conversation analytics JSON.

Version 4: refreshed filename to avoid stale downloads.

This script is designed for the Genesys Cloud Analytics Conversations Details
export shape, such as a JSON array of conversation records where each record has
conversationId, conversationStart, conversationEnd, and participants.

It also supports the raw conversation detail API shape and a few common wrapper
shapes.

Calculated timings include:
- customer answer -> speech detection (customer_to_speech_seconds)
- customer answer -> IVR start (customer_to_ivr_seconds)
- IVR start -> ACD offer (ivr_to_acd_seconds)
- ACD offer -> agent alert (acd_to_alert_seconds)
- agent alert -> agent connected (alert_to_agent_seconds)
- customer answer -> agent connected (customer_to_agent_seconds)

The script writes:
- conversation_timings.csv
- summary.csv
- outliers.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
WRAPPER_KEYS = ("conversations", "items", "results", "data", "records", "entities")


@dataclass
class ConversationTiming:
    file: str
    conversation_id: str
    conversation_start: Optional[str]
    conversation_end: Optional[str]
    conversation_duration_seconds: Optional[float]
    customer_participant_name: Optional[str]
    customer_connected_time: Optional[str]
    customer_connected_source: Optional[str]
    speech_detected_start: Optional[str]
    speech_detected_end: Optional[str]
    preconnect_duration_seconds: Optional[float]
    total_ringbacks: Optional[int]
    line_connected: Optional[bool]
    disposition_name: Optional[str]
    disposition_analyzer: Optional[str]
    speech_detected_participant_name: Optional[str]
    speech_detected_source: Optional[str]
    ivr_participant_name: Optional[str]
    ivr_start_time: Optional[str]
    ivr_start_source: Optional[str]
    acd_participant_name: Optional[str]
    acd_offer_time: Optional[str]
    acd_offer_source: Optional[str]
    agent_participant_name: Optional[str]
    agent_alert_time: Optional[str]
    agent_alert_source: Optional[str]
    agent_connected_time: Optional[str]
    agent_connected_source: Optional[str]
    dial_start_time: Optional[str]
    dial_start_source: Optional[str]
    dial_to_customer_seconds: Optional[float]
    customer_to_speech_seconds: Optional[float]
    customer_to_ivr_seconds: Optional[float]
    speech_to_ivr_seconds: Optional[float]
    ivr_to_acd_seconds: Optional[float]
    acd_to_alert_seconds: Optional[float]
    alert_to_agent_seconds: Optional[float]
    customer_to_acd_seconds: Optional[float]
    customer_to_agent_seconds: Optional[float]
    dominant_latency_stage: Optional[str]
    dominant_latency_seconds: Optional[float]
    has_conference: Optional[bool]
    notes: Optional[str]


# ---------------------------- time helpers ----------------------------

def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            return datetime.strptime(ts, ISO_FMT).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def seconds_between(later: Optional[str], earlier: Optional[str]) -> Optional[float]:
    a = parse_iso(later)
    b = parse_iso(earlier)
    if a is None or b is None:
        return None
    return round((a - b).total_seconds(), 6)


# ---------------------------- JSON loading ----------------------------

def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def is_conversation_record(obj: Any) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("participants"), list) and any(
        key in obj for key in ("id", "startTime", "endTime", "conversationId", "conversationStart", "conversationEnd")
    )


def collect_conversation_records(obj: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def key_for(node: Dict[str, Any]) -> str:
        conv_id = node.get("id") or node.get("conversationId")
        if conv_id:
            return str(conv_id)
        try:
            return json.dumps(node.get("participants", [])[:1], sort_keys=True, default=str)
        except Exception:
            return str(id(node))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if is_conversation_record(node):
                key = key_for(node)
                if key not in seen:
                    seen.add(key)
                    records.append(node)
                return

            for wrapper_key in WRAPPER_KEYS:
                value = node.get(wrapper_key)
                if isinstance(value, list):
                    for item in value:
                        walk(item)
                elif isinstance(value, dict):
                    walk(value)

            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return records


def parse_json_text(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []

    try:
        obj = json.loads(text)
        records = collect_conversation_records(obj)
        if records:
            return records
    except Exception:
        pass

    # NDJSON / JSONL fallback.
    ndjson_records: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ndjson_records.extend(collect_conversation_records(obj))
    if ndjson_records:
        return ndjson_records

    # Pasted text fallback: pull out object literals.
    records: List[Dict[str, Any]] = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    chunk = text[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                        records.extend(collect_conversation_records(obj))
                    except Exception:
                        pass
                    start = None
    return records


def try_parse_file(path: Path) -> List[Dict[str, Any]]:
    text = load_text(path)
    if not text:
        return []
    return parse_json_text(text)


def iter_input_files(input_path: Path) -> Iterable[Path]:
    if input_path.is_file():
        yield input_path
        return
    for p in sorted(input_path.rglob("*")):
        if p.is_file() and p.suffix.lower() in {".json", ".txt", ".jsonl"}:
            yield p


# ---------------------------- extraction helpers ----------------------------

def first_voice_session(participant: Dict[str, Any]) -> Dict[str, Any]:
    sessions = participant.get("sessions") or participant.get("calls") or []
    if not isinstance(sessions, list):
        return {}
    for sess in sessions:
        if isinstance(sess, dict):
            if sess.get("mediaType", "voice") == "voice" or "mediaType" not in sess:
                return sess
    return sessions[0] if sessions and isinstance(sessions[0], dict) else {}


def participant_purpose(p: Dict[str, Any]) -> str:
    return str(p.get("purpose") or p.get("participantType") or "").strip().lower()


def participant_name(p: Dict[str, Any]) -> str:
    return str(p.get("participantName") or p.get("name") or "").strip()


def classify_participant(p: Dict[str, Any]) -> str:
    purpose = participant_purpose(p)
    ptype = str(p.get("participantType") or "").strip().lower()
    name = participant_name(p).lower()

    if purpose in {"customer", "external", "enduser"} or ptype == "external":
        return "customer"
    if purpose in {"ivr", "flow"} or ptype == "ivr" or "outbound" in name and "flow" in name:
        return "ivr"
    if purpose in {"acd", "queue"} or ptype == "acd":
        return "acd"
    if purpose in {"agent", "user", "internal"} or ptype in {"agent", "user"}:
        return "agent"
    return "other"


def conversation_time(record: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def get_metrics(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    metrics = session.get("metrics") or []
    return metrics if isinstance(metrics, list) else []


def get_segments(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    segments = session.get("segments") or []
    return segments if isinstance(segments, list) else []


def find_metric_emit_date(session: Dict[str, Any], metric_name: str) -> Optional[str]:
    for metric in get_metrics(session):
        if not isinstance(metric, dict):
            continue
        if str(metric.get("name") or "") == metric_name:
            emit = metric.get("emitDate")
            if isinstance(emit, str) and emit:
                return emit
    return None


def find_first_segment_time(session: Dict[str, Any], segment_type: str, field: str = "segmentStart") -> Optional[str]:
    for seg in get_segments(session):
        if not isinstance(seg, dict):
            continue
        if str(seg.get("segmentType") or "").lower() == segment_type.lower():
            ts = seg.get(field)
            if isinstance(ts, str) and ts:
                return ts
    return None


def find_segment_time(session: Dict[str, Any], segment_types: Sequence[str], field: str = "segmentStart") -> Optional[str]:
    wanted = {s.lower() for s in segment_types}
    for seg in get_segments(session):
        if not isinstance(seg, dict):
            continue
        if str(seg.get("segmentType") or "").lower() in wanted:
            ts = seg.get(field)
            if isinstance(ts, str) and ts:
                return ts
    return None


def participant_has_conference(participant: Dict[str, Any]) -> bool:
    for sess in participant.get("sessions") or []:
        if not isinstance(sess, dict):
            continue
        for seg in sess.get("segments") or []:
            if isinstance(seg, dict) and bool(seg.get("conference")):
                return True
    return False


def select_participants(conversation: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    buckets = {"customer": [], "ivr": [], "acd": [], "agent": [], "other": []}
    for p in conversation.get("participants", []) or []:
        if not isinstance(p, dict):
            continue
        buckets[classify_participant(p)].append(p)
    return buckets


def best_by_time(participants: List[Dict[str, Any]], time_func) -> Optional[Dict[str, Any]]:
    best_participant = None
    best_time = None
    for p in participants:
        t = time_func(p)
        if t is None:
            continue
        if best_time is None or parse_iso(t) < parse_iso(best_time):
            best_participant = p
            best_time = t
    return best_participant


def get_customer_answer_time(p: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[float], Optional[int], Optional[bool], Optional[str]]:
    sess = first_voice_session(p)
    connected = find_metric_emit_date(sess, "nConnected")
    source = "metric:nConnected" if connected else None
    if connected is None:
        connected = find_first_segment_time(sess, "dialing", field="segmentEnd")
        if connected:
            source = "segment:dialing.end"
    if connected is None:
        connected = sess.get("connectedTime") if isinstance(sess.get("connectedTime"), str) else None
        if connected:
            source = "session.connectedTime"

    dial_start = find_first_segment_time(sess, "dialing", field="segmentStart")
    if dial_start is None:
        dial_start = sess.get("startTime") if isinstance(sess.get("startTime"), str) else None
    dial_source = "segment:dialing.start" if dial_start and source else ("session.startTime" if dial_start else None)

    disposition_name = None
    preconnect = None
    total_ringbacks = None
    line_connected = None

    disp = sess.get("disposition") if isinstance(sess.get("disposition"), dict) else {}
    if disp:
        disposition_name = disp.get("name") if isinstance(disp.get("name"), str) else None
        params = disp.get("dispositionParameters") or {}
        if isinstance(params, dict):
            adj = params.get("adjustableLiveSpeakerDetection") or {}
            if isinstance(adj, dict):
                preconnect = adj.get("preconnectDuration")
                total_ringbacks = adj.get("totalRingbacks")
                line_connected = adj.get("lineConnected")
    else:
        disposition_name = sess.get("dispositionName") if isinstance(sess.get("dispositionName"), str) else None
        if isinstance(sess.get("preconnectDuration"), str):
            preconnect = sess.get("preconnectDuration")
        total_ringbacks = sess.get("totalRingbacks") if isinstance(sess.get("totalRingbacks"), int) else None
        line_connected = sess.get("lineConnected") if isinstance(sess.get("lineConnected"), bool) else None

    preconnect_seconds = None
    if isinstance(preconnect, str) and preconnect.startswith("PT") and preconnect.endswith("S"):
        try:
            preconnect_seconds = float(preconnect[2:-1])
        except Exception:
            preconnect_seconds = None

    return connected, source, dial_start, dial_source, preconnect_seconds, total_ringbacks, line_connected, disposition_name


def _speech_candidate(
    participant_label: Optional[str],
    node: Dict[str, Any],
    source: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    analyzer = None
    disposition_name = None
    speech_start = None
    speech_end = None

    disp = node.get("disposition") if isinstance(node.get("disposition"), dict) else {}
    if disp:
        analyzer = disp.get("dispositionAnalyzer") if isinstance(disp.get("dispositionAnalyzer"), str) else None
        disposition_name = disp.get("name") if isinstance(disp.get("name"), str) else None
        speech_start = disp.get("detectedSpeechStart") if isinstance(disp.get("detectedSpeechStart"), str) else None
        speech_end = disp.get("detectedSpeechEnd") if isinstance(disp.get("detectedSpeechEnd"), str) else None
        if not source and speech_start:
            source = "disposition.detectedSpeechStart"
    else:
        analyzer = node.get("dispositionAnalyzer") if isinstance(node.get("dispositionAnalyzer"), str) else None
        disposition_name = node.get("dispositionName") if isinstance(node.get("dispositionName"), str) else None
        speech_start = node.get("detectedSpeechStart") if isinstance(node.get("detectedSpeechStart"), str) else None
        speech_end = node.get("detectedSpeechEnd") if isinstance(node.get("detectedSpeechEnd"), str) else None
        if not source and speech_start:
            source = "session.detectedSpeechStart"

    return participant_label, source, analyzer, disposition_name, speech_start, speech_end


def find_speech_detection(record: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Find the earliest speech detection timestamp anywhere in the conversation."""
    best: Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]] = (None, None, None, None, None, None)
    best_dt = None

    def consider(participant_label: Optional[str], node: Dict[str, Any], source: str) -> None:
        nonlocal best, best_dt
        candidate = _speech_candidate(participant_label, node, source)
        speech_start = candidate[4]
        if not speech_start:
            return
        dt = parse_iso(speech_start)
        if dt is None:
            return
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = candidate

    def walk(node: Any, participant_label: Optional[str] = None, source_prefix: str = "record") -> None:
        if isinstance(node, dict):
            current_participant = participant_label or participant_name(node) or None

            # Check the current node first.
            if any(k in node for k in ("detectedSpeechStart", "detectedSpeechEnd", "dispositionAnalyzer", "dispositionName", "disposition")):
                consider(current_participant, node, source_prefix)

            for key, value in node.items():
                if isinstance(value, dict):
                    walk(value, current_participant, f"{source_prefix}.{key}")
                elif isinstance(value, list):
                    for idx, item in enumerate(value):
                        walk(item, current_participant, f"{source_prefix}.{key}[{idx}]")
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                walk(item, participant_label, f"{source_prefix}[{idx}]")

    walk(record)
    return best


def get_ivr_start(p: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    sess = first_voice_session(p)
    t = find_first_segment_time(sess, "ivr", field="segmentStart")
    if t:
        return t, "segment:ivr.start"
    t = find_metric_emit_date(sess, "nFlow")
    if t:
        return t, "metric:nFlow"
    # Raw conversation API fallback.
    t = sess.get("connectedTime") if isinstance(sess.get("connectedTime"), str) else None
    if t:
        return t, "session.connectedTime"
    return None, None


def get_acd_offer_time(p: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    sess = first_voice_session(p)
    t = find_metric_emit_date(sess, "nOffered")
    if t:
        return t, "metric:nOffered"
    t = find_first_segment_time(sess, "delay", field="segmentEnd")
    if t:
        return t, "segment:delay.end"
    t = find_first_segment_time(sess, "interact", field="segmentStart")
    if t:
        return t, "segment:interact.start"
    t = sess.get("connectedTime") if isinstance(sess.get("connectedTime"), str) else None
    if t:
        return t, "session.connectedTime"
    return None, None


def get_agent_alert_time(p: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    sess = first_voice_session(p)
    t = find_first_segment_time(sess, "alert", field="segmentStart")
    if t:
        return t, "segment:alert.start"
    # Some raw conversation payloads use startAlertingTime.
    t = sess.get("startAlertingTime") if isinstance(sess.get("startAlertingTime"), str) else None
    if t:
        return t, "session.startAlertingTime"
    t = find_metric_emit_date(sess, "tAlert")
    if t:
        return t, "metric:tAlert"
    return None, None


def get_agent_connected_time(p: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    sess = first_voice_session(p)
    t = find_metric_emit_date(sess, "nOutboundConnected")
    if t:
        return t, "metric:nOutboundConnected"
    t = find_first_segment_time(sess, "interact", field="segmentStart")
    if t:
        return t, "segment:interact.start"
    t = sess.get("connectedTime") if isinstance(sess.get("connectedTime"), str) else None
    if t:
        return t, "session.connectedTime"
    return None, None


def extract_timing(record: Dict[str, Any], source_file: str) -> ConversationTiming:
    convo_id = conversation_time(record, "conversationId", "id") or ""
    convo_start = conversation_time(record, "conversationStart", "startTime")
    convo_end = conversation_time(record, "conversationEnd", "endTime")
    convo_duration = seconds_between(convo_end, convo_start)

    buckets = select_participants(record)
    conference = any(participant_has_conference(p) for p in record.get("participants", []) or [] if isinstance(p, dict))

    customer = best_by_time(buckets["customer"], lambda p: get_customer_answer_time(p)[0])
    ivr = best_by_time(buckets["ivr"], lambda p: get_ivr_start(p)[0])
    acd = best_by_time(buckets["acd"], lambda p: get_acd_offer_time(p)[0])
    agent = best_by_time(buckets["agent"], lambda p: get_agent_connected_time(p)[0])

    customer_connected = None
    customer_connected_source = None
    speech_start = None
    speech_end = None
    preconnect_seconds = None
    total_ringbacks = None
    line_connected = None
    disposition_name = None
    dial_start = None
    dial_source = None

    customer_name = participant_name(customer) if customer else None
    if customer:
        (
            customer_connected,
            customer_connected_source,
            dial_start,
            dial_source,
            preconnect_seconds,
            total_ringbacks,
            line_connected,
            disposition_name,
        ) = get_customer_answer_time(customer)

    speech_participant_name, speech_source, disposition_analyzer, speech_start, speech_end = (None, None, None, None, None)
    if record.get("participants"):
        speech_participant_name, speech_source, disposition_analyzer, _speech_disposition_name, speech_start, speech_end = find_speech_detection(record)
        if disposition_name is None:
            disposition_name = _speech_disposition_name

    ivr_name = participant_name(ivr) if ivr else None
    ivr_start, ivr_source = (get_ivr_start(ivr) if ivr else (None, None))

    acd_name = participant_name(acd) if acd else None
    acd_offer, acd_source = (get_acd_offer_time(acd) if acd else (None, None))

    agent_name = participant_name(agent) if agent else None
    agent_alert, agent_alert_source = (get_agent_alert_time(agent) if agent else (None, None))
    agent_connected, agent_connected_source = (get_agent_connected_time(agent) if agent else (None, None))

    dial_to_customer = seconds_between(customer_connected, dial_start)
    customer_to_speech = seconds_between(speech_start, customer_connected)
    customer_to_ivr = seconds_between(ivr_start, customer_connected)
    speech_to_ivr = seconds_between(ivr_start, speech_start)
    ivr_to_acd = seconds_between(acd_offer, ivr_start)
    acd_to_alert = seconds_between(agent_alert, acd_offer)
    alert_to_agent = seconds_between(agent_connected, agent_alert)
    customer_to_acd = seconds_between(acd_offer, customer_connected)
    customer_to_agent = seconds_between(agent_connected, customer_connected)

    stage_candidates: List[Tuple[str, Optional[float]]] = [
        ("dial_to_customer_seconds", dial_to_customer),
        ("customer_to_speech_seconds", customer_to_speech),
        ("customer_to_ivr_seconds", customer_to_ivr),
        ("speech_to_ivr_seconds", speech_to_ivr),
        ("ivr_to_acd_seconds", ivr_to_acd),
        ("acd_to_alert_seconds", acd_to_alert),
        ("alert_to_agent_seconds", alert_to_agent),
    ]
    dominant_stage = None
    dominant_seconds = None
    max_value = -1.0
    for stage_name, value in stage_candidates:
        if value is not None and value >= max_value:
            max_value = value
            dominant_stage = stage_name
            dominant_seconds = value

    return ConversationTiming(
        file=source_file,
        conversation_id=convo_id,
        conversation_start=convo_start,
        conversation_end=convo_end,
        conversation_duration_seconds=convo_duration,
        customer_participant_name=customer_name,
        customer_connected_time=customer_connected,
        customer_connected_source=customer_connected_source,
        speech_detected_start=speech_start,
        speech_detected_end=speech_end,
        preconnect_duration_seconds=preconnect_seconds,
        total_ringbacks=total_ringbacks,
        line_connected=line_connected,
        disposition_name=disposition_name,
        disposition_analyzer=disposition_analyzer,
        speech_detected_participant_name=speech_participant_name,
        speech_detected_source=speech_source,
        ivr_participant_name=ivr_name,
        ivr_start_time=ivr_start,
        ivr_start_source=ivr_source,
        acd_participant_name=acd_name,
        acd_offer_time=acd_offer,
        acd_offer_source=acd_source,
        agent_participant_name=agent_name,
        agent_alert_time=agent_alert,
        agent_alert_source=agent_alert_source,
        agent_connected_time=agent_connected,
        agent_connected_source=agent_connected_source,
        dial_start_time=dial_start,
        dial_start_source=dial_source,
        dial_to_customer_seconds=dial_to_customer,
        customer_to_speech_seconds=customer_to_speech,
        customer_to_ivr_seconds=customer_to_ivr,
        speech_to_ivr_seconds=speech_to_ivr,
        ivr_to_acd_seconds=ivr_to_acd,
        acd_to_alert_seconds=acd_to_alert,
        alert_to_agent_seconds=alert_to_agent,
        customer_to_acd_seconds=customer_to_acd,
        customer_to_agent_seconds=customer_to_agent,
        dominant_latency_stage=dominant_stage,
        dominant_latency_seconds=dominant_seconds,
        has_conference=conference,
        notes=None,
    )


# ---------------------------- reporting ----------------------------

def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    k = (len(values) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values[int(k)]
    return values[f] * (c - k) + values[c] * (k - f)


def summarize(rows: List[ConversationTiming]) -> List[Dict[str, Any]]:
    metrics = {
        "dial_to_customer_seconds": [],
        "customer_to_speech_seconds": [],
        "customer_to_ivr_seconds": [],
        "speech_to_ivr_seconds": [],
        "ivr_to_acd_seconds": [],
        "acd_to_alert_seconds": [],
        "alert_to_agent_seconds": [],
        "customer_to_acd_seconds": [],
        "customer_to_agent_seconds": [],
        "conversation_duration_seconds": [],
    }
    for r in rows:
        for name in metrics:
            value = getattr(r, name)
            if value is not None:
                metrics[name].append(float(value))

    out = []
    for metric_name, values in metrics.items():
        if not values:
            continue
        out.append(
            {
                "metric": metric_name,
                "count": len(values),
                "mean_seconds": round(statistics.mean(values), 6),
                "median_seconds": round(statistics.median(values), 6),
                "p90_seconds": round(percentile(values, 90) or 0.0, 6),
                "p95_seconds": round(percentile(values, 95) or 0.0, 6),
                "max_seconds": round(max(values), 6),
            }
        )
    return out


def outliers(rows: List[ConversationTiming], threshold_seconds: float) -> List[Dict[str, Any]]:
    selected = [r for r in rows if r.customer_to_agent_seconds is not None and r.customer_to_agent_seconds >= threshold_seconds]
    selected.sort(key=lambda r: r.customer_to_agent_seconds or 0.0, reverse=True)
    return [asdict(r) for r in selected]


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------- main ----------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Genesys outbound delivery timing from analytics JSON.")
    parser.add_argument("--input", required=True, help="Input JSON file or directory.")
    parser.add_argument("--output-dir", required=True, help="Directory for CSV output.")
    parser.add_argument("--outlier-threshold-seconds", type=float, default=5.0, help="Threshold for outliers.csv (default: 5.0).")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timings: List[ConversationTiming] = []
    source_files = list(iter_input_files(input_path))
    if not source_files and input_path.is_file():
        source_files = [input_path]

    for file_path in source_files:
        records = try_parse_file(file_path)
        for rec in records:
            try:
                timings.append(extract_timing(rec, source_file=file_path.name))
            except Exception as e:
                print(f"WARN: failed to process conversation in {file_path.name}: {e}")

    if not timings:
        print("No conversation records found.")
        return 1

    # conversation_timings.csv
    timing_rows = [asdict(t) for t in timings]
    per_convo_path = output_dir / "conversation_timings.csv"
    write_csv(per_convo_path, timing_rows, list(timing_rows[0].keys()))

    # summary.csv
    summary_rows = summarize(timings)
    summary_path = output_dir / "summary.csv"
    if summary_rows:
        write_csv(summary_path, summary_rows, list(summary_rows[0].keys()))
    else:
        summary_path.write_text("metric,count,mean_seconds,median_seconds,p90_seconds,p95_seconds,max_seconds\n", encoding="utf-8")

    # outliers.csv
    outlier_rows = outliers(timings, args.outlier_threshold_seconds)
    outlier_path = output_dir / "outliers.csv"
    if outlier_rows:
        write_csv(outlier_path, outlier_rows, list(outlier_rows[0].keys()))
    else:
        outlier_path.write_text("", encoding="utf-8")

    print(f"Wrote {per_convo_path}")
    print(f"Wrote {summary_path}")
    print(f"Wrote {outlier_path}")
    print(f"Processed {len(timings)} conversations from {len(source_files)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
