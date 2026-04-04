from pathlib import Path
import json

lineage_path = Path("outputs/week4/lineage_snapshots.jsonl")
old_node_id = "file::src/week3/refine.py"
new_node_id = "file::src/agents/orchestrator.py"
new_path = "src/agents/orchestrator.py"
new_label = "orchestrator.py"

print(f"Attempting to patch file: {lineage_path}")

try:
    # Read the single line of JSON
    content = lineage_path.read_text(encoding="utf-8")
    data = json.loads(content)

    # Find and update the node
    node_found = False
    for node in data.get("nodes", []):
        if node.get("node_id") == old_node_id:
            print(f"  Found node to update: '{old_node_id}'")
            node["node_id"] = new_node_id
            node["label"] = new_label
            if "metadata" in node:
                node["metadata"]["path"] = new_path
            node_found = True
            print(f"  --> Updated node to: '{new_node_id}'")
            break
    
    if not node_found:
        print(f"  Warning: Node '{old_node_id}' not found. No node was changed.")

    # Find and update the edge
    edge_found = False
    for edge in data.get("edges", []):
        if edge.get("source") == old_node_id:
            print(f"  Found edge to update: source='{old_node_id}'")
            edge["source"] = new_node_id
            edge_found = True
            print(f"  --> Updated edge source to: '{new_node_id}'")
            break
            
    if not edge_found:
        print(f"  Warning: No edge with source '{old_node_id}' found. No edge was changed.")

    # Write the corrected data back to the file
    lineage_path.write_text(json.dumps(data), encoding="utf-8")
    
    print("\nPatch complete. The lineage snapshot has been updated.")

except FileNotFoundError:
    print(f"Error: Could not find the lineage file at {lineage_path}")
except Exception as e:
    print(f"An unexpected error occurred: {e}")

