import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


SOURCE_FILE = Path("lineage_graph.json")
TARGET_FILE = Path("outputs/week4/lineage_snapshots.jsonl")

_NODE_TYPE_MAP: dict[str, str] = {
    "module": "FILE",
    "script": "FILE",
}

_RELATIONSHIP_MAP: dict[str, str] = {
    "TRANSFORM": "PRODUCES",
    "IMPORT": "IMPORTS",
}


def transform_graph_to_snapshot(graph: dict) -> dict:
    """Transform a raw lineage_graph.json dictionary into a canonical lineage_snapshot.

    Args:
        graph: The full parsed lineage_graph.json object.

    Returns:
        A single canonical lineage_snapshot dictionary.
    """
    # ------------------------------------------------------------------ nodes
    canonical_nodes: list[dict] = []
    for source_node in graph.get("nodes", []):
        path = source_node.get("path", "")
        canonical_nodes.append({
            "node_id": f"file::{path}",
            "type": _NODE_TYPE_MAP.get(source_node.get("kind", ""), "EXTERNAL"),
            "label": Path(path).name,
            "metadata": {
                "path": path,
                "language": source_node.get("language"),
                "purpose": "LLM-inferred purpose",
                "last_modified": "2025-01-14T09:00:00Z",
            },
        })

    # ------------------------------------------------------------------ edges
    canonical_edges: list[dict] = []
    for source_edge in graph.get("edges", []):
        operation_type = source_edge.get("operation_type", "")
        canonical_edges.append({
            "source": f"file::{source_edge.get('source', '')}",
            "target": f"file::{source_edge.get('target', '')}",
            "relationship": _RELATIONSHIP_MAP.get(operation_type, "CONSUMES"),
            "confidence": 0.95,
        })

    # -------------------------------------------------------- assemble snapshot
    git_commit = hashlib.sha1(b"lineage-graph-snapshot-v1").hexdigest()[:40]

    return {
        "snapshot_id": str(uuid.uuid4()),
        "codebase_root": (
            "C:\\Users\\Yohannes\\Desktop\\tenx education\\"
            "Weeks\\week 3\\The Document Intelligence Refinery"
        ),
        "git_commit": git_commit,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "nodes": canonical_nodes,
        "edges": canonical_edges,
    }


def migrate() -> None:
    """Read lineage_graph.json, transform it to a canonical snapshot, write to JSONL."""
    print(f"Starting migration: {SOURCE_FILE} -> {TARGET_FILE}")

    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not SOURCE_FILE.exists():
        print(f"ERROR: Source file '{SOURCE_FILE}' not found.")
        raise SystemExit(1)

    print(f"Reading source graph from: {SOURCE_FILE}")
    with SOURCE_FILE.open("r", encoding="utf-8") as src:
        graph_data = json.load(src)

    source_nodes = graph_data.get("nodes", [])
    source_edges = graph_data.get("edges", [])
    print(f"  Source graph: {len(source_nodes)} node(s), {len(source_edges)} edge(s).")

    snapshot = transform_graph_to_snapshot(graph_data)

    with TARGET_FILE.open("w", encoding="utf-8") as dst:
        dst.write(json.dumps(snapshot) + "\n")

    print(
        f"Migration complete. Snapshot written to {TARGET_FILE} "
        f"({len(snapshot['nodes'])} nodes, {len(snapshot['edges'])} edges)."
    )


if __name__ == "__main__":
    migrate()
