# SKILL — Step 1j: Dependency Graph Building

## Purpose

Build a queryable directed dependency graph from `CodeChunk` nodes and
`DependencyEdge` edges using NetworkX. Analyse the graph for architectural
violations. Persist to disk. Expose query methods used by the Review Agent
for context expansion and issue detection.

---

## Implemented In

`ingestion/graph_builder.py` → class `DependencyGraph`

---

## Input / Output

```python
dep_graph = DependencyGraph()
dep_graph.build(chunks=List[CodeChunk], edges=List[DependencyEdge])
dep_graph.analyse()   # runs cycle / violation / orphan detection
dep_graph.save(path="workspace/runs/{run_id}/graphs/dependency_graph.json")
```

---

## Graph Structure

```
Nodes  = chunk_id  (one per CodeChunk)
Edges  = DependencyEdge  (directed, typed, attributed)

Node attributes stored in _chunk_meta dict:
  symbol_name, file_path, chunk_type, language, start_line, end_line

Edge attributes:
  edge_type, from_symbol, to_symbol,
  from_file, to_file,
  is_cross_file, is_cross_domain, weight
```

---

## Build

```python
def build(self, chunks: List[CodeChunk], edges: List[DependencyEdge]):
    self.graph = nx.DiGraph()

    # Add all nodes first
    for chunk in chunks:
        self.graph.add_node(chunk.chunk_id, **{
            "symbol_name": chunk.symbol_name,
            "file_path":   chunk.file_path,
            "chunk_type":  chunk.chunk_type.value,
            "language":    chunk.language,
            "start_line":  chunk.start_line,
            "end_line":    chunk.end_line,
        })

    # Add edges (both nodes guaranteed to exist)
    for edge in edges:
        self.graph.add_edge(edge.from_chunk_id, edge.to_chunk_id, **{
            "edge_type":       edge.edge_type.value,
            "from_symbol":     edge.from_symbol,
            "to_symbol":       edge.to_symbol,
            "from_file":       edge.from_file,
            "to_file":         edge.to_file,
            "is_cross_file":   edge.is_cross_file,
            "is_cross_domain": edge.is_cross_domain,
            "weight":          edge.weight,
        })
```

---

## Query Methods

### `get_dependencies(chunk_id, depth=1) → List[Dict]`
Outgoing traversal — what this chunk depends on.
Used by reviewer_agent to expand context before LLM call.

```python
successors = nx.dfs_successors(self.graph, chunk_id, depth_limit=depth)
```

### `get_dependents(chunk_id, depth=1) → List[Dict]`
Incoming traversal — what depends on this chunk (blast radius / impact analysis).

```python
predecessors = nx.dfs_predecessors(self.graph.reverse(), chunk_id, depth_limit=depth)
```

### `find_cycles() → List[List[str]]`
Detects circular import / call chains.
Maps to `ReviewIssue` with `rule_violated="ARCH001"`, `severity=HIGH`.

```python
cycles = list(nx.simple_cycles(self.graph))
# Cap at 1000 to avoid OOM on pathological graphs
return cycles[:1000]
```

### `find_layer_violations() → List[Dict]`
Scans all edges with `is_cross_domain=True` and `edge_type=CALLS`.
A controller calling a database layer directly is a violation.
Maps to `ReviewIssue` with `rule_violated="ARCH002"`, `severity=HIGH`.

```python
violations = []
for u, v, data in self.graph.edges(data=True):
    if data.get("is_cross_domain") and data.get("edge_type") == "CALLS":
        violations.append({
            "from_file":   data["from_file"],
            "to_file":     data["to_file"],
            "from_symbol": data["from_symbol"],
            "to_symbol":   data["to_symbol"],
            "description": f"{data['from_file']} → {data['to_file']} (cross-domain CALLS)",
        })
return violations
```

### `find_orphans() → List[Dict]`
Nodes with zero incoming edges — potentially unused code.
Excludes: MODULE, IMPORT chunks and `__` magic-method names.
Maps to `ReviewIssue` with `rule_violated="ARCH003"`, `severity=LOW`.

### `find_high_coupling(threshold=10) → List[Dict]`
Nodes with `out_degree >= threshold` — God functions / classes.
Maps to `ReviewIssue` with `rule_violated="ARCH004"`, `severity=MEDIUM`.

### `get_stats() → Dict`
Returns summary statistics for `state["ingestion_stats"]`.

```python
return {
    "total_nodes":        self.graph.number_of_nodes(),
    "total_edges":        self.graph.number_of_edges(),
    "cycles_found":       len(self._cycles),
    "layer_violations":   len(self._violations),
    "orphans_found":      len(self._orphans),
    "density":            nx.density(self.graph),
}
```

---

## Persistence Format

Serialise to JSON using NetworkX node-link format:

```python
def save(self, path: str):
    data = nx.node_link_data(self.graph)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load(self, path: str):
    with open(path) as f:
        data = json.load(f)
    self.graph = nx.node_link_graph(data)
```

---

## Production Upgrade: Neo4j

POC uses NetworkX + JSON. Production upgrade to Neo4j requires only replacing
the `build()`, `save()`, `load()`, and query methods — the public interface stays identical.

```
Cypher equivalents:
  get_dependencies(depth=2):
    MATCH (a {chunk_id: $id})-[*1..2]->(b) RETURN b

  find_cycles():
    MATCH path=(a)-[*]->(a) RETURN nodes(path)

  find_layer_violations():
    MATCH (a)-[r:CALLS {is_cross_domain: true}]->(b) RETURN a, r, b
```

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Adding edges before nodes | Always add all nodes first, then edges |
| Using symbol_name as node ID | Node IDs are `chunk_id` UUIDs — symbol name is metadata |
| Calling `find_cycles()` repeatedly in a hot loop | Cache result in `self._cycles` after first call |
| Saving before `build()` completes | Call `save()` only after `build()` and `analyse()` return |
| Not capping `find_cycles()` | Cap at 1000 — `nx.simple_cycles()` is O(n+e), can OOM |

---

## Validation Checklist

- [ ] `graph.number_of_nodes()` == `len(chunks)`
- [ ] `graph.number_of_edges()` == `len(edges)`
- [ ] `save()` writes valid JSON to `workspace/runs/{run_id}/graphs/`
- [ ] `load()` restores same node and edge counts
- [ ] `get_dependencies(chunk_id)` returns non-empty for chunks with outgoing edges
- [ ] `find_cycles()` returns `[]` on a clean repo with no circular deps
- [ ] `get_stats()` returns all expected keys
- [ ] `[DependencyGraph] {n} nodes, {e} edges, {c} cycles, {v} violations` logged
