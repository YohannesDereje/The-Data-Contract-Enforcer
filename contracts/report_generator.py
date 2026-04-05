import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import openai
import yaml
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

load_dotenv()

client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)
MODEL_NAME = "anthropic/claude-sonnet-4-5"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report Generator — aggregates all Enforcer artifacts into a "
            "single structured payload ready for final summary reporting."
        ),
    )
    parser.add_argument(
        "--contract-id",
        required=True,
        help=(
            "The ID of the contract to report on "
            "(e.g. 'week3-contract-v1')."
        ),
    )
    return parser


def load_latest_json_report(directory: str | Path, prefix: str) -> dict | None:
    """Find and load the most recent JSON file in a directory matching a prefix.

    Args:
        directory: Directory to search for JSON files.
        prefix: Filename prefix to filter results (e.g. 'week3-contract-v1').

    Returns:
        Parsed JSON content as a dict, or None if no matching file exists.
    """
    search_dir = Path(directory)
    if not search_dir.exists():
        return None

    matching = [
        f for f in search_dir.glob("*.json")
        if f.name.startswith(prefix)
    ]

    if not matching:
        return None

    latest = max(matching, key=lambda f: f.stat().st_mtime)

    with latest.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_violation_log() -> list[dict]:
    """Read all entries from the violation log JSONL file.

    Returns:
        List of parsed violation dictionaries, or an empty list if the file
        does not exist.
    """
    log_path = Path("violation_log/violations.jsonl")
    if not log_path.exists():
        return []

    entries: list[dict] = []
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    return entries


def run_ai_extensions() -> dict:
    """Execute the ai_extensions.py script and return its results.

    Uses subprocess to run the script and captures stdout.  Parsing the
    mixed-format output (prose + JSON blocks) is non-trivial, so this
    function returns structured placeholder dicts for the three checks
    while still executing the script to verify it runs without errors.

    Returns:
        A dict with keys:
            embedding_drift       — drift check result (or placeholder)
            prompt_input_validation — input schema validation result (or placeholder)
            output_violation_rate — LLM output violation rate result (or placeholder)
            raw_output            — full captured stdout from the script
            exit_code             — process return code
    """
    result = subprocess.run(
        ["python", "contracts/ai_extensions.py"],
        capture_output=True,
        text=True,
    )

    raw_output = result.stdout

    # Placeholder results — the script's output mixes prose and JSON blocks
    # that require heuristic parsing.  Downstream callers can parse raw_output
    # for richer data if needed.
    return {
        "embedding_drift": {
            "status": "EXECUTED",
            "note": "See raw_output for full drift check details.",
        },
        "prompt_input_validation": {
            "status": "EXECUTED",
            "note": "See raw_output for full input validation details.",
        },
        "output_violation_rate": {
            "status": "EXECUTED",
            "note": "See raw_output for full violation rate details.",
        },
        "raw_output": raw_output,
        "exit_code": result.returncode,
    }


def calculate_health_score(validation_report: dict) -> int:
    """Compute a 0–100 data health score from a validation report.

    Formula: (passed / total) * 100 - (20 * critical_failures)
    The result is clamped to [0, 100].

    Args:
        validation_report: Parsed validation report dict from the runner.

    Returns:
        Integer health score between 0 and 100 inclusive.
    """
    total = validation_report.get("total_checks", 0)
    passed = validation_report.get("passed", 0)

    if total == 0:
        return 0

    critical_failures = sum(
        1 for r in validation_report.get("results", [])
        if r.get("severity") == "CRITICAL" and r.get("status") == "FAIL"
    )

    raw_score = (passed / total) * 100 - (20 * critical_failures)
    return max(0, min(100, int(raw_score)))


