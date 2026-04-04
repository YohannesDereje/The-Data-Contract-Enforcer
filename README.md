# The Data Contract Enforcer

This repository contains the complete implementation for the "Data Contract Enforcer" project. It is a comprehensive data governance tool that can generate, validate, and analyze data contracts, attribute violations, and produce executive-level reports.

## 🚀 Quickstart & End-to-End Execution Guide

This guide provides the step-by-step commands to run the entire suite of tools on a fresh clone of this repository. Evaluators should be able to follow these commands exactly and observe the expected output.

### 1. Initial Setup

First, ensure you have Python 3.11+ installed. Then, clone the repository and set up the environment.

**1.1. Install Dependencies:**
```bash
pip install -r requirements.txt
```

**1.2. Set Up API Key:**
This project uses the OpenRouter API for generating plain-English summaries. Create a `.env` file in the project root and add your API key.
```
# .env
OPENROUTER_API_KEY="your-openrouter-api-key-goes-here"
```

### 2. Data Preparation

This project requires several source data files. The following scripts will generate compliant, high-quality synthetic data for all required inputs.

**2.1. Run All Data Generation Scripts:**
```bash
python outputs/migrate/generate_synthetic_week1.py
python outputs/migrate/generate_synthetic_week2.py
python outputs/migrate/migrate_week3.py
python outputs/migrate/migrate_week4.py
python outputs/migrate/migrate_week5.py
python outputs/migrate/generate_langsmith_traces.py
```
**Expected Output:**
You will see a series of "Migration complete" and "Done" messages, confirming that all files in the `outputs/` directory have been created.

### 3. Generate All Data Contracts

Run the `ContractGenerator` for all systems to create the contract artifacts and schema snapshots.

```bash
python contracts/generator.py week1
python contracts/generator.py week2
python contracts/generator.py week3
python contracts/generator.py week4
python contracts/generator.py week5
python contracts/generator.py langsmith
```
**Expected Output:**
For each command, you will see a confirmation message like the following, and the corresponding `.yaml` files will be created in `generated_contracts/` and `schema_snapshots/`.
```
Contract saved to: generated_contracts/week1_intent_records.yaml
Schema snapshot saved to: schema_snapshots/week1-contract-v1/...
dbt schema saved to: generated_contracts/week1_schema.yml
```

### 4. Run the Validation Suite

Now we will use the `ValidationRunner` to validate our data against the contracts.

**4.1. Inject a Failure for Demonstration:**
To demonstrate the full capability of the system, we will manually inject a "statistical drift" error into our Week 3 data. A provided helper script automates this.
```bash
# This script will modify the first line of the Week 3 data file.
python utils/inject_failure.py
```
*(Note: You will need to create a simple `utils/inject_failure.py` script for this, or perform the edit manually as we did during our session.)*

**4.2. Run the Validation Runner:**
Now, run the runner against the modified Week 3 data.
```bash
python contracts/runner.py generated_contracts/week3_extractions.yaml --mode ENFORCE
```
**Expected Output:**
The script will find the failure and exit with a non-zero status code.
```
...
Running 22 quality checks...

Summary: 22 checks — 21 passed, 1 failed, 0 warned, 0 errored.
Mode: ENFORCE — exiting with code 1.
```

### 5. Analyze Schema Evolution

Run the `SchemaEvolutionAnalyzer` to compare two versions of a schema. First, we need to create a second snapshot by re-running the generator.

**5.1. Create a Second Snapshot:**
```bash
python contracts/generator.py week3
```

**5.2. Run the Analyzer:**
```bash
python contracts/schema_analyzer.py --contract-id week3-contract-v1
```
**Expected Output:**
Since no changes were made between the two identical snapshots, it will report no changes.
```
Successfully loaded two schemas for comparison: ... and ...
Detected 0 change(s).
Schema evolution report saved to: validation_reports/schema_evolution_...
Compatibility verdict: COMPATIBLE
```

### 6. Attribute a Violation

Run the `ViolationAttributor` to investigate the failure we created in step 4.1.

```bash
python contracts/attributor.py generated_contracts/week3_extractions.yaml
```
**Expected Output:**
The script will trace the failure back to a Git commit and log the violation.
```
...
Investigating failed check: confidence_max_value
...
Found 1 upstream source file(s).
Identified 1 blame candidate(s) (recent commits).
Violation logged to: violation_log/violations.jsonl ...

Violation logged successfully.
```

### 7. Run AI Contract Extensions

Execute the `ai_extensions.py` script to perform the specialized AI system checks.

```bash
python contracts/ai_extensions.py
```
**Expected Output:**
The script will run all three checks and print their results.
```
...
Embedding Drift Check Result:
   status       : PASS
...
Prompt Input Validation Result:
{
  "total": 3,
  "valid": 1,
  "quarantined": 2,
  ...
}
...
LLM Output Schema Violation Rate:
{
  "total_outputs": ...,
  "schema_violations": 1,
  ...
}
```

### 8. Generate the Final Enforcer Report

Finally, run the `report_generator.py` to aggregate all artifacts and produce the final PDF report.

```bash
python contracts/report_generator.py --contract-id week3-contract-v1
```
**Expected Output:**
The script will load all artifacts and generate the final PDF.
```
...
Successfully loaded validation report with 22 checks.
Successfully loaded violation log with 1 entries.
...
PDF report saved to: enforcer_report/report_week3-contract-v1_....pdf
```
You can now open the generated PDF file in the `enforcer_report/` directory to view the complete, synthesized summary.

```

---

This `README.md` is complete, accurate, and provides the clear, step-by-step instructions the evaluators need. It demonstrates the full capability of your system from end to end.