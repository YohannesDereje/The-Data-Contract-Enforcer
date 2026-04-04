import hashlib
import json
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path


SOURCE_FILE = Path("outputs/week1/intent_records.jsonl")
TARGET_FILE = Path("outputs/week2/verdicts.jsonl")

# Stable rubric fingerprint — same hash for every run.
_RUBRIC_ID = hashlib.sha256(b"rubric-v1.2.0").hexdigest()
_RUBRIC_VERSION = "1.2.0"

_CRITERIA = ["Correctness", "Readability", "Security", "Maintainability", "Testability"]


def generate_synthetic_verdict(code_ref: dict) -> dict:
    """Generate one realistic verdict record for a given code reference.

    Args:
        code_ref: A single code_ref dict from an intent_record
                  (must contain at least 'file' and 'symbol' keys).

    Returns:
        A canonical verdict_record dictionary.
    """
    file_path = code_ref.get("file", "unknown")
    overall_score = round(random.uniform(3.0, 4.9), 3)

    # Pick 2–3 criteria at random and score each.
    selected_criteria = random.sample(_CRITERIA, k=random.randint(2, 3))
    scores = {}
    for criterion in selected_criteria:
        scores[criterion] = {
            "score": random.randint(3, 5),
            "evidence": [
                f"The implementation in {file_path} was evaluated against {criterion.lower()} standards."
            ],
            "notes": "Consider adding more comments.",
        }

    return {
        "verdict_id": str(uuid.uuid4()),
        "target_ref": file_path,
        "rubric_id": _RUBRIC_ID,
        "rubric_version": _RUBRIC_VERSION,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "confidence": round(random.uniform(0.85, 0.98), 3),
        "overall_score": overall_score,
        "overall_verdict": "PASS" if overall_score > 2.5 else "WARN",
        "scores": scores,
    }


def generate_data() -> None:
    """Read intent records, generate one verdict per code_ref, and write output."""
    if not SOURCE_FILE.exists():
        print(f"Error: Input file not found: {SOURCE_FILE}")
        print("Run 'python outputs/migrate/generate_synthetic_week1.py' first.")
        raise SystemExit(1)

    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)

    print(f"Reading intent records from: {SOURCE_FILE}")
    intent_records: list[dict] = []
    with SOURCE_FILE.open("r", encoding="utf-8") as src:
        for line in src:
            line = line.strip()
            if line:
                intent_records.append(json.loads(line))

    print(f"  Loaded {len(intent_records)} intent record(s).")

    all_verdicts: list[dict] = []
    for intent_record in intent_records:
        for code_ref in intent_record.get("code_refs", []):
            all_verdicts.append(generate_synthetic_verdict(code_ref))

    print(f"  Generated {len(all_verdicts)} verdict record(s) "
          f"(from {sum(len(r.get('code_refs', [])) for r in intent_records)} total code refs).")

    with TARGET_FILE.open("w", encoding="utf-8") as dst:
        for verdict in all_verdicts:
            dst.write(json.dumps(verdict) + "\n")

    print(f"Done. {len(all_verdicts)} verdicts written to {TARGET_FILE}")


if __name__ == "__main__":
    generate_data()
