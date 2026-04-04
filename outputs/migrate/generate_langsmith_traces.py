import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


TARGET_FILE = Path("outputs/traces/runs.jsonl")

_DOC_IDS = [
    "doc-fta-2022-001",
    "doc-apex-commercial-001",
    "doc-loan-app-7834",
    "doc-compliance-report-q1",
    "doc-credit-analysis-9921",
]
_LLM_MODELS = [
    "claude-3-5-sonnet-20240620",
    "claude-3-opus-20240229",
    "gpt-4o-20240513",
]
_TOOL_NAMES = [
    "read_file",
    "search_documents",
    "validate_schema",
    "extract_entities",
    "write_output",
]
_CHAIN_NAMES = [
    "Document Processing Chain",
    "Contract Extraction Pipeline",
    "Compliance Review Chain",
    "Credit Analysis Chain",
    "Entity Resolution Pipeline",
]

# Approximate cost per token in USD (input rate, output rate).
_COST_PER_TOKEN = {
    "claude-3-5-sonnet-20240620": (3e-6,  15e-6),
    "claude-3-opus-20240229":     (15e-6, 75e-6),
    "gpt-4o-20240513":            (5e-6,  15e-6),
}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _generate_llm_run(
    parent_run_id: str,
    session_id: str,
    session_start: datetime,
) -> tuple[dict, int, int, float]:
    """Build one child LLM trace record.

    Returns:
        (record, prompt_tokens, completion_tokens, total_cost)
    """
    run_id = str(uuid.uuid4())
    model = random.choice(_LLM_MODELS)
    prompt_tokens = random.randint(256, 4096)
    completion_tokens = random.randint(64, 1024)
    total_tokens = prompt_tokens + completion_tokens

    cost_in, cost_out = _COST_PER_TOKEN[model]
    total_cost = round(prompt_tokens * cost_in + completion_tokens * cost_out, 6)

    llm_start = session_start + timedelta(milliseconds=random.randint(50, 300))
    llm_end = llm_start + timedelta(milliseconds=random.randint(800, 4000))

    record = {
        "id": run_id,
        "name": model,
        "run_type": "llm",
        "parent_run_id": parent_run_id,
        "session_id": session_id,
        "start_time": _iso(llm_start),
        "end_time": _iso(llm_end),
        "inputs": {
            "messages": [
                {"role": "system", "content": "You are a document analysis assistant."},
                {"role": "user", "content": "Extract key facts from the provided document."},
            ]
        },
        "outputs": {
            "generations": [
                {"text": "Extracted entities and facts from the document successfully."}
            ]
        },
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "error": None,
        "status": "success",
    }
    return record, prompt_tokens, completion_tokens, total_cost


def _generate_tool_run(
    parent_run_id: str,
    session_id: str,
    session_start: datetime,
) -> dict:
    """Build one child tool trace record."""
    tool_start = session_start + timedelta(milliseconds=random.randint(10, 100))
    tool_end = tool_start + timedelta(milliseconds=random.randint(20, 250))
    tool_name = random.choice(_TOOL_NAMES)

    return {
        "id": str(uuid.uuid4()),
        "name": tool_name,
        "run_type": "tool",
        "parent_run_id": parent_run_id,
        "session_id": session_id,
        "start_time": _iso(tool_start),
        "end_time": _iso(tool_end),
        "inputs": {"path": f"documents/{random.choice(_DOC_IDS)}.pdf"},
        "outputs": {"status": "ok", "bytes_read": random.randint(4096, 524288)},
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "total_cost": 0.0,
        "error": None,
        "status": "success",
    }


def generate_data(num_sessions: int = 25) -> None:
    """Generate synthetic LangSmith trace records and write to TARGET_FILE.

    Each session produces 3 records: one parent chain run and two child runs
    (one LLM call and one tool call), linked via parent_run_id.

    Args:
        num_sessions: Number of agent sessions to simulate.
    """
    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"Generating {num_sessions} sessions ({num_sessions * 3} total trace records)...")

    all_records: list[dict] = []

    # Spread sessions across the past 14 days.
    now = datetime.now(timezone.utc)

    for _ in range(num_sessions):
        session_id = str(uuid.uuid4())
        chain_id = str(uuid.uuid4())
        doc_id = random.choice(_DOC_IDS)

        session_offset_hours = random.randint(0, 14 * 24)
        session_start = now - timedelta(hours=session_offset_hours)

        # -- Child LLM run (generated first so we can sum tokens for the parent).
        llm_record, prompt_tokens, completion_tokens, total_cost = _generate_llm_run(
            chain_id, session_id, session_start
        )

        # -- Child tool run.
        tool_record = _generate_tool_run(chain_id, session_id, session_start)

        # -- Parent chain run (end time is after both children finish).
        chain_start = session_start
        chain_end = chain_start + timedelta(milliseconds=random.randint(3000, 8000))

        chain_record = {
            "id": chain_id,
            "name": random.choice(_CHAIN_NAMES),
            "run_type": "chain",
            "parent_run_id": None,
            "session_id": session_id,
            "start_time": _iso(chain_start),
            "end_time": _iso(chain_end),
            "inputs": {"doc_id": doc_id},
            "outputs": None,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "total_cost": total_cost,
            "error": None,
            "status": "success",
        }

        # Parent first, then children — preserves logical ordering in the file.
        all_records.extend([chain_record, llm_record, tool_record])

    with TARGET_FILE.open("w", encoding="utf-8") as dst:
        for record in all_records:
            dst.write(json.dumps(record) + "\n")

    print(f"Done. {len(all_records)} trace records written to {TARGET_FILE}")


if __name__ == "__main__":
    generate_data(num_sessions=25)
