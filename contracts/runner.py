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
    parser.add_argument(
        "--mode",
        default="AUDIT",
        choices=["AUDIT", "WARN", "ENFORCE"],
        help=(
            "Enforcement mode. "
            "AUDIT/WARN: always exit 0 regardless of results. "
            "ENFORCE: exit 1 if any CRITICAL or HIGH severity check fails."
        ),
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
    """Load and flatten the data file referenced in the contract's servers section.

    The flattening strategy mirrors load_and_flatten_data in generator.py exactly,
    so that column names in the DataFrame match the column names used when the
    quality checks were generated.

    Args:
        contract: A parsed Bitol contract dictionary.

    Returns:
        A tuple of (DataFrame, data_path). The DataFrame is flattened in the same
        way as the generator. data_path is returned for snapshot_id computation.

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

    # Derive the system name from the contract id (e.g. "week3-contract-v1" -> "week3").
    system_name = contract.get("id", "").split("-contract-")[0]

    if system_name == "week1":
        df = pd.json_normalize(
            records,
            record_path=["code_refs"],
            meta=["intent_id", "description", "created_at"],
            meta_prefix="meta_",
            sep=".",
        )
    elif system_name == "week3":
        df = pd.json_normalize(
            records,
            record_path=["extracted_facts"],
            meta=[
                "doc_id", "source_path", "extraction_model", "extracted_at",
                ["token_count", "input"], ["token_count", "output"],
            ],
            meta_prefix="meta_",
            sep=".",
        )
    else:
        df = pd.DataFrame(
            [pd.json_normalize(rec, sep=".").to_dict("records")[0] for rec in records]
        )

    print(f"  DataFrame shape after flattening: {df.shape}")
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
    "missing_count": "CRITICAL",
    "duplicate_count": "CRITICAL",
    "min": "HIGH",
    "max": "HIGH",
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


def check_statistical_drift(
    df: pd.DataFrame,
    column: str,
    baseline_stats: dict,
) -> float:
    """Calculate how many standard deviations the current column mean has drifted.

    Args:
        df: The loaded data DataFrame.
        column: The column name to evaluate.
        baseline_stats: Dict with keys 'baseline_mean' and 'baseline_stddev'.

    Returns:
        Drift expressed in number of standard deviations from the baseline mean.
        Returns 0.0 if baseline_stddev is zero or missing.
    """
    current_mean = float(pd.to_numeric(df[column], errors="coerce").mean())
    baseline_mean = float(baseline_stats.get("baseline_mean", current_mean))
    baseline_stddev = float(baseline_stats.get("baseline_stddev", 0.0))

    if baseline_stddev == 0.0:
        return 0.0

    return abs(current_mean - baseline_mean) / baseline_stddev


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

    # Load baseline stats for statistical_drift checks (gracefully optional).
    baselines: dict = {}
    baselines_path = Path("schema_snapshots/baselines.json")
    if baselines_path.exists():
        with baselines_path.open("r", encoding="utf-8") as bf:
            baselines = json.load(bf)

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
            elif check_type == "statistical_drift":
                column_baselines = baselines.get(column, {})
                drift_stdevs = check_statistical_drift(df, column, column_baselines)
                if drift_stdevs > 3:
                    drift_status = "FAIL"
                elif drift_stdevs > 2:
                    drift_status = "WARN"
                else:
                    drift_status = "PASS"
                results.append({
                    "check_id": check_id,
                    "column_name": column,
                    "check_type": check_type,
                    "status": drift_status,
                    "actual_value": round(drift_stdevs, 4),
                    "expected": "> 2 stddevs = WARN, > 3 stddevs = FAIL",
                    "severity": severity,
                    "records_failing": 0,
                    "sample_failing": [],
                    "message": (
                        f"Statistical drift: {drift_stdevs:.4f} stddevs from baseline. "
                        f"Status: {drift_status}."
                    ),
                })
                continue
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
        # Ensure a dummy baselines file exists for statistical drift testing.
        baselines_path = Path("schema_snapshots/baselines.json")
        if not baselines_path.exists():
            baselines_path.parent.mkdir(parents=True, exist_ok=True)
            dummy_baselines = {
                "confidence_score": {
                    "baseline_mean": 0.75,
                    "baseline_stddev": 0.1,
                }
            }
            with baselines_path.open("w", encoding="utf-8") as bf:
                bf.write(json.dumps(dummy_baselines, indent=2))
            print(f"Created dummy baselines file at: {baselines_path}")

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

        exit_code = 0
        if args.mode == "ENFORCE":
            for result in report["results"]:
                if result["status"] == "FAIL" and result["severity"] in ("CRITICAL", "HIGH"):
                    exit_code = 1
                    break

        print(f"Mode: {args.mode} — exiting with code {exit_code}.")
        raise SystemExit(exit_code)
    except (FileNotFoundError, KeyError) as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1)
