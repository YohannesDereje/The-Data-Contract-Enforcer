# DOMAIN NOTES: The Data Contract Enforcer

**Author:** Yohannes Dereje
**Date:** 2026-04-01

This document outlines the core concepts, design rationale, and strategic thinking behind the Data Contract Enforcer project. It serves as a foundational reference for understanding the principles of data-first engineering, schema evolution, and automated quality enforcement within our AI-driven ecosystem.

---

### 1. Backward-Compatible vs. Breaking Schema Changes

A fundamental challenge in any distributed system is managing schema evolution. The distinction between a "backward-compatible" change and a "breaking" change is critical for maintaining system uptime and data integrity.

A **backward-compatible change** is an additive modification to a data schema. It enhances the schema without disrupting the functionality of downstream consumers that are unaware of the change. These consumers can continue to parse and process the data as they always have, simply ignoring the new elements.

In contrast, a **breaking change** is a modification that violates the assumptions of a downstream consumer. It removes, renames, or fundamentally alters an existing field that other systems depend on. This forces the consumer to either fail catastrophically or, more dangerously, to process the data incorrectly, leading to silent failures.

Here are three examples of each type, drawn from our Weeks 1-5 systems:

#### **Examples of Backward-Compatible Changes:**

1.  **Adding an Optional Field (Week 1: Agent Trace):** In our `agent_trace.jsonl` output, we could add a new `retry_count` field to the `metadata` object.
    *   **Before:** `{"metadata": {"agent_name": "Planner"}}`
    *   **After:** `{"metadata": {"agent_name": "Planner", "retry_count": 2}}`
    *   **Reasoning:** A downstream log analysis system that calculates agent success rates would continue to function perfectly. It would read the `agent_name` and other fields it knows about and simply ignore the new `retry_count` field it wasn't programmed to expect.

2.  **Adding a New, Non-Required Nested Object (Week 2: Verdicts):** Based on our Week 2 "AI Peer Review Agent" report, we can define a hypothetical `verdicts.jsonl` schema. If we add a new, optional object for `plagiarism_analysis`, existing systems are unaffected.
    *   **Before:** `{"record_id": "...", "final_assessment": "Good", "rubric_scores": {"clarity": 4}}`
    *   **After:** `{"record_id": "...", "final_assessment": "Good", "rubric_scores": {"clarity": 4}, "plagiarism_analysis": {"status": "PASSED", "score": 0.05}}`
    *   **Reasoning:** A dashboard that displays the `final_assessment` and `rubric_scores` will not break. It has no dependency on the `plagiarism_analysis` object and will not even attempt to parse it.

3.  **Adding a New Enum Value (Week 5: Events):** In our `events.jsonl` from the event sourcing system, the `event_type` field could be expanded. Let's say we add a new event, `LoanApplicationResubmitted`.
    *   **Existing Values:** `LoanApplicationStarted`, `CreditScoreChecked`
    *   **New Value:** `LoanApplicationResubmitted`
    *   **Reasoning:** A consumer that is only responsible for handling `LoanApplicationStarted` events (e.g., to create a new customer record) would simply see this new event type, determine it's not relevant, and discard it. Its core functionality remains intact. This assumes the consumer uses a `case` statement or `if/elif` block with a default `else` pass, which is standard practice.

#### **Examples of Breaking Changes:**

1.  **Renaming a Critical Key (Week 3: Extractions):** Our `extractions.jsonl` file, after migration, has a `confidence_score` field. If an upstream change in the Week 3 system renamed this to `extraction_confidence`, it would be a breaking change.
    *   **Before:** `{"record_id": "...", "confidence_score": 0.95}`
    *   **After:** `{"record_id": "...", "extraction_confidence": 0.95}`
    *   **Reasoning:** The Week 4 Cartographer system, which might use this score to determine the reliability of an edge in the lineage graph, would fail with a `KeyError` when trying to access `record['confidence_score']`.

2.  **Changing a Data Type (Week 2: Verdicts):** In our hypothetical `verdicts.jsonl` schema, the `rubric_scores` field is a dictionary of string-to-integer mappings (`{"clarity": 4}`). If this were changed to a list of strings (`["clarity: 4/5"]`), it would break any system performing mathematical operations.
    *   **Before:** `{"rubric_scores": {"clarity": 4, "evidence": 3}}`
    *   **After:** `{"rubric_scores": ["clarity: 4/5", "evidence: 3/5"]}`
    *   **Reasoning:** A system designed to calculate the average rubric score would crash. It would attempt to iterate over dictionary items (`.items()`) or access keys (`['clarity']`), both of which would fail on a list.

