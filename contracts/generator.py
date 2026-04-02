import argparse
import json
from pathlib import Path
from typing import Any

import yaml

import numpy as np
import pandas as pd
from ydata_profiling import ProfileReport


def _json_serializer(obj: Any) -> Any:
    """Coerce numpy scalars to native Python types for json.dumps."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


# Maps a system name to its canonical JSONL output file.
SYSTEM_DATA_MAP: dict[str, str] = {
    "week3": "outputs/week3/extractions.jsonl",
    "week4": "outputs/week4/lineage_snapshots.jsonl",
    "week5": "outputs/week5/events.jsonl",
}


def load_and_flatten_data(system_name: str) -> pd.DataFrame:
    """Load a system's canonical JSONL file and return a flat DataFrame.

    Each JSON record is flattened via pandas.json_normalize so that nested
    objects become dot-separated column names, e.g.:
        {'a': 1, 'b': {'c': 2}}  ->  {'a': 1, 'b.c': 2}

    Args:
        system_name: One of the keys defined in SYSTEM_DATA_MAP.

    Returns:
        A pandas DataFrame with one row per record and flat column names.

    Raises:
        ValueError: If system_name is not a recognised key.
        FileNotFoundError: If the resolved JSONL file does not exist.
    """
    if system_name not in SYSTEM_DATA_MAP:
        known = ", ".join(sorted(SYSTEM_DATA_MAP))
        raise ValueError(
            f"Unknown system '{system_name}'. Known systems: {known}"
        )

    file_path = Path(SYSTEM_DATA_MAP[system_name])
    print(f"Loading data for system '{system_name}' from: {file_path}")

    if not file_path.exists():
        raise FileNotFoundError(
            f"Data file not found: {file_path}\n"
            "Run the corresponding migrate script first."
        )

    records: list[dict] = []
    with file_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    print(f"  Loaded {len(records)} raw records.")

    # Flatten each record individually so nested keys become 'parent.child'.
    flat_records = [
        pd.json_normalize(record).to_dict(orient="records")[0]
        for record in records
    ]

    df = pd.DataFrame(flat_records)
    print(f"  DataFrame shape after flattening: {df.shape} "
          f"({df.shape[0]} rows x {df.shape[1]} columns)")
    return df


def extract_profiling_metadata(df: pd.DataFrame) -> dict[str, Any]:
    """Profile a DataFrame and return structural and statistical metadata.

    Uses ydata-profiling in minimal mode to analyse each column and extracts:
    - Structural properties (type, missing-value rate, uniqueness) for all columns.
    - Statistical properties (mean, std, min, max, percentiles) for Numeric columns.

    Args:
        df: The flattened DataFrame produced by load_and_flatten_data.

    Returns:
        A dictionary keyed by column name, each value being a dict of metadata.
    """
    print("\nRunning ydata-profiling (minimal=True) — this may take a moment...")
    profile = ProfileReport(df, minimal=True, title="Data Profile")
    description = profile.get_description()

    metadata: dict[str, Any] = {}

    for column_name, column_info in description.variables.items():

        col_meta: dict[str, Any] = {
            "type": column_info.get("type"),
            "p_missing": column_info.get("p_missing"),
            "is_unique": column_info.get("is_unique"),
        }

        col_meta["n_distinct"] = column_info.get("n_distinct")
        col_meta["distinct"] = list(
            column_info.get("value_counts_without_nan", {}).keys()
        )

        if column_info.get("type") == "Numeric":
            try:
                col_meta["mean"] = column_info["mean"]
                col_meta["std"] = column_info["std"]
                col_meta["min"] = column_info["min"]
                col_meta["max"] = column_info["max"]
                col_meta["p25"] = column_info["25%"]
                col_meta["p50"] = column_info["50%"]
                col_meta["p75"] = column_info["75%"]
            except KeyError:
                pass

        metadata[column_name] = col_meta

    return metadata


def find_downstream_consumers(
    data_file_path: str,
    lineage_graph: dict[str, Any],
) -> list[dict[str, Any]]:
    """Find all nodes that directly consume data_file_path in the lineage graph.

    Args:
        data_file_path: The canonical path of the file being profiled
            (e.g. 'outputs/week3/extractions.jsonl').
        lineage_graph: A lineage snapshot dictionary with 'nodes' and 'edges'.

    Returns:
        A list of consumer dicts, each with 'id', 'label', and 'description'.
        Returns an empty list if the node is not found or has no downstream edges.
    """
    node_id = f"file::{data_file_path}"

    nodes: list[dict[str, Any]] = lineage_graph.get("nodes", [])
    edges: list[dict[str, Any]] = lineage_graph.get("edges", [])

    # Confirm the node exists in the graph.
    known_ids = {n["node_id"] for n in nodes}
    if node_id not in known_ids:
        return []

    # Build a lookup from node_id -> node for O(1) target resolution.
    node_by_id: dict[str, dict[str, Any]] = {n["node_id"]: n for n in nodes}

    consumers: list[dict[str, Any]] = []
    for edge in edges:
        if edge.get("source") != node_id:
            continue
        target_id = edge.get("target", "")
        target_node = node_by_id.get(target_id, {})
        consumers.append({
            "id": target_id,
            "label": target_node.get("label", target_id),
            "description": (
                f"Downstream consumer via '{edge.get('relationship', 'UNKNOWN')}' "
                f"edge (confidence: {edge.get('confidence', 'N/A')})."
            ),
        })

    return consumers


_PROFILER_TO_BITOL: dict[str, str] = {
    "Numeric": "number",
    "Text": "string",
    "Boolean": "boolean",
    "Unsupported": "string",
}


def create_bitol_contract(
    system_name: str,
    profiling_metadata: dict[str, Any],
    lineage_graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transform profiling metadata into a Bitol-compliant contract dictionary.

    Args:
        system_name: The system key (e.g. 'week3') used for IDs and file paths.
        profiling_metadata: Output of extract_profiling_metadata.

    Returns:
        A dictionary representing the full Bitol DataContract.
    """
    contract: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "id": f"{system_name}-contract-v1",
        "info": {
            "title": f"{system_name.capitalize()} Data Contract",
            "version": "1.0.0",
            "owner": "data-engineering",
            "description": (
                f"Auto-generated data contract for the {system_name} system, "
                "produced by the Data Contract Enforcer pipeline."
            ),
        },
        "servers": {
            "local": {
                "type": "local",
                "path": SYSTEM_DATA_MAP[system_name],
                "format": "jsonl",
            }
        },
        "terms": {
            "usage": "Internal analytics and downstream contract validation only.",
            "limitations": "Not for use in production serving without manual review.",
        },
        "schema": {},
        "quality": {},
        "lineage": {},
    }

    # ----------------------------------------------------------------- lineage
    if lineage_graph is not None:
        consumers = find_downstream_consumers(
            SYSTEM_DATA_MAP[system_name], lineage_graph
        )
        contract["lineage"] = {"downstream": consumers}

    # ------------------------------------------------------------------ schema
    for column_name, column_meta in profiling_metadata.items():
        profiler_type = column_meta.get("type")
        p_missing = column_meta.get("p_missing") or 0.0
        is_unique = column_meta.get("is_unique") or False
        n_distinct = column_meta.get("n_distinct") or 0
        distinct_values = column_meta.get("distinct") or []

        bitol_type = _PROFILER_TO_BITOL.get(profiler_type, "string")

        description = f"Field '{column_name}' of type {bitol_type}."
        if p_missing > 0.0:
            description += f" Warning: {p_missing:.1%} of values are missing."

        field_schema: dict[str, Any] = {
            "type": bitol_type,
            "required": p_missing == 0.0,
            "unique": bool(is_unique),
            "description": description,
        }

        # Pattern inference for Text columns based on column name semantics.
        if profiler_type == "Text":
            col_lower = column_name.lower()
            if "uuid" in col_lower or (
                "id" in col_lower and "valid" not in col_lower
            ):
                field_schema["pattern"] = (
                    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                    r"-[0-9a-f]{4}-[0-9a-f]{12}$"
                )
            elif "email" in col_lower:
                field_schema["pattern"] = r"^[^@]+@[^@]+\.[^@]+$"

            # Enum inference for low-cardinality, non-unique Text columns.
            if n_distinct > 0 and n_distinct < 10 and not is_unique and distinct_values:
                field_schema["enum"] = [
                    str(v) for v in distinct_values if v is not None
                ]

        # Min/max bounds for Numeric columns.
        if profiler_type == "Numeric":
            if column_name == "confidence_score":
                field_schema["minimum"] = 0.0
                field_schema["maximum"] = 1.0
            else:
                col_min = column_meta.get("min")
                col_max = column_meta.get("max")
                if col_min is not None:
                    field_schema["minimum"] = col_min
                if col_max is not None:
                    field_schema["maximum"] = col_max

        contract["schema"][column_name] = field_schema

    # ------------------------------------------------------------------ quality
    checks: list[dict[str, Any]] = []

    for column_name, column_meta in profiling_metadata.items():
        profiler_type = column_meta.get("type")
        p_missing = column_meta.get("p_missing") or 0.0
        is_unique = column_meta.get("is_unique") or False

        # Missing-count check for every column.
        checks.append({
            "type": "missing_count",
            "column": column_name,
            "must_be": "=",
            "value": 0,
            "name": f"{column_name}_no_missing_values",
        })

        # Duplicate check for unique columns.
        if is_unique:
            checks.append({
                "type": "duplicate_count",
                "column": column_name,
                "must_be": "=",
                "value": 0,
                "name": f"{column_name}_no_duplicates",
            })

        # Range checks for numeric columns.
        if profiler_type == "Numeric":
            if column_name == "confidence_score":
                col_min, col_max = 0.0, 1.0
            else:
                col_min = column_meta.get("min")
                col_max = column_meta.get("max")

            if col_min is not None:
                checks.append({
                    "type": "min",
                    "column": column_name,
                    "must_be": ">=",
                    "value": col_min,
                    "name": f"{column_name}_min_value",
                })
            if col_max is not None:
                checks.append({
                    "type": "max",
                    "column": column_name,
                    "must_be": "<=",
                    "value": col_max,
                    "name": f"{column_name}_max_value",
                })

    contract["quality"] = {
        "type": "SodaChecks",
        "checks": checks,
    }

    return contract


