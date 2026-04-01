import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Data Contract Validation Runner — validates a data file "
                    "against a Bitol YAML contract.",
    )
    parser.add_argument(
        "contract_path",
        help="File path to the YAML contract to be executed "
             "(e.g. 'generated_contracts/week3_extractions.yaml').",
    )
    return parser


def load_contract(contract_path: Path) -> dict:
    """Load and parse a Bitol YAML contract file.

    Args:
        contract_path: Path to the YAML contract file.

    Returns:
        The parsed contract as a Python dictionary.

    Raises:
        FileNotFoundError: If the contract file does not exist.
    """
    if not contract_path.exists():
        raise FileNotFoundError(f"Contract file not found: {contract_path}")

    with contract_path.open("r", encoding="utf-8") as fh:
        contract = yaml.safe_load(fh)

    print(f"Loaded contract: '{contract.get('id', 'unknown')}' from {contract_path}")
    return contract


def load_data_from_contract(contract: dict) -> tuple[pd.DataFrame, Path]:
    """Load the data file referenced in the contract's servers section.

    Args:
        contract: A parsed Bitol contract dictionary.

    Returns:
        A tuple of (DataFrame, data_path). The DataFrame has one row per JSONL
        record (unflattened). data_path is returned for snapshot_id computation.

    Raises:
        KeyError: If the expected server path key is missing from the contract.
        FileNotFoundError: If the data file does not exist at the specified path.
    """
    try:
        data_path = Path(contract["servers"]["local"]["path"])
    except KeyError as exc:
        raise KeyError(
            f"Contract is missing the expected key: {exc}. "
            "Expected structure: contract['servers']['local']['path']."
        ) from exc

    if not data_path.exists():
        raise FileNotFoundError(
            f"Data file not found: {data_path}\n"
            "Run the corresponding migrate script first."
        )

    print(f"Loading data from: {data_path}")

    records: list[dict] = []
    with data_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    df = pd.DataFrame(records)
    return df, data_path


def compute_snapshot_id(data_path: Path) -> str:
    """Compute a SHA-256 fingerprint of the input JSONL file.

    Args:
        data_path: Path to the JSONL data file.

    Returns:
        Hex-encoded SHA-256 digest of the file contents.
    """
    sha256 = hashlib.sha256()
    with data_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


_OPERATORS = {
    "=":  lambda actual, expected: actual == expected,
    ">=": lambda actual, expected: actual >= expected,
    "<=": lambda actual, expected: actual <= expected,
}

_CHECK_SEVERITY: dict[str, str] = {
    "missing_count": "error",
    "duplicate_count": "error",
    "min": "error",
    "max": "error",
}


def _failing_records(
    df: pd.DataFrame,
    column: str,
    check_type: str,
    expected: object,
    status: str,
) -> tuple[int, list]:
    """Return (records_failing, sample_failing) for a check result.

    For PASS results, always returns (0, []).
    For FAIL results, identifies the specific rows that violate the constraint.
    """
    if status == "PASS":
        return 0, []

    series = df[column]

    if check_type == "missing_count":
        # Failing records are the null rows; their values are uninformative.
        return int(series.isna().sum()), []

    if check_type == "duplicate_count":
        mask = series.duplicated(keep=False)
        sample = series[mask].dropna().head(5).tolist()
        return int(mask.sum()), sample

    if check_type == "min":
        # Rows whose value is below the expected floor.
        mask = pd.to_numeric(series, errors="coerce") < expected
        sample = series[mask].dropna().head(5).tolist()
        return int(mask.sum()), sample

    if check_type == "max":
        # Rows whose value is above the expected ceiling.
        mask = pd.to_numeric(series, errors="coerce") > expected
        sample = series[mask].dropna().head(5).tolist()
        return int(mask.sum()), sample

    return 0, []


