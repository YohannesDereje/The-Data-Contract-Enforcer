import csv
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


# NOTE: The spec references 'events.jsonl' in the project root, but the actual
# source file is 'week5_events.csv'. This script reads the CSV directly since
# it contains the exact columns the mapping requires (event_id, stream_id,
# stream_position, payload, recorded_at, event_type).
SOURCE_FILE = Path("week5_events.csv")
TARGET_FILE = Path("outputs/week5/events.jsonl")

# Namespace for deterministic correlation_id generation per aggregate_id.
_CORRELATION_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def _to_pascal_case(text: str) -> str:
    """Convert any-case string to PascalCase.

    Works for snake_case, kebab-case, space-separated, or already PascalCase.
    Examples:
        "loan_application_started" -> "LoanApplicationStarted"
        "CreditScoreChecked"       -> "CreditScoreChecked"  (unchanged)
    """
    # If no word separators are present, the string is already PascalCase
    # (or a single word). Return it unchanged to avoid lowercasing the interior.
    if not re.search(r"[_\-\s]", text):
        return text

    tokens = re.split(r"[_\-\s]+", text)
    return "".join(word.capitalize() for word in tokens if word)


def _parse_payload(raw: str) -> dict:
    """Safely parse a JSON string into a dict; return empty dict on failure."""
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def transform_record(source_record: dict) -> dict:
    """Transform one CSV row (as a dict) into a canonical event_record.

    Field mapping from source CSV columns:
        event_id        -> event_id
        stream_id       -> aggregate_id
        stream_position -> sequence_number  (cast to int)
        payload         -> payload          (JSON-parsed)
        recorded_at     -> occurred_at, recorded_at
        event_type      -> event_type       (converted to PascalCase)

    All other canonical fields are synthesised.

    Args:
        source_record: A dict of raw CSV column values.

    Returns:
        A canonical event_record dictionary.
    """
    aggregate_id = source_record.get("stream_id") or ""
    raw_position = source_record.get("stream_position") or "0"

    try:
        sequence_number = int(raw_position)
    except ValueError:
        sequence_number = 0

    event_type_raw = source_record.get("event_type") or ""
    event_type = _to_pascal_case(event_type_raw) if event_type_raw else ""

    metadata = {
        "causation_id": None,
        "correlation_id": str(uuid.uuid5(_CORRELATION_NS, aggregate_id))
        if aggregate_id
        else None,
        "user_id": "system",
        "source_service": "week5-event-sourcing-platform",
    }

    return {
        "event_id": source_record.get("event_id"),
        "event_type": event_type,
        "aggregate_id": aggregate_id,
        "aggregate_type": "LoanApplication",
        "sequence_number": sequence_number,
        "payload": _parse_payload(source_record.get("payload") or ""),
        "schema_version": "1.0",
        "occurred_at": source_record.get("recorded_at"),
        "recorded_at": source_record.get("recorded_at"),
        "metadata": metadata,
    }


def migrate() -> None:
    """Read week5_events.csv, transform every row, write canonical JSONL."""
    print(f"Starting migration: {SOURCE_FILE} -> {TARGET_FILE}")

    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not SOURCE_FILE.exists():
        print(f"ERROR: Source file '{SOURCE_FILE}' not found.")
        raise SystemExit(1)

    print(f"Reading source records from: {SOURCE_FILE}")
    source_records: list[dict] = []
    with SOURCE_FILE.open("r", encoding="utf-8", newline="") as src:
        reader = csv.DictReader(src)
        for row in reader:
            source_records.append(dict(row))

    print(f"  Loaded {len(source_records)} source record(s).")

    canonical_records: list[dict] = []
    for source_record in source_records:
        canonical_records.append(transform_record(source_record))

    print(f"  Transformation complete. {len(canonical_records)} canonical record(s) ready.")

    with TARGET_FILE.open("w", encoding="utf-8") as dst:
        for record in canonical_records:
            dst.write(json.dumps(record) + "\n")

    print(f"Migration complete. {len(canonical_records)} records written to {TARGET_FILE}")


if __name__ == "__main__":
    migrate()
