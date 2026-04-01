import json
from pathlib import Path


SOURCE_FILE = Path("Extraction_ledger.jsonl")
TARGET_FILE = Path("outputs/week3/extractions.jsonl")


def transform_record(record: dict) -> dict:
    details = record.get("details", {}) or {}

    extracted_info = {}
    for key, value in details.items():
        if key == "case_id":
            extracted_info["case_number"] = value
        else:
            extracted_info[key] = value

    canonical = {
        "record_id": record.get("extraction_id"),
        "doc_id": record.get("document_id"),
        "confidence_score": record.get("confidence"),
        "llm_trace_id": record.get("llm_trace"),
        "extracted_info": extracted_info,
    }

    for key, value in record.items():
        if key not in ("extraction_id", "document_id", "confidence", "llm_trace", "details"):
            canonical[key] = value

    return canonical


def migrate():
    print(f"Starting migration: {SOURCE_FILE} -> {TARGET_FILE}")

    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with (
        SOURCE_FILE.open("r", encoding="utf-8") as src,
        TARGET_FILE.open("w", encoding="utf-8") as dst,
    ):
        for line in src:
            line = line.strip()
            if not line:
                continue

            record = json.loads(line)
            canonical = transform_record(record)
            dst.write(json.dumps(canonical) + "\n")
            count += 1

    print(f"Migration complete. {count} records migrated to {TARGET_FILE}")


if __name__ == "__main__":
    migrate()
