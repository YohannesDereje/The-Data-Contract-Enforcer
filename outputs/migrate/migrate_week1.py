import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


TARGET_FILE = Path("outputs/week1/intent_records.jsonl")

_DESCRIPTIONS = [
    "Find all database connection strings and audit their security configuration.",
    "Refactor the user authentication flow to use JWT tokens.",
    "Implement caching for the main API endpoint to reduce latency.",
    "Identify all PII fields across the data pipeline and apply masking rules.",
]

_GOVERNANCE_TAGS = ["auth", "pii", "billing", "performance"]

_FILES = [
    "src/users/models.py",
    "src/api/views.py",
    "config/settings.py",
    "src/auth/middleware.py",
    "src/billing/service.py",
]

_SYMBOLS = [
    "get_user",
    "update_profile",
    "DATABASE_CONFIG",
    "authenticate_request",
    "calculate_invoice",
    "cache_response",
]


def generate_synthetic_intent_record(record_num: int) -> dict:
    """Generate one realistic, randomized intent_record dictionary.

    Args:
        record_num: The sequential record number (used for timestamp spacing).

    Returns:
        A canonical intent_record dictionary.
    """
    # Spread records across the past 30 days.
    offset_hours = random.randint(0, 30 * 24)
    created_at = (
        datetime.now(timezone.utc) - timedelta(hours=offset_hours)
    ).isoformat()

    # Random non-empty subset of governance tags (1–4 tags).
    num_tags = random.randint(1, len(_GOVERNANCE_TAGS))
    governance_tags = random.sample(_GOVERNANCE_TAGS, k=num_tags)

    # 1–3 code references per record.
    num_refs = random.randint(1, 3)
    code_refs = []
    for _ in range(num_refs):
        line_start = random.randint(10, 50)
        line_end = random.randint(line_start + 5, line_start + 25)
        code_refs.append({
            "file": random.choice(_FILES),
            "line_start": line_start,
            "line_end": line_end,
            "symbol": random.choice(_SYMBOLS),
            "confidence": round(random.uniform(0.75, 0.98), 3),
        })

    return {
        "intent_id": str(uuid.uuid4()),
        "description": random.choice(_DESCRIPTIONS),
        "created_at": created_at,
        "governance_tags": governance_tags,
        "code_refs": code_refs,
    }


def generate_data(num_records: int = 50) -> None:
    """Generate synthetic intent records and write them to TARGET_FILE.

    Args:
        num_records: Number of intent records to generate.
    """
    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)

    print(f"Generating {num_records} synthetic intent records...")

    with TARGET_FILE.open("w", encoding="utf-8") as dst:
        for i in range(num_records):
            record = generate_synthetic_intent_record(i)
            dst.write(json.dumps(record) + "\n")

    print(f"Done. {num_records} records written to {TARGET_FILE}")


if __name__ == "__main__":
    generate_data(num_records=50)
