import argparse
import json
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import git
import yaml


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Data Contract Attributor — investigates a failed validation report, "
            "identifies the root cause, and maps downstream impact."
        ),
    )
    parser.add_argument(
        "contract_path",
        help=(
            "File path to the YAML contract that had a validation failure "
            "(e.g. 'generated_contracts/week3_extractions.yaml')."
        ),
    )
    return parser


def load_latest_report(contract_id: str) -> dict:
    """Find and load the most recent validation report for the given contract.

    Args:
        contract_id: The contract identifier (e.g. 'week3-contract-v1').

    Returns:
        The parsed report as a Python dictionary.

    Raises:
        FileNotFoundError: If no matching report file exists.
    """
    reports_dir = Path("validation_reports")
    matching = [
        f for f in reports_dir.glob("*.json")
        if f.name.startswith(contract_id)
    ]

    if not matching:
        raise FileNotFoundError(
            f"No validation reports found for contract '{contract_id}' "
            f"in {reports_dir}/"
        )

    latest = max(matching, key=lambda f: f.stat().st_mtime)
    print(f"Loading report: {latest}")

    with latest.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def find_first_failure(report_data: dict) -> dict | None:
    """Return the first FAIL result from a validation report.

    Args:
        report_data: The parsed report dictionary from load_latest_report.

    Returns:
        The first result dict whose status is 'FAIL', or None if all passed.
    """
    for result in report_data.get("results", []):
        if result.get("status") == "FAIL":
            return result
    return None


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
        print(f"Warning: Registry not found at {registry_path}. Skipping lineage impact.")
        return []


def load_lineage_graph() -> dict | None:
    """Load the first lineage snapshot record from the week4 JSONL file.

    Returns:
        The parsed snapshot dict, or None if the file does not exist.
    """
    lineage_path = Path("outputs/week4/lineage_snapshots.jsonl")
    try:
        with lineage_path.open("r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
        if not first_line:
            return None
        return json.loads(first_line)
    except FileNotFoundError:
        print(f"Warning: Lineage file not found at {lineage_path}. Skipping graph data.")
        return None


def get_blast_radius_from_registry(contract_id: str, subscriptions: list[dict]) -> dict:
    """Determine which downstream subscribers are affected by a contract failure.

    Args:
        contract_id: The contract identifier whose failure is being investigated.
        subscriptions: The full list of subscription dicts from the registry.

    Returns:
        A dict with key 'affected_nodes' containing a list of subscriber IDs.
    """
    affected = [
        sub["subscriber_id"]
        for sub in subscriptions
        if sub.get("contract_id") == contract_id
    ]
    return {"affected_nodes": affected}


def enrich_blast_radius_with_lineage(
    blast_radius: dict,
    lineage: dict | None,
) -> dict:
    """Extend a registry-based blast radius with transitively contaminated nodes.

    The registry identifies direct subscribers (depth 0). This function walks
    the lineage graph **forwards** (source → target, PRODUCES direction) from
    each of those nodes to discover second- and third-order systems that receive
    data produced by the directly affected subscribers.

    BFS is used so that contamination_depth accurately reflects the number of
    hops from the original failure point.

    Args:
        blast_radius: Dict returned by get_blast_radius_from_registry, containing
            an 'affected_nodes' list of subscriber ID strings.
        lineage: The lineage snapshot dict from load_lineage_graph, or None.

    Returns:
        An enriched blast radius dict with two keys:
            affected_nodes  — original list of direct subscriber IDs (depth 0)
            transitive_nodes — list of dicts {node_id, contamination_depth} for
                              every transitively reachable node beyond depth 0
    """
    if lineage is None:
        return {**blast_radius, "transitive_nodes": []}

    edges: list[dict] = lineage.get("edges", [])

    # Build a forward adjacency map: source_id → [target_id, ...]
    forward_adj: dict[str, list[str]] = {}
    for edge in edges:
        source = edge.get("source", "")
        target = edge.get("target", "")
        if source and target:
            forward_adj.setdefault(source, []).append(target)

    direct_nodes: list[str] = blast_radius.get("affected_nodes", [])

    # BFS starting from all direct nodes at depth 0.
    visited: set[str] = set(direct_nodes)
    queue: deque[tuple[str, int]] = deque((node, 0) for node in direct_nodes)
    transitive_nodes: list[dict] = []

    while queue:
        current_id, depth = queue.popleft()
        for child_id in forward_adj.get(current_id, []):
            if child_id not in visited:
                visited.add(child_id)
                transitive_nodes.append({
                    "node_id": child_id,
                    "contamination_depth": depth + 1,
                })
                queue.append((child_id, depth + 1))

    return {
        "affected_nodes": direct_nodes,
        "transitive_nodes": transitive_nodes,
    }


def find_upstream_source_files(contract: dict, lineage: dict) -> list[dict]:
    """Trace the lineage graph backwards from the contract's data file to find
    upstream Python and SQL source files.

    Edges in the graph point source → target (PRODUCES direction). To walk
    upstream we reverse this: for each visited node we look for edges where
    target == current_node_id and follow them back to their source.

    Args:
        contract: The parsed contract dictionary (used to derive the start node).
        lineage: The first lineage snapshot record from load_lineage_graph.

    Returns:
        A list of dicts with keys 'file_path' and 'depth', one per Python/SQL
        source file found in the upstream ancestry.
    """
    nodes: list[dict] = lineage.get("nodes", [])
    edges: list[dict] = lineage.get("edges", [])

    # Index nodes by node_id for O(1) lookup.
    node_by_id: dict[str, dict] = {n["node_id"]: n for n in nodes}

    # Build a reverse adjacency map: target_id → [source_id, ...]
    reverse_adj: dict[str, list[str]] = {}
    for edge in edges:
        target = edge.get("target", "")
        source = edge.get("source", "")
        reverse_adj.setdefault(target, []).append(source)

    # Derive the starting node_id from the contract's server path.
    server_path = contract.get("servers", {}).get("local", {}).get("path", "")
    start_node_id = f"file::{server_path}"

    if start_node_id not in node_by_id:
        print(f"  Note: start node '{start_node_id}' not found in lineage graph.")

    source_files: list[dict] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_node_id, 0)])

    while queue:
        current_id, depth = queue.popleft()
        if current_id in visited:
            continue
        visited.add(current_id)

        node = node_by_id.get(current_id)
        if node is not None:
            language = (node.get("metadata") or {}).get("language") or ""
            if language in ("python", "sql"):
                file_path = (node.get("metadata") or {}).get("path", "")
                if file_path:
                    source_files.append({"file_path": file_path, "depth": depth})

        for upstream_id in reverse_adj.get(current_id, []):
            if upstream_id not in visited:
                queue.append((upstream_id, depth + 1))

    return source_files