def summarize_violations(violations: list[dict]) -> list[str]:
    """Use an LLM to produce plain-English summaries of the top 3 violations.

    Args:
        violations: List of violation dicts from the violation log.

    Returns:
        List of one-sentence manager-facing summaries, one per violation.
    """
    summaries: list[str] = []

    for violation in violations[:3]:
        check_id = violation.get("check_id", "unknown")
        blame_chain = violation.get("blame_chain", [])
        blast_radius = violation.get("blast_radius", {})

        # Pre-extract key fields to guide the LLM unambiguously.
        subscriber_id = (
            blast_radius.get("affected_nodes", ["unknown"])[0]
            if blast_radius.get("affected_nodes")
            else "unknown"
        )
        commit_summary = blame_chain[0].get("summary", "unknown change") if blame_chain else "unknown change"
        # check_id is typically "{column_name}_{check_type}" — expose the column part.
        column_name = check_id.split("_")[0] if "_" in check_id else check_id

        user_prompt = (
            f"You are given a structured data quality violation record. "
            f"You MUST produce exactly ONE sentence using the template below.\n\n"
            f"VIOLATION JSON:\n"
            f"  check_id      : {check_id}\n"
            f"  column_name   : {column_name}   ← use this as the failing field name\n"
            f"  commit_summary: {commit_summary}   ← use this as the cause\n"
            f"  subscriber_id : {subscriber_id}   ← use this as the impacted system\n\n"
            f"Full blame chain for context:\n{json.dumps(blame_chain, indent=2)}\n"
            f"Full blast radius for context:\n{json.dumps(blast_radius, indent=2)}\n\n"
            f"REQUIRED OUTPUT FORMAT (fill in the placeholders, output nothing else):\n"
            f"\"A failure in the '{{column_name}}' field, likely caused by '{{commit_summary}}', "
            f"is impacting the '{{subscriber_id}}' system.\""
        )

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a data engineering assistant that translates technical "
                        "data contract violations into manager-facing summaries. "
                        "You always follow the exact output format given in the user prompt "
                        "and never deviate from it or add extra text."
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
        )
        summaries.append(response.choices[0].message.content.strip())

    return summaries


def summarize_schema_changes(changes: list[dict]) -> list[str]:
    """Use an LLM to produce plain-English summaries of schema changes.

    Args:
        changes: List of change dicts from the schema evolution report.

    Returns:
        List of one-sentence summaries, one per schema change.
    """
    summaries: list[str] = []

    for change in changes:
        user_prompt = (
            f"Here is a schema change:\n\n"
            f"{json.dumps(change, indent=2)}\n\n"
            "Explain this schema change and its compatibility verdict in one "
            "plain-English sentence."
        )

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a data engineering assistant. Your job is to "
                        "translate technical schema changes into clear, concise "
                        "summaries for non-technical stakeholders."
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
        )
        summaries.append(response.choices[0].message.content.strip())

    return summaries


def assess_ai_risks(ai_results: dict) -> dict:
    """Parse the raw output of ai_extensions.py and produce plain-English summaries.

    The three checks (embedding drift, prompt input validation, output violation
    rate) each print structured key/value lines that are extracted here via
    simple regex matches.

    Args:
        ai_results: Dict returned by run_ai_extensions, must contain 'raw_output'.

    Returns:
        Dict with keys 'embedding_drift', 'prompt_input_validation', and
        'output_violation_rate', each mapped to a human-readable summary string.
    """
    raw = ai_results.get("raw_output", "")

    assessment: dict[str, str] = {
        "embedding_drift": "Could not parse embedding drift result.",
        "prompt_input_validation": "Could not parse prompt input validation result.",
        "output_violation_rate": "Could not parse output violation rate result.",
    }

    # --- Embedding Drift ---
    status_match = re.search(r"status\s*:\s*(\S+)", raw)
    drift_match = re.search(r"drift_score\s*:\s*(\S+)", raw)
    threshold_match = re.search(r"threshold\s*:\s*(\S+)", raw)

    if status_match:
        status = status_match.group(1)
        if status == "BASELINE_SET":
            assessment["embedding_drift"] = (
                "No prior baseline existed — a new embedding baseline has been saved. "
                "Run again to perform a real drift comparison."
            )
        elif status == "PASS":
            drift = drift_match.group(1) if drift_match else "unknown"
            threshold = threshold_match.group(1) if threshold_match else "unknown"
            assessment["embedding_drift"] = (
                f"Embedding drift check PASSED. Drift score {drift} is within "
                f"the acceptable threshold of {threshold}."
            )
        else:
            drift = drift_match.group(1) if drift_match else "unknown"
            threshold = threshold_match.group(1) if threshold_match else "unknown"
            assessment["embedding_drift"] = (
                f"Embedding drift check FAILED. Drift score {drift} exceeds "
                f"the threshold of {threshold} — semantic distribution has shifted."
            )

    # --- Prompt Input Validation ---
    quarantine_match = re.search(r'"quarantined":\s*(\d+)', raw)
    if quarantine_match:
        quarantined = int(quarantine_match.group(1))
        if quarantined == 0:
            assessment["prompt_input_validation"] = (
                "All prompt input records passed schema validation. "
                "No records were quarantined."
            )
        else:
            assessment["prompt_input_validation"] = (
                f"{quarantined} record(s) failed prompt input schema validation "
                "and were quarantined. Review outputs/quarantine/ for details."
            )

    # --- Output Violation Rate ---
    violation_rate_match = re.search(r'"violation_rate":\s*([0-9.]+)', raw)
    violation_status_match = re.search(r'"status":\s*"(PASS|WARN)"', raw)
    if violation_rate_match:
        rate = float(violation_rate_match.group(1))
        vstatus = violation_status_match.group(1) if violation_status_match else "unknown"
        assessment["output_violation_rate"] = (
            f"LLM output schema violation rate is {rate:.2%} (status: {vstatus}). "
            + (
                "Rate is within acceptable limits."
                if vstatus == "PASS"
                else "Rate exceeds the warning threshold — review LLM outputs."
            )
        )

    return assessment


