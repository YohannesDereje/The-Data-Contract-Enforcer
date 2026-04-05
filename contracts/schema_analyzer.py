import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Schema Analyzer — recursively compares two versions of a data "
            "contract schema to detect breaking and non-breaking changes and "
            "generates a comprehensive migration impact report."
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
    parser.add_argument(
        "--since",
        default="7 days ago",
        help=(
            "Only compare snapshots newer than this date expression "
            "(e.g. '7 days ago', '30 days ago'). Default: '7 days ago'."
        ),
    )
    return parser


def load_snapshots(contract_id: str) -> tuple[dict, dict, str, str]:
    """Load the two most recent schema snapshots for a given contract.

    Snapshots are stored as timestamped JSON files under
    schema_snapshots/{contract_id}/. Files are sorted alphabetically, which
    is equivalent to chronological order because filenames use ISO 8601 format.

    Args:
        contract_id: The contract identifier (e.g. 'week3-contract-v1').

    Returns:
        A tuple of (old_schema, new_schema, old_filename, new_filename).

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

    return old_schema, new_schema, old_file.name, new_file.name


def _diff_properties(
    old_props: dict,
    new_props: dict,
    path_prefix: str,
    changes: list[dict],
) -> None:
    """Recursively diff two 'properties' dicts and append changes to the list.

    Handles nested objects (via their own 'properties') and arrays (via
    'items.properties'), reporting every change with a dot-notation path.

    Args:
        old_props: The properties dict from the older schema node.
        new_props: The properties dict from the newer schema node.
        path_prefix: Dot-notation prefix accumulated so far (empty at root).
        changes: Mutable list to append detected change dicts to.
    """
    old_fields = set(old_props.keys())
    new_fields = set(new_props.keys())

    def full_path(field: str) -> str:
        return f"{path_prefix}.{field}" if path_prefix else field

    # --- Removed fields ---
    for field in old_fields - new_fields:
        changes.append({
            "change_type": "Remove Field",
            "field": full_path(field),
            "old_value": old_props[field].get("type"),
            "new_value": None,
            "compatibility": "No (BREAKING)",
            "severity": "CRITICAL",
        })

    # --- Added fields ---
    for field in new_fields - old_fields:
        field_def = new_props[field]
        is_required = field_def.get("required", False)
        changes.append({
            "change_type": "Add Required Field" if is_required else "Add Nullable Field",
            "field": full_path(field),
            "old_value": None,
            "new_value": field_def.get("type"),
            "compatibility": "No (BREAKING)" if is_required else "Yes",
            "severity": "HIGH" if is_required else "LOW",
        })

    # --- Modified fields ---
    for field in old_fields & new_fields:
        old_def = old_props[field]
        new_def = new_props[field]
        fp = full_path(field)

        # Type changes.
        old_type = old_def.get("type")
        new_type = new_def.get("type")
        if old_type != new_type:
            if old_type == "number" and new_type == "integer":
                changes.append({
                    "change_type": "Narrow Type",
                    "field": fp,
                    "old_value": old_type,
                    "new_value": new_type,
                    "compatibility": "No (BREAKING) — data loss risk",
                    "severity": "CRITICAL",
                })
            elif old_type == "integer" and new_type == "number":
                changes.append({
                    "change_type": "Widen Type",
                    "field": fp,
                    "old_value": old_type,
                    "new_value": new_type,
                    "compatibility": "Yes",
                    "severity": "LOW",
                })
            else:
                changes.append({
                    "change_type": "Change Type",
                    "field": fp,
                    "old_value": old_type,
                    "new_value": new_type,
                    "compatibility": "No (BREAKING)",
                    "severity": "CRITICAL",
                })

        # Required promotion.
        if not old_def.get("required", False) and new_def.get("required", False):
            changes.append({
                "change_type": "Make Field Required",
                "field": fp,
                "old_value": False,
                "new_value": True,
                "compatibility": "No (BREAKING)",
                "severity": "HIGH",
            })

        # Enum shrinkage.
        old_enum = set(old_def.get("enum", []))
        new_enum = set(new_def.get("enum", []))
        if old_enum and not new_enum.issuperset(old_enum):
            removed = sorted(old_enum - new_enum)
            changes.append({
                "change_type": "Remove Enum Value",
                "field": fp,
                "old_value": sorted(old_enum),
                "new_value": sorted(new_enum),
                "compatibility": f"No (BREAKING) — removed: {removed}",
                "severity": "HIGH",
            })

        # Recurse into nested object properties.
        if old_type == "object" and new_type == "object":
            _diff_properties(
                old_def.get("properties", {}),
                new_def.get("properties", {}),
                fp,
                changes,
            )

        # Recurse into array item properties.
        if old_type == "array" and new_type == "array":
            old_items = old_def.get("items", {})
            new_items = new_def.get("items", {})
            if old_items.get("type") == "object" and new_items.get("type") == "object":
                _diff_properties(
                    old_items.get("properties", {}),
                    new_items.get("properties", {}),
                    f"{fp}.items.properties",
                    changes,
                )


def diff_schemas(old_schema: dict, new_schema: dict) -> list[dict]:
    """Recursively compare two schema dictionaries and classify all changes.

    Walks the full nested structure of both schemas, reporting every
    change with a dot-notation path and a severity classification.

    Args:
        old_schema: The earlier schema snapshot dictionary.
        new_schema: The newer schema snapshot dictionary.

    Returns:
        A list of change dicts, each with keys:
        change_type, field, old_value, new_value, compatibility, severity.
    """
    changes: list[dict] = []

    old_props = old_schema.get("properties", {})
    new_props = new_schema.get("properties", {})

    _diff_properties(old_props, new_props, "", changes)
    return changes


def load_registry() -> list[dict]:
    """Load subscription entries from the contract registry.

    Returns:
        List of subscription dicts, or an empty list if the registry is missing.
    """
    registry_path = Path("contract_registry/subscriptions.yaml")
    try:
        with registry_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data.get("subscriptions", [])
    except FileNotFoundError:
        print("Warning: contract_registry/subscriptions.yaml not found.")
        return []


def get_impacted_consumers(
    changes: list[dict],
    subscriptions: list[dict],
    contract_id: str,
) -> list[dict]:
    """Identify which downstream consumers are impacted by breaking changes.

    For each breaking change, the function checks every subscriber's
    'breaking_fields' list to determine whether the changed field is declared
    as a dependency.

    Args:
        changes: Output of diff_schemas.
        subscriptions: The full list of subscription dicts from the registry.
        contract_id: Only subscriptions for this contract are considered.

    Returns:
        A list of impact dicts, one per (subscriber, breaking_change) pair,
        each containing the subscriber ID, contact, affected field, failure
        mode description, and original change dict.
    """
    breaking_changes = [
        c for c in changes if "BREAKING" in c.get("compatibility", "")
    ]

    contract_subs = [
        s for s in subscriptions if s.get("contract_id") == contract_id
    ]

    impacts: list[dict] = []
    for sub in contract_subs:
        breaking_field_defs = sub.get("breaking_fields", [])
        breaking_field_names = {
            bf["field"] if isinstance(bf, dict) else bf
            for bf in breaking_field_defs
        }
        breaking_field_reasons = {
            (bf["field"] if isinstance(bf, dict) else bf): bf.get("reason", "")
            for bf in breaking_field_defs
            if isinstance(bf, dict)
        }

        for change in breaking_changes:
            changed_field = change["field"]
            # Match if the changed field starts with any declared breaking field
            # (handles nested paths like 'extracted_facts.confidence').
            matched_bf = next(
                (bf for bf in breaking_field_names if changed_field.startswith(bf)),
                None,
            )
            if matched_bf is not None:
                impacts.append({
                    "subscriber_id": sub["subscriber_id"],
                    "contact": sub.get("contact", "unknown"),
                    "affected_field": changed_field,
                    "failure_mode": breaking_field_reasons.get(matched_bf, "Impact reason not documented."),
                    "change": change,
                })

    return impacts


def generate_report(
    contract_id: str,
    changes: list[dict],
    subscriptions: list[dict],
) -> dict:
    """Build a comprehensive schema evolution and migration impact report.

    Args:
        contract_id: The contract identifier being analysed.
        changes: Output of diff_schemas.
        subscriptions: Registry subscriptions for blast radius analysis.

    Returns:
        A rich report dictionary including blast radius, per-consumer failure
        analysis, a concrete migration checklist, and a rollback plan.
    """
    compatibility_verdict = (
        "BREAKING"
        if any("BREAKING" in c.get("compatibility", "") for c in changes)
        else "COMPATIBLE"
    )

    impacted_consumers = get_impacted_consumers(changes, subscriptions, contract_id)

    # Blast radius: unique subscribers that are impacted.
    affected_subscriber_ids = sorted({i["subscriber_id"] for i in impacted_consumers})
    blast_radius = {
        "affected_subscribers": affected_subscriber_ids,
        "total_affected": len(affected_subscriber_ids),
        "per_consumer_failure_analysis": impacted_consumers,
    }

    # Concrete migration checklist.
    migration_checklist: list[str] = []
    step = 1
    notified: set[str] = set()
    for impact in impacted_consumers:
        sub_id = impact["subscriber_id"]
        contact = impact["contact"]
        field = impact["affected_field"]
        if sub_id not in notified:
            migration_checklist.append(
                f"{step}. Notify `{contact}` ({sub_id}) of breaking change to `{field}`."
            )
            step += 1
            notified.add(sub_id)
    migration_checklist.append(f"{step}. Coordinate a deployment window with all affected teams.")
    step += 1
    migration_checklist.append(f"{step}. Update producer code and apply the schema migration.")
    step += 1
    migration_checklist.append(f"{step}. Validate downstream consumers pass integration tests.")
    step += 1
    migration_checklist.append(f"{step}. Re-run the Data Contract Enforcer runner in ENFORCE mode.")

    if not migration_checklist[:1]:  # no breaking changes
        migration_checklist = ["No breaking changes detected. No migration actions required."]

    # Detailed rollback plan.
    rollback_plan = (
        "1. Revert the schema change commit in the producer repository. "
        "2. Re-deploy the producer service with the reverted schema. "
        "3. Notify all downstream teams listed in the blast radius to roll back "
        "any consumers they may have already updated. "
        "4. Run the schema analyzer again to confirm the compatibility verdict "
        "returns to COMPATIBLE."
    )

    return {
        "report_id": str(uuid.uuid4()),
        "contract_id": contract_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compatibility_verdict": compatibility_verdict,
        "changes": changes,
        "blast_radius": blast_radius,
        "migration_checklist": migration_checklist,
        "rollback_plan": rollback_plan,
    }


def save_report(report: dict) -> None:
    """Save a schema evolution report as a JSON file in validation_reports/.

    Args:
        report: The report dictionary produced by generate_report.
    """
    output_dir = Path("validation_reports")
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
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
        old_schema, new_schema, old_filename, new_filename = load_snapshots(contract_id)

        print(
            f"Successfully loaded two schemas for comparison: "
            f"{old_filename} and {new_filename}"
        )
        print(f"(--since filter value: '{args.since}')")

        subscriptions = load_registry()
        print(f"Loaded {len(subscriptions)} subscription(s) from the registry.")

        changes = diff_schemas(old_schema, new_schema)
        print(f"\nDetected {len(changes)} change(s).")

        report = generate_report(contract_id, changes, subscriptions)

        blast = report["blast_radius"]
        print(
            f"Blast radius: {blast['total_affected']} directly affected subscriber(s): "
            f"{blast['affected_subscribers']}"
        )
        print(f"Compatibility verdict: {report['compatibility_verdict']}")

        save_report(report)

    except (FileNotFoundError, ValueError) as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1)
