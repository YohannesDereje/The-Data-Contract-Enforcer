import json
import uuid
from datetime import datetime, timezone
from pathlib import Path



SOURCE_FILE = Path("lineage_graph.json")
TARGET_FILE = Path("outputs/week4/lineage_snapshots.jsonl")


def transform_node(node: dict) -> dict:
    path = node.get("path", "")
    return {
        "node_id": f"file::{path}",
        "type": (node.get("kind") or "").upper(),
        "label": Path(path).name,
        "metadata": {
            "path": path,
            "language": node.get("language"),
            "purpose": "LLM-inferred purpose",
            "last_modified": "2025-01-14T09:00:00Z",
        },
    }


def transform_edge(edge: dict) -> dict:
    operation_type = edge.get("operation_type") or ""
    relationship = "PRODUCES" if operation_type == "TRANSFORM" else operation_type.upper()
    return {
        "source": f"file::{edge.get('source', '')}",
        "target": f"file::{edge.get('target', '')}",
        "relationship": relationship,
        "confidence": 0.95,
    }


def migrate():
    print(f"Starting migration: {SOURCE_FILE} -> {TARGET_FILE}")

    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)

    print(f"Reading source file: {SOURCE_FILE}")
    with SOURCE_FILE.open("r", encoding="utf-8") as src:
        graph = json.load(src)

    source_nodes = graph.get("nodes", [])
    source_edges = graph.get("edges", [])
    print(f"Found {len(source_nodes)} nodes and {len(source_edges)} edges")

    canonical_nodes = [transform_node(n) for n in source_nodes]
    canonical_edges = [transform_edge(e) for e in source_edges]

    snapshot = {
        "snapshot_id": str(uuid.uuid4()),
        "codebase_root": "/app/src",
        "git_commit": "f" * 40,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "nodes": canonical_nodes,
        "edges": canonical_edges,
    }

    with TARGET_FILE.open("w", encoding="utf-8") as dst:
        dst.write(json.dumps(snapshot) + "\n")

    print(
        f"Migration complete. Snapshot written to {TARGET_FILE} "
        f"({len(canonical_nodes)} nodes, {len(canonical_edges)} edges)"
    )


if __name__ == "__main__":
    migrate()
