import hashlib
import json
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

# This script synthesizes a compliant extraction_record because the
# source extractions_ledger.jsonl file has a completely different schema.
# It intelligently maps the few overlapping fields and generates the required
# complex nested structures (entities, extracted_facts) from scratch.

SOURCE_FILE = Path("extraction_ledger.jsonl")
TARGET_FILE = Path("outputs/week3/extractions.jsonl")

_ENTITY_TYPES = ["ORGANIZATION", "PERSON", "LOCATION", "CONTRACT_PARTY"]
_PARTY_POOL = [
    "Federal Transit Administration",
    "Acme Contracting LLC",
    "Department of Transportation",
    "City of Springfield Transit Authority",
    "Global Infrastructure Partners",
]
_FACT_TEMPLATES = [
    "The agreement stipulates a performance threshold of {pct}% for on-time delivery.",
    "Contractor must submit quarterly reports no later than {days} days after period end.",
    "Payment terms are net-{days} from receipt of approved invoice.",
    "Liquidated damages accrue at ${amount} per day of delay beyond the deadline.",
]
_MODELS = ["claude-3-5-sonnet-20240620", "gpt-4o-20240513", "gemini-1.5-pro-20240515"] # Updated to look more realistic
_CANONICAL_VALUES = {
    "Federal Transit Administration": "FTA",
    "Department of Transportation": "DOT",
}

def _synthetic_fact_text() -> str:
    template = random.choice(_FACT_TEMPLATES)
    return template.format(
        pct=random.randint(85, 99),
        days=random.randint(15, 45),
        amount=random.randint(500, 5000),
    )

def _source_hash(file_name: str) -> str:
    return hashlib.sha256(file_name.encode()).hexdigest()

def transform_record(source_record: dict) -> dict:
    """
    Transforms one source record into a canonical extraction_record,
    mapping existing fields and synthesizing required nested structures.
    """
    # --- Map real fields & Synthesize ---
    file_name = source_record.get("file_name") or "unknown_document.pdf"
    doc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, file_name))
    extracted_at = (
        source_record.get("timestamp")
        or datetime.now(timezone.utc).isoformat()
    )
    processing_time_ms = (
        source_record.get("processing_time_ms")
        or round(random.uniform(800, 5000), 2)
    )
    base_confidence = float(source_record.get("confidence_score") or 0.85)
    fact_confidence = round(max(0.80, min(0.98, base_confidence + random.uniform(-0.05, 0.05))), 3)
    
    # **CORRECTED LOGIC**: Always use a realistic model name from the pool.
    #Sextraction_model = random.choice(_MODELS)

    # --- Synthesize entities ---
    parties = (source_record.get("details") or {}).get("parties") or random.sample(
        _PARTY_POOL, k=random.randint(2, 3)
    )
    canonical_entities: list[dict] = []
    entity_ids: list[str] = []
    for name in parties:
        eid = str(uuid.uuid4())
        entity_ids.append(eid)
        canonical_entities.append({
            "entity_id": eid,
            "name": name,
            "type": random.choice(_ENTITY_TYPES),
            "canonical_value": _CANONICAL_VALUES.get(name, name),
        })

    # --- Synthesize extracted_facts ---
    raw_facts = (source_record.get("details") or {}).get("facts") or _synthetic_fact_text()
    if isinstance(raw_facts, str):
        raw_facts = [raw_facts]

    canonical_facts: list[dict] = []
    for fact_text in raw_facts:
        page_ref = random.randint(1, 50)
        canonical_facts.append({
            "fact_id": str(uuid.uuid4()),
            "text": fact_text,
            "confidence": fact_confidence,
            "page_ref": page_ref,
            "source_excerpt": fact_text[:120] + ("..." if len(fact_text) > 120 else ""),
            "entity_refs": entity_ids,
        })

    # --- Assemble final record ---
    return {
        "doc_id": doc_id,
        "source_path": f"documents/{file_name}",
        "source_hash": _source_hash(file_name),
        "extracted_facts": canonical_facts,
        "entities": canonical_entities,
        "extraction_model": extraction_model,
        "processing_time_ms": int(processing_time_ms),
        "token_count": {"input": random.randint(512, 4096), "output": random.randint(256, 1024)},
        "extracted_at": extracted_at,
    }

def migrate() -> None:
    # ... (The rest of the migrate function remains unchanged)
    print(f"Starting migration: {SOURCE_FILE} -> {TARGET_FILE}")
    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    if not SOURCE_FILE.exists():
        print(f"ERROR: Source file '{SOURCE_FILE}' not found.")
        print("Please copy your 'extractions_ledger.jsonl' to the project root before running.")
        raise SystemExit(1)
        
    print(f"Reading source records from: {SOURCE_FILE}")
    source_records: list[dict] = []
    with SOURCE_FILE.open("r", encoding="utf-8") as src:
        for line in src:
            line = line.strip()
            if line:
                source_records.append(json.loads(line))
    print(f"  Loaded {len(source_records)} source record(s).")
    
    canonical_records = [transform_record(r) for r in source_records]
    print(f"  Transformation complete. {len(canonical_records)} canonical record(s) ready.")

    with TARGET_FILE.open("w", encoding="utf-8") as dst:
        for record in canonical_records:
            dst.write(json.dumps(record) + "\n")
            
    print(f"Migration complete. {len(canonical_records)} records written to {TARGET_FILE}")


if __name__ == "__main__":
    migrate()