def run_checks(df: pd.DataFrame, checks: list[dict]) -> list[dict]:
    """Execute each quality check against the DataFrame and return results.

    Each result conforms to the canonical result schema:
    check_id, column_name, check_type, status, actual_value, expected,
    severity, records_failing, sample_failing, message.

    Args:
        df: The loaded data DataFrame.
        checks: The list of check dicts from contract['quality']['checks'].

    Returns:
        A list of result dicts conforming to the required output schema.
    """
    results: list[dict] = []

    for check in checks:
        check_type = check.get("type")
        column = check.get("column", "")
        must_be = check.get("must_be", "=")
        expected = check.get("value")
        check_id = check.get("name", f"{column}_{check_type}")
        severity = _CHECK_SEVERITY.get(check_type, "error")

        # Column absent from the loaded data — skip with an informative message.
        if column not in df.columns:
            results.append({
                "check_id": check_id,
                "column_name": column,
                "check_type": check_type,
                "status": "SKIP",
                "actual_value": None,
                "expected": f"{must_be} {expected}",
                "severity": severity,
                "records_failing": 0,
                "sample_failing": [],
                "message": f"Column '{column}' not found in the data file.",
            })
            continue

        # Execute the appropriate check and capture the actual metric.
        try:
            if check_type == "missing_count":
                actual = int(df[column].isna().sum())
            elif check_type == "duplicate_count":
                actual = int(df[column].duplicated(keep=False).sum())
            elif check_type == "min":
                actual = float(pd.to_numeric(df[column], errors="coerce").min())
            elif check_type == "max":
                actual = float(pd.to_numeric(df[column], errors="coerce").max())
            else:
                results.append({
                    "check_id": check_id,
                    "column_name": column,
                    "check_type": check_type,
                    "status": "SKIP",
                    "actual_value": None,
                    "expected": f"{must_be} {expected}",
                    "severity": severity,
                    "records_failing": 0,
                    "sample_failing": [],
                    "message": f"Unsupported check type '{check_type}'.",
                })
                continue
        except Exception as exc:  # noqa: BLE001
            results.append({
                "check_id": check_id,
                "column_name": column,
                "check_type": check_type,
                "status": "ERROR",
                "actual_value": None,
                "expected": f"{must_be} {expected}",
                "severity": severity,
                "records_failing": 0,
                "sample_failing": [],
                "message": f"Check raised an unexpected error: {exc}",
            })
            continue

        comparator = _OPERATORS.get(must_be)
        if comparator is None:
            status = "SKIP"
        elif comparator(actual, expected):
            status = "PASS"
        elif severity == "warn":
            status = "WARN"
        else:
            status = "FAIL"

        records_failing, sample_failing = _failing_records(
            df, column, check_type, expected, status
        )

        if status == "PASS":
            message = f"Check passed. Actual value: {actual}."
        else:
            message = (
                f"Check failed. Expected {must_be} {expected}, "
                f"got {actual}. {records_failing} record(s) violate this constraint."
            )

        results.append({
            "check_id": check_id,
            "column_name": column,
            "check_type": check_type,
            "status": status,
            "actual_value": actual,
            "expected": f"{must_be} {expected}",
            "severity": severity,
            "records_failing": records_failing,
            "sample_failing": sample_failing,
            "message": message,
        })

    return results


def generate_report(
    contract: dict,
    results: list[dict],
    snapshot_id: str,
) -> dict:
    """Assemble validation results into the canonical flat report schema.

    Args:
        contract: The parsed Bitol contract dictionary.
        results: The list of result dicts from run_checks.
        snapshot_id: SHA-256 fingerprint of the input data file.

    Returns:
        A flat report dictionary matching the required output schema.
    """
    return {
        "report_id": str(uuid.uuid4()),
        "contract_id": contract["id"],
        "snapshot_id": snapshot_id,
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "total_checks": len(results),
        "passed":  sum(1 for r in results if r["status"] == "PASS"),
        "failed":  sum(1 for r in results if r["status"] == "FAIL"),
        "warned":  sum(1 for r in results if r["status"] == "WARN"),
        "errored": sum(1 for r in results if r["status"] == "ERROR"),
        "results": results,
    }


def save_report_to_json(report: dict) -> None:
    """Save a validation report dictionary to a timestamped JSON file.

    Args:
        report: The report dictionary produced by generate_report.
    """
    output_dir = Path("validation_reports")
    output_dir.mkdir(exist_ok=True)

    contract_id = report["contract_id"]
    timestamp = (
        report["run_timestamp"]
        .replace(":", "-")
        .replace("+", "Z")
        .split(".")[0]          # trim microseconds for a cleaner filename
    )
    output_path = output_dir / f"{contract_id}-{timestamp}.json"

    with output_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(report, indent=2))

    print(f"Report saved to: {output_path}")


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        contract = load_contract(Path(args.contract_path))
        df, data_path = load_data_from_contract(contract)
        print(f"Loaded data. Shape: {df.shape}")

        snapshot_id = compute_snapshot_id(data_path)
        print(f"Snapshot ID (sha256): {snapshot_id[:16]}...")

        quality_checks = contract["quality"]["checks"]
        print(f"Running {len(quality_checks)} quality checks...\n")

        results = run_checks(df, quality_checks)
        report = generate_report(contract, results, snapshot_id)
        save_report_to_json(report)

        print(
            f"\nSummary: {report['total_checks']} checks — "
            f"{report['passed']} passed, {report['failed']} failed, "
            f"{report['warned']} warned, {report['errored']} errored."
        )
    except (FileNotFoundError, KeyError) as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1)
