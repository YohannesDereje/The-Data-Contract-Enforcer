import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Schema Analyzer — compares two versions of a data contract schema "
            "to detect breaking and non-breaking changes."
        ),
    )
    parser.add_argument(
        "--contract-id",
        required=True,
        help=(
            "The ID of the contract to analyze (e.g. 'week3-contract-v1'). "
            "Must match the subdirectory name under schema_snapshots/."
        ),
    )
    return parser


def load_snapshots(contract_id: str) -> tuple[dict, dict]:
    """Load the two most recent schema snapshots for a given contract.

    Snapshots are stored as timestamped JSON files under
    schema_snapshots/{contract_id}/. Files are sorted alphabetically, which
    is equivalent to chronological order because filenames use ISO 8601 format.

    Args:
        contract_id: The contract identifier (e.g. 'week3-contract-v1').

    Returns:
        A tuple of (old_schema, new_schema) where old_schema is the
        second-to-last snapshot and new_schema is the most recent.

    Raises:
        FileNotFoundError: If the snapshot directory does not exist.
        ValueError: If fewer than two snapshots are available to compare.
    """
    snapshot_dir = Path("schema_snapshots") / contract_id

    if not snapshot_dir.exists():
        raise FileNotFoundError(
            f"Snapshot directory not found: {snapshot_dir}\n"
            "Run the generator at least twice to create comparable snapshots."
        )

    snapshot_files = list(snapshot_dir.glob("*.json"))

    if len(snapshot_files) < 2:
        raise ValueError(
            f"Found only {len(snapshot_files)} snapshot(s) in {snapshot_dir}. "
            "At least two versions are needed to compare."
        )

    sorted_files = sorted(snapshot_files, key=lambda f: f.name)
    old_file = sorted_files[-2]
    new_file = sorted_files[-1]

    with old_file.open("r", encoding="utf-8") as fh:
        old_schema = json.load(fh)

    with new_file.open("r", encoding="utf-8") as fh:
        new_schema = json.load(fh)

    return old_schema, new_schema


def diff_schemas(old_schema: dict, new_schema: dict) -> list[dict]:
    """Compare two schema dictionaries and classify all detected changes.

    Operates on the top-level 'properties' of each schema (JSON Schema object
    style). Each detected change is classified for backward compatibility.

    Args:
        old_schema: The earlier schema snapshot dictionary.
        new_schema: The newer schema snapshot dictionary.

    Returns:
        A list of change dicts, each with keys:
        change_type, field, old_value, new_value, compatibility.
    """
    changes: list[dict] = []

    old_props: dict = old_schema.get("properties", {})
    new_props: dict = new_schema.get("properties", {})

    old_fields = set(old_props.keys())
    new_fields = set(new_props.keys())

    # --- Removed fields ---
    for field in old_fields - new_fields:
        changes.append({
            "change_type": "Remove Field",
            "field": field,
            "old_value": old_props[field].get("type"),
            "new_value": None,
            "compatibility": "No (BREAKING)",
        })

    # --- Added fields ---
    for field in new_fields - old_fields:
        field_def = new_props[field]
        is_required = field_def.get("required", False)
        changes.append({
            "change_type": "Add Required Field" if is_required else "Add Nullable Field",
            "field": field,
            "old_value": None,
            "new_value": field_def.get("type"),
            "compatibility": "No (BREAKING)" if is_required else "Yes",
        })

    # --- Modified fields ---
    for field in old_fields & new_fields:
        old_def = old_props[field]
        new_def = new_props[field]

        # Type narrowing: number → integer is a potential data-loss change.
        old_type = old_def.get("type")
        new_type = new_def.get("type")
        if old_type != new_type:
            if old_type == "number" and new_type == "integer":
                compatibility = "No - data loss"
            else:
                compatibility = "No (BREAKING)"
            changes.append({
                "change_type": "Narrow Type" if (old_type == "number" and new_type == "integer") else "Change Type",
                "field": field,
                "old_value": old_type,
                "new_value": new_type,
                "compatibility": compatibility,
            })

        # Required promotion: optional → required is breaking for consumers.
        old_required = old_def.get("required", False)
        new_required = new_def.get("required", False)
        if not old_required and new_required:
            changes.append({
                "change_type": "Make Field Required",
                "field": field,
                "old_value": old_required,
                "new_value": new_required,
                "compatibility": "No (BREAKING)",
            })

        # Enum shrinkage: removing an allowed value breaks existing data.
        old_enum = set(old_def.get("enum", []))
        new_enum = set(new_def.get("enum", []))
        if old_enum and not new_enum.issuperset(old_enum):
            removed_values = sorted(old_enum - new_enum)
            changes.append({
                "change_type": "Change Enum Values",
                "field": field,
                "old_value": sorted(old_enum),
                "new_value": sorted(new_enum),
                "compatibility": f"No (BREAKING) — removed: {removed_values}",
            })

    return changes


def generate_report(contract_id: str, changes: list[dict]) -> dict:
    """Build a schema evolution report from a list of detected changes.

    Args:
        contract_id: The contract identifier being analysed.
        changes: Output of diff_schemas.

    Returns:
        A report dictionary ready for serialisation.
    """
    compatibility_verdict = (
        "BREAKING"
        if any("BREAKING" in c.get("compatibility", "") for c in changes)
        else "COMPATIBLE"
    )

    return {
        "report_id": str(uuid.uuid4()),
        "contract_id": contract_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compatibility_verdict": compatibility_verdict,
        "changes": changes,
        "migration_checklist": [
            "Review breaking changes with downstream consumers."
        ],
        "rollback_plan": (
            "Revert the schema to the previous version and redeploy producer services."
        ),
    }


def save_report(report: dict) -> None:
    """Save a schema evolution report as a JSON file in validation_reports/.

    Args:
        report: The report dictionary produced by generate_report.
    """
    output_dir = Path("validation_reports")
    output_dir.mkdir(exist_ok=True)

    timestamp = (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H-%M-%SZ")
    )
    contract_id = report["contract_id"]
    filename = f"schema_evolution_{contract_id}_{timestamp}.json"
    output_path = output_dir / filename

    with output_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(report, indent=2))

    print(f"Schema evolution report saved to: {output_path}")


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    contract_id = args.contract_id

    try:
        snapshot_dir = Path("schema_snapshots") / contract_id
        sorted_files = sorted(snapshot_dir.glob("*.json"), key=lambda f: f.name)

        old_schema, new_schema = load_snapshots(contract_id)

        old_filename = sorted_files[-2].name
        new_filename = sorted_files[-1].name

        print(
            f"Successfully loaded two schemas for comparison: "
            f"{old_filename} and {new_filename}"
        )

        changes = diff_schemas(old_schema, new_schema)
        print(f"\nDetected {len(changes)} change(s).")

        report = generate_report(contract_id, changes)
        save_report(report)
        print(f"Compatibility verdict: {report['compatibility_verdict']}")

    except (FileNotFoundError, ValueError) as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1)
