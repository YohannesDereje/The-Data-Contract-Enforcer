import json
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from jsonschema import ValidationError, validate
from sentence_transformers import SentenceTransformer

# Load the model once at import time so repeated calls don't pay the cost.
embedding_model = SentenceTransformer("all-MiniLM-L6-v2")

# JSON Schema that every record must satisfy before being used in a prompt.
PROMPT_INPUT_SCHEMA = {
    '$schema': 'http://json-schema.org/draft-07/schema#',
    'type': 'object',
    'required': ['doc_id', 'source_path', 'content_preview'],
    'properties': {
        'doc_id':           {'type': 'string', 'minLength': 36, 'maxLength': 36},
        'source_path':      {'type': 'string', 'minLength': 1},
        'content_preview':  {'type': 'string', 'maxLength': 8000}
    },
    'additionalProperties': False
}


def check_embedding_drift(
    texts: list[str],
    baseline_path: str | Path,
    threshold: float = 0.05,
) -> dict:
    """Detect semantic drift between the current data and a saved baseline.

    Computes embeddings for a random sample of texts, calculates the sample
    centroid, and compares it to the stored baseline centroid using cosine
    distance.  On first run (no baseline) the centroid is saved as the baseline.

    Args:
        texts: All candidate text strings to sample from.
        baseline_path: Path where the baseline .npz file is stored or will
            be created.
        threshold: Maximum acceptable cosine distance before the check fails.

    Returns:
        A dictionary with keys:
            status        — 'BASELINE_SET', 'PASS', or 'FAIL'
            drift_score   — cosine distance from baseline (None when setting baseline)
            threshold     — the value used for comparison
            sample_size   — number of texts that were encoded
    """
    baseline_path = Path(baseline_path)

    sample_size = min(200, len(texts))
    sampled_texts = random.sample(texts, sample_size)

    print(f"Encoding {sample_size} text sample(s) with '{embedding_model._modules}' ...")
    embeddings: np.ndarray = embedding_model.encode(sampled_texts)
    current_centroid: np.ndarray = np.mean(embeddings, axis=0)

    if not baseline_path.exists():
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(str(baseline_path), centroid=current_centroid)
        print(f"Baseline saved to: {baseline_path}")
        return {
            "status": "BASELINE_SET",
            "drift_score": None,
            "threshold": threshold,
            "sample_size": sample_size,
        }

    baseline_data = np.load(str(baseline_path))
    baseline_centroid: np.ndarray = baseline_data["centroid"]

    dot = np.dot(current_centroid, baseline_centroid)
    norm_product = np.linalg.norm(current_centroid) * np.linalg.norm(baseline_centroid)
    drift = float(1.0 - dot / norm_product)

    status = "PASS" if drift <= threshold else "FAIL"

    return {
        "status": status,
        "drift_score": round(drift, 6),
        "threshold": threshold,
        "sample_size": sample_size,
    }


def check_prompt_inputs(records: list[dict]) -> dict:
    """Validate a list of records against PROMPT_INPUT_SCHEMA.

    Invalid records are quarantined to a JSONL file rather than silently
    discarded, providing an audit trail for downstream investigation.

    Args:
        records: List of raw record dictionaries to validate.

    Returns:
        A summary dict with keys:
            total         — total records processed
            valid         — count that passed schema validation
            quarantined   — count that failed schema validation
            quarantine_path — path to the quarantine file, or None
    """
    valid_records: list[dict] = []
    quarantined_records: list[dict] = []

    for record in records:
        try:
            validate(instance=record, schema=PROMPT_INPUT_SCHEMA)
            valid_records.append(record)
        except ValidationError:
            quarantined_records.append(record)

    quarantine_path = None
    if quarantined_records:
        quarantine_dir = Path("outputs/quarantine")
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantine_file = quarantine_dir / "invalid_prompt_inputs.jsonl"
        with quarantine_file.open("w", encoding="utf-8") as fh:
            for rec in quarantined_records:
                fh.write(json.dumps(rec) + "\n")
        quarantine_path = str(quarantine_file)
        print(f"Quarantined {len(quarantined_records)} record(s) to: {quarantine_file}")

    return {
        "total": len(records),
        "valid": len(valid_records),
        "quarantined": len(quarantined_records),
        "quarantine_path": quarantine_path,
    }