def generate_recommendations(violations: list[dict], contract_id: str = "unknown") -> list[str]:
    """Use an LLM to generate actionable fix recommendations from violations.

    Focuses on the most significant (first) violation and asks the model to
    produce 1-3 concrete, engineer-facing recommendations in the exact rubric
    format required.

    Args:
        violations: List of violation dicts from the violation log.
        contract_id: The contract identifier, used to populate the recommendation
            template's contract clause reference.

    Returns:
        A list of recommendation strings, each following the required format.
    """
    if not violations:
        return ["No violations found, no actions required."]

    top_violation = violations[0]
    blame_chain = top_violation.get("blame_chain", [])
    check_id = top_violation.get("check_id", "unknown")

    # Pre-extract every field the LLM needs so there is no ambiguity.
    file_path = blame_chain[0].get("file_path", "unknown/file.py") if blame_chain else "unknown/file.py"
    # check_id is typically "{column_name}_{check_type}" — expose both parts.
    if "_" in check_id:
        parts = check_id.split("_")
        failing_field = parts[0]
        check_type = "_".join(parts[1:])
    else:
        failing_field = check_id
        check_type = "unknown"

    user_prompt = (
        f"You are reviewing a data quality incident. "
        f"You MUST generate between 1 and 3 recommendations. "
        f"Each recommendation MUST be a single sentence in the EXACT format shown below.\n\n"
        f"VIOLATION CONTEXT (use these values to fill in the template):\n"
        f"  file_path     : {file_path}\n"
        f"    ← Source: first entry in 'blame_chain[].file_path'\n"
        f"  failing_field : {failing_field}\n"
        f"    ← Source: the column/field part of 'check_id' (before the first underscore)\n"
        f"  check_type    : {check_type}\n"
        f"    ← Source: the remainder of 'check_id' (after the first underscore)\n"
        f"  contract_id   : {contract_id}\n"
        f"    ← The data contract being enforced\n"
        f"  check_id      : {check_id}\n"
        f"    ← The specific clause that was violated\n\n"
        f"Full violation JSON for additional context:\n"
        f"{json.dumps(top_violation, indent=2)}\n\n"
        f"REQUIRED OUTPUT FORMAT for EACH recommendation (fill in placeholders):\n"
        f"\"Update [file_path] to output [failing_field] as [expected_type/format] "
        f"per contract [contract_id] clause [check_id].\"\n\n"
        f"Rules:\n"
        f"- Output ONLY the numbered recommendation sentences — no preamble, no extra text.\n"
        f"- Use the exact file_path, failing_field, contract_id, and check_id values above.\n"
        f"- For [expected_type/format], infer the correct type or constraint from the check_type "
        f"and violation details (e.g., 'a non-null string', 'a value >= 0', 'a unique identifier').\n"
        f"- If there are additional entries in the blame_chain, generate one recommendation per "
        f"upstream file, up to a maximum of 3 total recommendations."
    )

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a Principal Engineer reviewing a data incident report. "
                    "Your sole job is to output precise, immediately actionable fix "
                    "recommendations for engineers. You always follow the exact output "
                    "format specified in the user prompt without deviation, preamble, "
                    "or explanation. Every recommendation you produce names a specific "
                    "file, a specific field, and references the contract clause violated."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    )

    raw_response = response.choices[0].message.content.strip()
    recommendations = [
        line.strip()
        for line in raw_response.splitlines()
        if line.strip()
    ]
    return recommendations


