import json
import uuid
from pathlib import Path
import pandas as pd

# The source file from your project root
SOURCE_FILE = Path("week5_events.csv")
TARGET_FILE = Path("outputs/week5/events.jsonl")

def parse_payload(raw: object) -> dict:
    """Parse a JSON string into a dict; return empty dict on failure."""
    if not raw or (isinstance(raw, float)):
        return {}
    try:
        return json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return {}

def migrate():
    print(f"--- Starting Week 5 Migration ---")
    print(f"Source file: {SOURCE_FILE}")
    print(f"Target file: {TARGET_FILE}")
    
    if not SOURCE_FILE.is_file():
        print(f"ERROR: Source file not found at {SOURCE_FILE}")
        print("Please ensure 'week5_events.csv' is in the project root directory.")
        return

    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    # Reading with dtype=str is a good defensive practice
    df = pd.read_csv(SOURCE_FILE, dtype=str)
    print(f"Loaded {len(df)} rows from {SOURCE_FILE}")

    namespace = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    correlation_cache: dict[str, str] = {}
    def get_correlation_id(aggregate_id: str) -> str:
        if aggregate_id not in correlation_cache:
            correlation_cache[aggregate_id] = str(uuid.uuid5(namespace, aggregate_id))
        return correlation_cache[aggregate_id]

    count = 0
    with TARGET_FILE.open("w", encoding="utf-8") as dst:
        for _, row in df.iterrows():
            # Use the ACTUAL column names from your CSV
            aggregate_id = row.get("entity_id")
            
            # Handle potential missing sequence numbers
            sequence_num_str = row.get("version")
            sequence_number = int(sequence_num_str) if pd.notna(sequence_num_str) else None

            canonical = {
                "event_id": row.get("event_id"),
                "event_type": row.get("event_type"),
                "aggregate_id": aggregate_id,
                "aggregate_type": "LoanApplication",
                "sequence_number": sequence_number,
                "payload": parse_payload(row.get("event_data")), # Use 'event_data'
                "schema_version": "1.0",
                "occurred_at": row.get("timestamp"), # Use 'timestamp'
                "recorded_at": row.get("timestamp"), # Use 'timestamp'
                "metadata": {
                    "causation_id": None,
                    "correlation_id": get_correlation_id(str(aggregate_id)) if aggregate_id else None,
                    "user_id": "system",
                    "source_service": "database-export",
                },
            }
            dst.write(json.dumps(canonical) + "\n")
            count += 1
            
    print(f"\n--- Migration Complete ---")
    print(f"Successfully migrated {count} records to {TARGET_FILE}")

if __name__ == "__main__":
    migrate()