def get_git_blame_for_files(source_files: list[dict], codebase_root: str) -> list[dict]:
    """Find recent commits for each upstream source file."""
    blame_candidates: list[dict] = []
    
    print("\n--- ENTERING GIT BLAME FUNCTION ---")
    print(f"Attempting to open Git repo at: '{codebase_root}'")
    
    try:
        repo = git.Repo(codebase_root)
        print("  ✅ Git repo opened successfully.")
    except (git.InvalidGitRepositoryError, git.NoSuchPathError) as exc:
        print(f"  ❌ CRITICAL ERROR: Could not open git repo at '{codebase_root}'.")
        print(f"  Reason: {exc}")
        return blame_candidates

    print(f"Found {len(source_files)} source file(s) to check.")
    for file in source_files:
        print(f"DEBUG: Running git log on path: {file['file_path']}")
        file_path = file["file_path"]
        print(f"\nProcessing file: '{file_path}'")
        try:
            # Using list() to force the iterator to execute immediately for debugging
            commits = list(repo.iter_commits(paths=file_path, since="90 days ago"))
            print(f"  Found {len(commits)} commits for this file in the last 90 days.")
            
            for commit in commits:
                candidate = {
                    "file_path": file_path,
                    "lineage_depth": file["depth"],
                    "commit_sha": commit.hexsha,
                    "author_email": commit.author.email,
                    "committed_datetime": commit.committed_datetime.isoformat(),
                    "summary": commit.summary,
                }
                blame_candidates.append(candidate)
                print(f"    -> Added candidate commit: {commit.hexsha[:7]} by {commit.author.email}")

        except Exception as exc:
            print(f"  ❌ WARNING: An unexpected error occurred while processing commits for '{file_path}'.")
            print(f"  Reason: {exc}")
            
    print("--- EXITING GIT BLAME FUNCTION ---")
    return blame_candidates


