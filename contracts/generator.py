import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
import yaml
import numpy as np
import pandas as pd
from ydata_profiling import ProfileReport

# --- (Serializer and Data Map) ---
def _json_serializer(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.floating,)): return float(obj)
    if isinstance(obj, (np.bool_,)): return bool(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

SYSTEM_DATA_MAP: dict[str, str] = {
    "week1": "outputs/week1/intent_records.jsonl", "week2": "outputs/week2/verdicts.jsonl",
    "week3": "outputs/week3/extractions.jsonl", "week4": "outputs/week4/lineage_snapshots.jsonl",
    "week5": "outputs/week5/events.jsonl", "langsmith": "outputs/traces/runs.jsonl",
}

# --- Data Loading ---
def load_and_flatten_data(system_name: str, records: list[dict]) -> pd.DataFrame:
    print("Flattening records for profiling...")
    if system_name == 'week1':
        df = pd.json_normalize(records, record_path=['code_refs'], meta=['intent_id', 'description', 'created_at'], meta_prefix='meta_', sep='.')
    elif system_name == 'week3':
        df = pd.json_normalize(records, record_path=['extracted_facts'], meta=['doc_id', 'source_path', 'extraction_model', 'extracted_at', ['token_count', 'input'], ['token_count', 'output']], meta_prefix='meta_', sep='.')
    else:
        df = pd.DataFrame([pd.json_normalize(rec, sep='.').to_dict('records')[0] for rec in records])
    print(f"  DataFrame shape after flattening: {df.shape}")
    return df

def extract_profiling_metadata(df: pd.DataFrame) -> dict[str, Any]:
    print("\nRunning ydata-profiling...")
    profile = ProfileReport(df, minimal=True, title="Data Profile")
    description = profile.get_description()
    metadata: dict[str, Any] = {}
    for col, info in description.variables.items():
        col_meta = {"type": info.get("type"), "p_missing": info.get("p_missing"), "is_unique": info.get("is_unique")}
        if info.get("type") == "Numeric":
            col_meta.update({k: info.get(k) for k in ["min", "max"]})
        metadata[col] = col_meta
    return metadata

# --- Schema Building ---
def build_schema_from_sample(data: Any, path: str, flat_profiling_meta: dict) -> dict:
    schema: dict[str, Any] = {}
    if isinstance(data, dict):
        schema["type"] = "object"
        schema["properties"] = {key: build_schema_from_sample(value, f"{path}.{key}" if path else key, flat_profiling_meta) for key, value in data.items()}
    elif isinstance(data, list):
        schema["type"] = "array"
        if data: schema["items"] = build_schema_from_sample(data[0], path, flat_profiling_meta)
    else:
        if isinstance(data, bool): schema["type"] = "boolean"
        elif isinstance(data, (int, float)): schema["type"] = "number"
        else: schema["type"] = "string"

    lookup_path = path
    if path.startswith("meta_"): lookup_path = path.replace("meta_", "")
    
    # Special handling for flattened meta fields from json_normalize
    if path in ['intent_id', 'description', 'created_at', 'doc_id', 'source_path', 'extraction_model', 'extracted_at']:
        lookup_path = f"meta_{path}"

    if lookup_path in flat_profiling_meta:
        meta = flat_profiling_meta[lookup_path]
        p_missing = meta.get('p_missing') or 0.0
        schema["description"] = f"Field '{path}' of type {schema.get('type')}." + (f" Warning: {p_missing:.1%} missing." if p_missing > 0 else "")
        schema["required"] = (p_missing == 0.0)
        schema["unique"] = bool(meta.get("is_unique"))
        if schema.get("type") == "number":
            min_val, max_val = meta.get("min"), meta.get("max")
            if "confidence" in path: min_val, max_val = 0.0, 1.0
            if min_val is not None: schema["minimum"] = min_val
            if max_val is not None: schema["maximum"] = max_val
    return schema

def create_bitol_contract(system_name: str, first_record: dict, profiling_metadata: dict[str, Any], subscriptions: list[dict] | None = None) -> dict[str, Any]:
    contract_id = f"{system_name}-contract-v1"
    contract: dict[str, Any] = {"kind": "DataContract", "apiVersion": "v3.0.0", "id": contract_id, "info": {"title": f"{system_name.capitalize()} Contract", "version": "1.0.0"}, "servers": {"local": {"type": "local", "path": SYSTEM_DATA_MAP[system_name], "format": "jsonl"}}, "schema": {}, "quality": {}, "lineage": {}}
    contract["schema"] = build_schema_from_sample(first_record, "", profiling_metadata)
    if subscriptions:
        contract["lineage"] = {"downstream": [s for s in subscriptions if s.get("contract_id") == contract_id]}

    checks: list[dict[str, Any]] = []
    for column_name, column_meta in profiling_metadata.items():
        checks.append({
            "type": "missing_count",
            "column": column_name,
            "must_be": "=",
            "value": 0,
            "name": f"{column_name}_no_missing_values",
        })

        if column_meta.get("is_unique"):
            checks.append({
                "type": "duplicate_count",
                "column": column_name,
                "must_be": "=",
                "value": 0,
                "name": f"{column_name}_no_duplicates",
            })

        if column_meta.get("type") == "Numeric":
            if "confidence" in column_name:
                col_min, col_max = 0.0, 1.0
            else:
                col_min = column_meta.get("min")
                col_max = column_meta.get("max")

            if col_min is not None:
                checks.append({
                    "type": "min", "column": column_name,
                    "must_be": ">=", "value": col_min,
                    "name": f"{column_name}_min_value",
                })
            if col_max is not None:
                checks.append({
                    "type": "max", "column": column_name,
                    "must_be": "<=", "value": col_max,
                    "name": f"{column_name}_max_value",
                })

    contract["quality"] = {
        "type": "SodaChecks",
        "checks": checks,
    }
    return contract

def create_dbt_schema_yml(system_name: str, profiling_metadata: dict[str, Any]) -> dict[str, Any]:
    columns = [{"name": c, "tests": (["not_null"] if (m.get("p_missing") or 1)==0 else []) + (["unique"] if m.get("is_unique") else [])} for c, m in profiling_metadata.items()]
    return {"version": 2, "models": [{"name": f"{system_name}_model", "columns": columns}]}

# --- Save Functions ---
def save_contract_to_yaml(system_name: str, contract: dict[str, Any]) -> None:
    output_dir = Path("generated_contracts"); output_dir.mkdir(exist_ok=True)
    filename_map = {"week1": "week1_intent_records.yaml", "week2": "week2_verdicts.yaml", "week3": "week3_extractions.yaml", "week4": "week4_lineage.yaml", "week5": "week5_events.yaml", "langsmith": "langsmith_traces.yaml"}
    output_path = output_dir / filename_map.get(system_name, f"{system_name}_contract.yaml")
    with output_path.open("w", encoding="utf-8") as fh: yaml.dump(json.loads(json.dumps(contract, default=_json_serializer)), fh, sort_keys=False, indent=2)
    print(f"Contract saved to: {output_path}")

def save_dbt_schema_to_yaml(system_name: str, dbt_schema: dict[str, Any]) -> None:
    output_dir = Path("generated_contracts"); output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"{system_name}_schema.yml"
    with output_path.open("w", encoding="utf-8") as fh: yaml.dump(json.loads(json.dumps(dbt_schema, default=_json_serializer)), fh, sort_keys=False, indent=2)
    print(f"dbt schema saved to: {output_path}")

def save_schema_snapshot(contract_id: str, schema_dict: dict[str, Any]) -> None:
    """Save a timestamped JSON snapshot of the contract schema.

    Args:
        contract_id: The contract identifier (e.g. 'week3-contract-v1').
        schema_dict: The 'schema' value from the generated contract dictionary.
    """
    snapshots_dir = Path("schema_snapshots")
    contract_dir = snapshots_dir / contract_id
    contract_dir.mkdir(parents=True, exist_ok=True)

    filename = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ") + ".json"
    output_path = contract_dir / filename

    with output_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(schema_dict, indent=2, default=_json_serializer))

    print(f"Schema snapshot saved to: {output_path}")