def create_dbt_schema_yml(
    system_name: str,
    profiling_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build a dbt-compatible schema.yml dictionary from profiling metadata.

    Maps contract constraints to native dbt tests:
    - required (p_missing == 0) -> 'not_null'
    - unique                    -> 'unique'
    - low-cardinality Text      -> 'accepted_values'

    Args:
        system_name: Used to name the dbt model (e.g. 'week3').
        profiling_metadata: Output of extract_profiling_metadata.

    Returns:
        A dictionary ready to be serialized as a dbt schema.yml file.
    """
    columns: list[dict[str, Any]] = []

    for column_name, column_meta in profiling_metadata.items():
        profiler_type = column_meta.get("type")
        p_missing = column_meta.get("p_missing") or 0.0
        is_unique = column_meta.get("is_unique") or False
        n_distinct = column_meta.get("n_distinct") or 0
        distinct_values = column_meta.get("distinct") or []

        tests: list[Any] = []

        if p_missing == 0.0:
            tests.append("not_null")

        if is_unique:
            tests.append("unique")

        # Low-cardinality, non-unique Text columns get an accepted_values test.
        if (
            profiler_type == "Text"
            and 0 < n_distinct < 10
            and not is_unique
            and distinct_values
        ):
            enum_list = [str(v) for v in distinct_values if v is not None]
            tests.append({"accepted_values": {"values": enum_list}})

        columns.append({"name": column_name, "tests": tests})

    return {
        "version": 2,
        "models": [
            {
                "name": f"{system_name}_extractions",
                "columns": columns,
            }
        ],
    }


def save_dbt_schema_to_yaml(system_name: str, dbt_schema: dict[str, Any]) -> None:
    """Serialize a dbt schema dictionary to a .yml file in generated_contracts/.

    Args:
        system_name: Used to derive the output filename (e.g. 'week3').
        dbt_schema: The dbt schema dictionary produced by create_dbt_schema_yml.
    """
    output_dir = Path("generated_contracts")
    output_dir.mkdir(exist_ok=True)

    output_path = output_dir / f"{system_name}_extractions_dbt.yml"

    native_schema = json.loads(json.dumps(dbt_schema, default=_json_serializer))

    with output_path.open("w", encoding="utf-8") as fh:
        yaml.dump(native_schema, fh, sort_keys=False, indent=2)

    print(f"dbt schema saved to: {output_path}")


def save_contract_to_yaml(system_name: str, contract: dict[str, Any]) -> None:
    """Serialize a Bitol contract dictionary to a YAML file.

    Args:
        system_name: Used to derive the output filename (e.g. 'week3').
        contract: The complete Bitol contract dictionary.
    """
    output_dir = Path("generated_contracts")
    output_dir.mkdir(exist_ok=True)

    output_path = output_dir / f"{system_name}_extractions.yaml"

    # Round-trip through JSON to coerce numpy scalars (bool_, int64, float64)
    # to native Python types; yaml.dump otherwise emits !!python/object tags.
    native_contract = json.loads(json.dumps(contract, default=_json_serializer))

    with output_path.open("w", encoding="utf-8") as fh:
        yaml.dump(native_contract, fh, sort_keys=False, indent=2)

    print(f"Contract saved to: {output_path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Data Contract Generator — profiles a system's output "
                    "and generates a baseline data contract.",
    )
    parser.add_argument(
        "system_name",
        help=(
            "Name of the system to generate a contract for "
            "(e.g., 'week3', 'week5'). "
            f"Available systems: {', '.join(sorted(SYSTEM_DATA_MAP))}."
        ),
    )
    return parser


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    lineage_graph: dict[str, Any] | None = None
    lineage_path = Path("outputs/week4/lineage_snapshots.jsonl")
    try:
        with lineage_path.open("r", encoding="utf-8") as lf:
            first_line = lf.readline().strip()
            if first_line:
                lineage_graph = json.loads(first_line)
                print(f"Loaded lineage graph from: {lineage_path}")
    except FileNotFoundError:
        print(f"Warning: Lineage file not found at {lineage_path}. "
              "Lineage section will be empty.")

    try:
        df = load_and_flatten_data(args.system_name)
        print(f"\nSuccessfully loaded and flattened data for '{args.system_name}'.")
        metadata = extract_profiling_metadata(df)
        print(f"\nBuilding Bitol contract for '{args.system_name}'...")
        contract = create_bitol_contract(args.system_name, metadata, lineage_graph)
        downstream = contract["lineage"].get("downstream", [])
        print(f"Contract generated with {len(contract['schema'])} schema fields, "
              f"{len(contract['quality']['checks'])} quality checks, "
              f"and {len(downstream)} downstream consumer(s).")
        save_contract_to_yaml(args.system_name, contract)
        dbt_schema = create_dbt_schema_yml(args.system_name, metadata)
        save_dbt_schema_to_yaml(args.system_name, dbt_schema)
        print(f"dbt counterpart generated for '{args.system_name}'.")
    except (ValueError, FileNotFoundError) as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1)