def check_output_schema_violation_rate(
    verdict_records: list[dict],
    warn_threshold: float = 0.05,
    baseline_path: str | Path | None = None,
) -> dict:
    """Calculate the rate of LLM output records that violate the verdict enum.

    A record is considered a violation if its 'overall_verdict' field contains
    any value other than 'PASS', 'FAIL', or 'WARN'.

    On first run (no baseline file) the current rate is saved as the baseline
    and the trend is 'BASELINE_SET'. On subsequent runs the trend is compared
    against the saved baseline rate.

    Args:
        verdict_records: List of verdict dictionaries to analyse.
        warn_threshold: Maximum acceptable violation rate before status is WARN.
        baseline_path: Optional path to a JSON file storing the baseline rate.
            If None, baseline comparison is skipped.

    Returns:
        A dict with keys:
            total_outputs     — total records examined
            schema_violations — count of records with an invalid verdict value
            violation_rate    — violations / total (0.0 if total is 0)
            status            — 'PASS' or 'WARN'
            trend             — 'BASELINE_SET', 'rising', 'stable', or 'falling'
    """
    valid_verdicts = {"PASS", "FAIL", "WARN"}
    total_outputs = len(verdict_records)

    schema_violations = sum(
        1 for r in verdict_records
        if r.get("overall_verdict") not in valid_verdicts
    )

    violation_rate = schema_violations / total_outputs if total_outputs > 0 else 0.0
    status = "PASS" if violation_rate <= warn_threshold else "WARN"

    # --- Baseline comparison ---
    trend: str = "N/A"
    if baseline_path is not None:
        baseline_path = Path(baseline_path)
        if not baseline_path.exists():
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            with baseline_path.open("w", encoding="utf-8") as fh:
                json.dump({"baseline_rate": violation_rate}, fh)
            trend = "BASELINE_SET"
        else:
            with baseline_path.open("r", encoding="utf-8") as fh:
                baseline_data = json.load(fh)
            baseline_rate = baseline_data.get("baseline_rate", violation_rate)
            if violation_rate > baseline_rate:
                trend = "rising"
            elif violation_rate < baseline_rate:
                trend = "falling"
            else:
                trend = "stable"

    return {
        "total_outputs": total_outputs,
        "schema_violations": schema_violations,
        "violation_rate": round(violation_rate, 6),
        "status": status,
        "trend": trend,
    }


def log_ai_warning(check_name: str, details: dict, rate: float) -> None:
    """Append a simplified WARNING violation record to the central violation log.

    Args:
        check_name: The name of the AI check that triggered the warning.
        details: Additional context dict to include in the record.
        rate: The violation rate that exceeded the threshold.
    """
    record = {
        "violation_id": str(uuid.uuid4()),
        "check_id": check_name,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "severity": "WARNING",
        "message": f"LLM output violation rate of {rate:.2%} exceeds threshold.",
        "details": details,
    }

    log_path = Path("violation_log/violations.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    print(f"AI warning logged to: {log_path}  (id={record['violation_id']})")


if __name__ == "__main__":
    data_path = Path("outputs/week3/extractions.jsonl")

    texts: list[str] = []
    with data_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            for fact in record.get("extracted_facts", []):
                text = fact.get("text") or fact.get("value") or fact.get("fact_text")
                if text:
                    texts.append(str(text))

    print(f"Collected {len(texts)} text(s) from extracted_facts.")

    if not texts:
        print("No texts found in extracted_facts. Cannot run drift check.")
        raise SystemExit(1)

    baseline_path = Path("schema_snapshots/embedding_baseline.npz")

    result = check_embedding_drift(texts, baseline_path)

    print("\nEmbedding Drift Check Result:")
    print(f"  status      : {result['status']}")
    print(f"  drift_score : {result['drift_score']}")
    print(f"  threshold   : {result['threshold']}")
    print(f"  sample_size : {result['sample_size']}")

    if result["status"] == "BASELINE_SET":
        print(
            "\nTip: Run this script a second time to perform a real drift comparison "
            "against the baseline that was just saved."
        )

    # --- Test: Prompt Input Schema Validation ---
    print("\n--- Testing check_prompt_inputs ---")

    sample_records = [
        # Valid record: conforms to the new schema
        {
            "doc_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "source_path": "docs/report_2026.pdf",
            "content_preview": "This is a preview of the document content..."
        },
        # Invalid record: 'doc_id' is too short (violates minLength: 36)
        {
            "doc_id": "this-id-is-too-short",
            "source_path": "docs/brief.txt",
            "content_preview": "A short note."
        },
        # Invalid record: missing required field 'content_preview'
        {
            "doc_id": "f02b6d7d-a9ac-4189-8d98-1c7e8f74dc40",
            "source_path": "docs/no_preview.pdf"
        }
    ]

    validation_result = check_prompt_inputs(sample_records)

    print("\nPrompt Input Validation Result:")
    print(json.dumps(validation_result, indent=2))

    # --- Test: LLM Output Schema Violation Rate ---
    print("\n--- Testing check_output_schema_violation_rate ---")

    verdicts_path = Path("outputs/week2/verdicts.jsonl")
    verdict_records: list[dict] = []
    with verdicts_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                verdict_records.append(json.loads(line))

    # Inject one invalid record to make the test meaningful.
    verdict_records.append({
        "verdict_id": "test-invalid-001",
        "overall_verdict": "INVALID_STATUS",
        "reason": "Manually injected invalid record for violation rate test.",
    })

    output_violation_baseline = Path("schema_snapshots/output_violation_baseline.json")

    violation_result = check_output_schema_violation_rate(
        verdict_records,
        baseline_path=output_violation_baseline,
    )
    print(f"\nAnalysed {violation_result['total_outputs']} verdict record(s) "
          f"(including 1 injected invalid record).")
    print(f"  status         : {violation_result['status']}")
    print(f"  violation_rate : {violation_result['violation_rate']}")
    print(f"  trend          : {violation_result['trend']}")
    print(json.dumps(violation_result, indent=2))

    if violation_result["status"] == "WARN":
        log_ai_warning(
            check_name="check_output_schema_violation_rate",
            details=violation_result,
            rate=violation_result["violation_rate"],
        )