3.  **Removing a Required Field (Week 5: Events):** The `aggregate_id` in our `events.jsonl` is essential for associating an event with a specific loan application. If this field were removed, the data would become unusable.
    *   **Before:** `{"event_id": "...", "aggregate_id": "loan-app-123", "event_type": "CreditScoreChecked"}`
    *   **After:** `{"event_id": "...", "event_type": "CreditScoreChecked"}`
    *   **Reasoning:** The downstream projection builder, responsible for reconstructing the current state of a loan application, would be unable to identify which application to update. The event, while valid in isolation, would be meaningless without its aggregate context.

---

### 2. The Confidence Score Change and a Contract Clause

The scenario of the Week 3 `confidence_score` changing from a `float` (0.0-1.0) to an `integer` (0-100) is a classic example of a **statistical breaking change**. The schema structure (field name, data type family) might seem the same, but the semantic meaning and expected range of the data are violated.

**Trace of Failure:**

1.  **Week 3 (Source):** A developer updates the Document Refinery, changing the confidence calculation to output an integer score of 95 instead of a float of 0.95. The change is deployed.
2.  **Data at Rest:** The `extractions.jsonl` file now contains records like `{"confidence_score": 95}`.
3.  **Week 4 (Consumer):** The Cartographer system ingests this record. It has a function to assess the "quality" of a lineage graph edge, defined as `quality = 0.5 + (0.5 * confidence_score)`.
4.  **Silent Failure:** The calculation now becomes `quality = 0.5 + (0.5 * 95)`, which results in a quality score of `48.0`. The system was designed to work with quality scores in the range of `0.5` to `1.0`.
5.  **Incorrect Output:** The Cartographer, now operating on wildly incorrect quality values, might incorrectly prune what it deems "low-quality" edges or produce a graph with nonsensical weights. It doesn't crash, but its output is corrupted, leading to flawed downstream analysis and a loss of trust in the lineage data.

**The Data Contract Clause:**

This failure would be caught instantly by a well-defined data contract. The `quality` section of a Bitol-compatible YAML contract would explicitly define the statistical properties of the `confidence_score` field.