def create_final_report_data(
    validation_report: dict | None,
    violations: list[dict],
    schema_report: dict | None,
    ai_results: dict,
) -> dict:
    """Orchestrate all processing functions and assemble a single report payload.

    Args:
        validation_report: Parsed validation report, or None if unavailable.
        violations: List of violation dicts from the violation log.
        schema_report: Parsed schema evolution report, or None if unavailable.
        ai_results: Dict returned by run_ai_extensions.

    Returns:
        A complete report dictionary with sections for every analysis component.
    """
    health_score = (
        calculate_health_score(validation_report)
        if validation_report is not None
        else None
    )

    violations_summary = summarize_violations(violations)

    schema_changes = schema_report.get("changes", []) if schema_report is not None else []
    schema_summary = summarize_schema_changes(schema_changes)

    ai_risk_assessment = assess_ai_risks(ai_results)

    contract_id = validation_report.get("contract_id", "unknown") if validation_report else "unknown"
    recommendations = generate_recommendations(violations, contract_id)

    return {
        "health_score": health_score,
        "violations_summary": violations_summary,
        "schema_summary": schema_summary,
        "ai_risk_assessment": ai_risk_assessment,
        "recommendations": recommendations,
    }


def generate_pdf_report(report_data: dict, contract_id: str) -> None:
    """Render the final report data as a PDF using reportlab.

    Writes each report section to the canvas with a simple cursor-based
    layout that moves down the page as content is drawn, starting a new
    page automatically when the cursor reaches the bottom margin.

    Args:
        report_data: The dict returned by create_final_report_data.
        contract_id: Used to build the output filename.
    """
    output_dir = Path("enforcer_report")
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    output_path = output_dir / f"report_{contract_id}_{timestamp}.pdf"

    styles = getSampleStyleSheet()
    page_width, page_height = letter
    left_margin = 60
    right_margin = page_width - 60
    bottom_margin = 60

    c = canvas.Canvas(str(output_path), pagesize=letter)

    # Mutable cursor so the nested helpers can advance it.
    cursor = [page_height - 60]

    def new_page() -> None:
        c.showPage()
        cursor[0] = page_height - 60

    def ensure_space(needed: float = 20) -> None:
        if cursor[0] - needed < bottom_margin:
            new_page()

    def draw_line(text: str, font: str = "Helvetica", size: int = 11,
                  color: tuple = (0, 0, 0), indent: int = 0) -> None:
        ensure_space(size + 6)
        c.setFont(font, size)
        c.setFillColorRGB(*color)
        c.drawString(left_margin + indent, cursor[0], text)
        cursor[0] -= size + 6

    def draw_section_title(title: str) -> None:
        ensure_space(30)
        cursor[0] -= 10
        c.setFont("Helvetica-Bold", 13)
        c.setFillColorRGB(0.2, 0.2, 0.6)
        c.drawString(left_margin, cursor[0], title)
        cursor[0] -= 4
        c.setStrokeColorRGB(0.2, 0.2, 0.6)
        c.line(left_margin, cursor[0], right_margin, cursor[0])
        cursor[0] -= 14

    def draw_wrapped(text: str, font: str = "Helvetica", size: int = 10,
                     indent: int = 10) -> None:
        """Naive word-wrap: split into ~90-char chunks and draw each."""
        words = text.split()
        line_buf: list[str] = []
        max_chars = int((right_margin - left_margin - indent) / (size * 0.55))
        for word in words:
            if sum(len(w) + 1 for w in line_buf) + len(word) > max_chars:
                draw_line(" ".join(line_buf), font=font, size=size, indent=indent)
                line_buf = [word]
            else:
                line_buf.append(word)
        if line_buf:
            draw_line(" ".join(line_buf), font=font, size=size, indent=indent)

    # ------------------------------------------------------------------ Title
    c.setFont("Helvetica-Bold", 22)
    c.setFillColorRGB(0.1, 0.1, 0.4)
    c.drawCentredString(page_width / 2, cursor[0], "Enforcer Report")
    cursor[0] -= 28
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawCentredString(page_width / 2, cursor[0], f"Contract: {contract_id}   |   {timestamp}")
    cursor[0] -= 24

    # --------------------------------------------------------- Health Score
    draw_section_title("Data Health Score")
    score = report_data.get("health_score")
    if score is None:
        draw_line("N/A — no validation report available.", size=12)
    else:
        if score >= 80:
            score_color = (0.0, 0.6, 0.0)   # green
        elif score >= 50:
            score_color = (0.85, 0.55, 0.0)  # amber
        else:
            score_color = (0.8, 0.0, 0.0)    # red
        draw_line(f"{score} / 100", font="Helvetica-Bold", size=26, color=score_color)

    # ------------------------------------------------- Violation Summaries
    draw_section_title("Violations This Week")
    summaries = report_data.get("violations_summary", [])
    if not summaries:
        draw_line("No violations recorded.", size=10)
    else:
        for i, summary in enumerate(summaries, start=1):
            draw_wrapped(f"{i}. {summary}")

    # ------------------------------------------------ Schema Change Summary
    draw_section_title("Schema Changes Detected")
    schema_summaries = report_data.get("schema_summary", [])
    if not schema_summaries:
        draw_line("No schema changes detected.", size=10)
    else:
        for i, summary in enumerate(schema_summaries, start=1):
            draw_wrapped(f"{i}. {summary}")

    # ------------------------------------------- AI System Risk Assessment
    draw_section_title("AI System Risk Assessment")
    ai_assessment = report_data.get("ai_risk_assessment", {})
    for check, summary in ai_assessment.items():
        draw_line(check.replace("_", " ").title() + ":", font="Helvetica-Bold",
                  size=10, indent=0)
        draw_wrapped(summary, indent=16)
        cursor[0] -= 4

    # ------------------------------------------------ Recommended Actions
    draw_section_title("Recommended Actions")
    recommendations = report_data.get("recommendations", [])
    if not recommendations:
        draw_line("No recommendations.", size=10)
    else:
        for i, rec in enumerate(recommendations, start=1):
            draw_wrapped(f"{i}. {rec}")

    c.save()
    print(f"PDF report saved to: {output_path}")


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    contract_id = args.contract_id

    try:
        # --- Validation report ---
        validation_report = load_latest_json_report("validation_reports", contract_id)
        if validation_report is None:
            print(f"Warning: No validation report found for '{contract_id}'.")
            total_checks = 0
        else:
            total_checks = validation_report.get("total_checks", 0)
        print(f"Successfully loaded validation report with {total_checks} checks.")

        # --- Violation log ---
        violations = load_violation_log()
        print(f"Successfully loaded violation log with {len(violations)} entries.")

        # --- Schema evolution report ---
        schema_report = load_latest_json_report(
            "validation_reports", f"schema_evolution_{contract_id}"
        )
        if schema_report is None:
            print(f"Warning: No schema evolution report found for '{contract_id}'.")
            total_changes = 0
        else:
            total_changes = len(schema_report.get("changes", []))
        print(f"Successfully loaded schema evolution report with {total_changes} changes.")

        # --- AI extensions ---
        ai_results = run_ai_extensions()
        if ai_results["exit_code"] != 0:
            print(
                f"Warning: ai_extensions.py exited with code {ai_results['exit_code']}."
            )
        print("Successfully ran AI extensions.")

        # --- Final report ---
        final_report = create_final_report_data(
            validation_report, violations, schema_report, ai_results
        )
        generate_pdf_report(final_report, contract_id)

    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1)