def rank_blame_chain(blame_candidates: list[dict]) -> list[dict]:
    """Score, sort, and return the top 5 blame candidates.

    Each candidate is scored by recency (days since commit) and proximity
    (lineage depth). More recent and shallower commits rank higher.

    Formula: score = 1.0 - (days_since_commit * 0.1) - (lineage_depth * 0.2)

    Args:
        blame_candidates: Output of get_git_blame_for_files.

    Returns:
        Up to the top 5 candidates sorted by confidence_score descending,
        each augmented with 'confidence_score' and 'rank' keys.
    """
    now = datetime.now(timezone.utc)

    for candidate in blame_candidates:
        committed = datetime.fromisoformat(candidate["committed_datetime"])
        # Ensure tz-aware for subtraction.
        if committed.tzinfo is None:
            committed = committed.replace(tzinfo=timezone.utc)
        days_since_commit = (now - committed).total_seconds() / 86400
        candidate["confidence_score"] = (
            1.0
            - (days_since_commit * 0.1)
            - (candidate["lineage_depth"] * 0.2)
        )

    sorted_candidates = sorted(
        blame_candidates, key=lambda c: c["confidence_score"], reverse=True
    )

    for rank, candidate in enumerate(sorted_candidates[:5], start=1):
        candidate["rank"] = rank

    return sorted_candidates[:5]


def log_violation(
    failed_check: dict,
    blame_chain: list[dict],
    blast_radius: dict,
) -> None:
    """Append a violation record to the violation log JSONL file.

    Args:
        failed_check: The first failed check dict from find_first_failure.
        blame_chain: The ranked blame chain from rank_blame_chain.
        blast_radius: The blast radius dict from get_blast_radius_from_registry.
    """
    violation = {
        "violation_id": str(uuid.uuid4()),
        "check_id": failed_check["check_id"],
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "blame_chain": blame_chain,
        "blast_radius": blast_radius,
    }

    output_path = Path("violation_log/violations.jsonl")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(violation) + "\n")

    print(f"Violation logged to: {output_path}  (id={violation['violation_id']})")


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        contract_path = Path(args.contract_path)
        if not contract_path.exists():
            raise FileNotFoundError(f"Contract file not found: {contract_path}")

        with contract_path.open("r", encoding="utf-8") as fh:
            contract = yaml.safe_load(fh)

        contract_id = contract.get("id", "")
        print(f"Investigating contract: '{contract_id}'")

        report_data = load_latest_report(contract_id)
        failed_check = find_first_failure(report_data)

        if failed_check is None:
            print(f"\nNo failures found in the latest report for '{contract_id}'. Nothing to attribute.")
            raise SystemExit(0)

        print(f"\nInvestigating failed check: {failed_check['check_id']}")

        subscriptions = load_registry()
        print(f"Loaded Contract Registry with {len(subscriptions)} subscriptions.")

        lineage = load_lineage_graph()
        if lineage is not None:
            nodes = lineage.get("nodes", [])
            edges = lineage.get("edges", [])
            print(f"Loaded Lineage Graph with {len(nodes)} nodes and {len(edges)} edges.")
        else:
            print("Loaded Lineage Graph with 0 nodes and 0 edges.")

        blast_radius = get_blast_radius_from_registry(contract_id, subscriptions)
        blast_radius = enrich_blast_radius_with_lineage(blast_radius, lineage)
        print(f"\nBlast Radius (from Registry): {blast_radius['affected_nodes']}")
        print(f"Transitive Contamination ({len(blast_radius['transitive_nodes'])} node(s)):")
        for node in blast_radius["transitive_nodes"]:
            print(f"  depth={node['contamination_depth']}  {node['node_id']}")

        source_files = find_upstream_source_files(contract, lineage) if lineage is not None else []
        print(f"\nFound {len(source_files)} upstream source file(s).")

        codebase_root = lineage.get("codebase_root", "") if lineage is not None else ""
        blame_candidates = get_git_blame_for_files(source_files, codebase_root) if source_files else []

        blame_chain = rank_blame_chain(blame_candidates)
        log_violation(failed_check, blame_chain, blast_radius)

        print("\nViolation logged successfully.")

    except (FileNotFoundError, KeyError) as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1)