```yaml
quality:
  # This section defines statistical and semantic rules for the data.
  - type: row-constraint
    name: "Confidence score must be a probability float between 0.0 and 1.0"
    description: "The confidence_score from the extraction model represents a probability and its value must be constrained to the [0.0, 1.0] range."
    field: confidence_score
    must_be:
      - ">=" : 0.0
      - "<=" : 1.0
  - type: schema
    name: "Schema must be valid"
    # This clause ensures the data type itself is a number (float or int)
    # but the row-constraint above provides the critical range check.

The ValidationRunner would execute the checks defined in this row-constraint clause. Upon encountering the value 95, the must_be: [ "<=", 1.0 ] check would fail, immediately flagging the violation and preventing the corrupted data from ever reaching the Cartographer.

3. Blame Chain Attribution via the Lineage Graph
The Data Contract Enforcer's most powerful feature is its ability to move beyond simple detection and perform lineage-based attribution. It connects a data failure directly to a code change and an author. This is achieved by a specific graph traversal algorithm using the lineage graph produced by the Week 4 Cartographer.

Step-by-Step Blame Chain Process:

Violation Detected: The ValidationRunner detects a violation in a data file, for example, in outputs/week3/extractions.jsonl. The failing field is confidence_score.

Identify Failing Node: The Enforcer looks up the data file (outputs/week3/extractions.jsonl) in the lineage_snapshots.jsonl graph. It finds the corresponding node_id for this file. Let's call it node_extractions.

Initiate Backward Traversal: The core of the attribution is a backward traversal on the directed lineage graph, starting from node_extractions. The goal is to find all upstream nodes that could have influenced this file. The algorithm traces all incoming edges to node_extractions.

Explore Upstream Paths: Let's say the graph shows one incoming edge from a node representing a source code file: node_refinery_script (/src/week3/refine.py). The algorithm has found a potential source. It continues the traversal backward from node_refinery_script. Perhaps this script depends on a shared utility file, node_utils, creating another path.

Identify Candidate Source Files: The traversal collects a set of all unique source code files found along these upstream paths (e.g., {'src/week3/refine.py', 'src/shared/utils.py'}). These are the candidate files.

Query Git History: For each candidate file, the Enforcer uses the gitpython library to query its commit history. It executes a command equivalent to git blame or git log -- <file_path> on the repository corresponding to that file (e.g., the Week 3 repo).

Score and Rank Commits: The system retrieves a list of recent commits that modified each candidate file. Each commit is a "blame candidate" and is scored based on two factors:

Temporal Proximity: How recently the commit occurred. More recent commits are more likely to be the cause.

Lineage Distance: How many "hops" away the candidate file is from the failing node in the lineage graph. A direct dependency (1 hop) is more likely to be the cause than a transitive dependency (3 hops).

The formula provided in the practitioner manual, confidence = 1.0 - (days_since_commit * 0.1) - (lineage_hops * 0.2), is applied to each commit to generate a ranked list.

Produce Blame Chain: The top-ranked commits (typically 1-5 candidates) are presented as the final "blame chain." Each entry includes the commit hash, author, commit message, and the file that was changed, providing an actionable starting point for debugging.

4. Data Contract for LangSmith Trace Records
The trace_record schema from LangSmith is an excellent candidate for a data contract, as it contains a mix of structural, statistical, and AI-specific data.

yaml
kind: Contract
apiVersion: 1.0.0
id: langsmith-traces-v1
info:
  title: LangSmith Trace Record Contract
  description: >
    Defines the expected structure, quality, and semantic properties of trace
    records exported from LangSmith for AI agent performance monitoring.
  owner: yohannesdereje1221@gmail.com

schema:
  type: object
  required:
    - trace_id
    - start_time
    - end_time
    - name
    - status
  properties:
    trace_id:
      type: string
      pattern: "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    name:
      type: string
      enum: ["Toolbelt", "Planner", "FunctionCall", "LLM", "Tool"]
    duration_ms:
      type: number
    status:
      type: string
      enum: ["SUCCESS", "ERROR"]
    # ... other properties

quality:
  - type: row-constraint
    name: "Trace duration must be non-negative"
    field: duration_ms
    must_be:
      - ">=": 0

  - type: ai-embedding-drift
    name: "Semantic drift detection for LLM inputs"
    description: >
      Monitors for significant changes in the semantic meaning of the prompts
      being sent to the core planner LLM, which could indicate a change in user
      behavior or an upstream data corruption issue.
    field: inputs.prompt # Assumes a nested field containing the text prompt
    max_distance: 0.2 # Maximum allowable cosine distance from baseline centroid
Clause Breakdown:

Structural Clause: The pattern defined for the trace_id property is a structural check. It uses a regular expression to enforce that the trace_id is always a valid UUID. This prevents malformed IDs from entering the system.

Statistical Clause: The row-constraint on duration_ms is a statistical check. It ensures that the trace duration is always a non-negative number, catching any calculation errors or data corruption that might result in a negative value.

AI-Specific Clause: The ai-embedding-drift clause is a powerful, AI-specific check. It goes beyond structure and simple statistics. It calculates the embedding vector for the text in the inputs.prompt field and compares its position in vector space to a baseline centroid of "normal" prompts. If the new prompt is too far away (a cosine distance greater than 0.2), it signals a significant semantic shift, alerting us to potential model performance degradation even if the data's structure is perfectly valid.

5. Preventing Contract Staleness
The most common failure mode of contract enforcement systems in production is contract staleness. This occurs when the code and the data it produces evolve, but the data contracts that govern them are not updated in tandem. The contracts become outdated, leading to one of two negative outcomes:

False Positives: The contract forbids a valid, intentional change, leading to a high volume of spurious alerts. Engineers begin to ignore the alerts, and the entire system loses credibility.

False Negatives: The contract is too permissive or doesn't cover new fields/data types, failing to catch genuine breaking changes. This provides a false sense of security while silent failures proliferate.

Contracts get stale because updating them is often a manual, high-friction process disconnected from the regular development workflow. An engineer pushes a code change but forgets, or doesn't know how, to update the corresponding YAML contract file.

Our Architecture's Prevention Mechanism:

Our Data Contract Enforcer architecture directly combats this failure mode through automated contract generation and evolution as a core part of the development lifecycle.

The key component is the ContractGenerator (generator.py).

Instead of being a one-off manual task, writing contracts is a repeatable, automated step. The ContractGenerator reads the actual output of a system and uses statistical profiling (ydata-profiling) to generate a baseline contract that reflects the ground truth of the data.

This fundamentally changes the workflow:

Code Change: A developer modifies an upstream system (e.g., the Week 3 Document Refinery).

Generate New Data: They run the updated system, which produces a new extractions.jsonl file.

Regenerate Contract: They run python contracts/generator.py. This creates a new contract based on the new data.

Analyze Evolution: They run the SchemaEvolutionAnalyzer (schema_analyzer.py), which programmatically diffs the new contract against the previously version-controlled snapshot.

Review and Commit: The analyzer provides a clear verdict: BREAKING or COMPATIBLE. The developer can now make an informed decision. If the breaking change is intentional, they commit both the code change and the new contract snapshot in the same pull request.

This "contract-as-code" approach, where contracts are generated and versioned alongside the code that produces the data, ensures they can never become stale. It makes updating the contract an explicit, low-friction, and mandatory part of the development process, solving the primary cause of contract decay in production systems.