# --- Main Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Data Contract Generator")
    parser.add_argument("system_name", help=f"System to generate contract for. Available: {', '.join(SYSTEM_DATA_MAP)}")
    args = parser.parse_args()
    subscriptions = []
    try:
        with Path("contract_registry/subscriptions.yaml").open("r") as rf:
            subscriptions = yaml.safe_load(rf).get("subscriptions", [])
        print(f"Loaded {len(subscriptions)} subscription(s).")
    except FileNotFoundError: print("Warning: Registry not found.")
    
    try:
        file_path = Path(SYSTEM_DATA_MAP[args.system_name])
        records = [json.loads(line) for line in file_path.read_text(encoding="utf-8").splitlines() if line]
        if not records: raise ValueError("Data file is empty.")
        
        df = load_and_flatten_data(args.system_name, records)
        metadata = extract_profiling_metadata(df)
        
        print(f"\nBuilding Bitol contract for '{args.system_name}'...")
        contract = create_bitol_contract(args.system_name, records[0], metadata, subscriptions)
        save_contract_to_yaml(args.system_name, contract)
        save_schema_snapshot(contract["id"], contract["schema"])
        
        # --- RE-ADDED DBT GENERATION ---
        print(f"\nBuilding dbt schema for '{args.system_name}'...")
        dbt_schema = create_dbt_schema_yml(args.system_name, metadata)
        save_dbt_schema_to_yaml(args.system_name, dbt_schema)
        # -----------------------------
        
        print("\nGeneration complete.")

    except (ValueError, FileNotFoundError) as exc:
        print(f"\nError: {exc}"); raise SystemExit(1)
