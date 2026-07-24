"""
Step 1j — Dependency Graph Building

DependencyGraph builds a queryable directed graph from CodeChunk nodes and
DependencyEdge edges using NetworkX.  It runs architectural analysis and
exposes query methods used by the Review Agent.

Architectural checks:
  find_cycles()           → ARCH001: circular import/call chains
  find_layer_violations() → ARCH002: cross-domain CALLS edges
  find_orphans()          → ARCH003: nodes with zero incoming edges
  find_high_coupling()    → ARCH004: God functions/classes (out-degree ≥ 10)

Graph is persisted as JSON (NetworkX node-link format) so Stage 3 can load
it without re-running ingestion.

Production upgrade path: replace NetworkX with Neo4j by swapping only the
build(), save(), load(), and query methods — the public interface stays the same.

Usage:
    dep_graph = DependencyGraph()
    dep_graph.build(chunks, edges)
    dep_graph.analyse()
    dep_graph.save(path)
    deps  = dep_graph.get_dependencies(chunk_id, depth=1)
    stats = dep_graph.get_stats()
"""

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import networkx as nx
except ImportError as exc:
    raise ImportError(
        "networkx not installed. Run: pip install networkx"
    ) from exc

from core.models import CodeChunk, DependencyEdge

logger = logging.getLogger(__name__)

HIGH_COUPLING_THRESHOLD = 10  # out-degree above which a node is flagged


class DependencyGraph:
    """
    NetworkX DiGraph wrapper with architectural analysis and query methods.

    Nodes  = chunk_id (UUID string)
    Edges  = typed DependencyEdge objects stored as NetworkX edge attributes

    Node attributes:
        symbol_name, file_path, chunk_type, language, start_line, end_line
    """

    def __init__(self):
        self.graph: nx.DiGraph = nx.DiGraph()
        self._chunk_meta: Dict[str, dict] = {}   # chunk_id → metadata dict

        # Cached analysis results (computed once in analyse())
        self._cycles:     List[List[str]] = []
        self._violations: List[Dict]      = []
        self._orphans:    List[Dict]       = []
        self._high_coupling: List[Dict]   = []

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self, chunks: List[CodeChunk], edges: List[DependencyEdge]) -> None:
        """
        Populate the graph from chunks (nodes) and edges.

        Nodes are added first so that edge references are always valid.

        Args:
            chunks: All CodeChunks produced by Steps 1g–1i.
            edges:  DependencyEdge list from Step 1i.
        """
        self.graph = nx.DiGraph()
        self._chunk_meta = {}

        # Add nodes
        for chunk in chunks:
            meta = {
                "symbol_name": chunk.symbol_name,
                "file_path":   chunk.file_path,
                "chunk_type":  chunk.chunk_type.value,
                "language":    chunk.language,
                "start_line":  chunk.start_line,
                "end_line":    chunk.end_line,
                "layer":       chunk.layer,
                # Multi-label layers stored as comma-joined string (Priority 5).
                # Empty string means the chunk has only the primary `layer`.
                "layers":      ",".join(chunk.layers) if chunk.layers else "",
            }
            self.graph.add_node(chunk.chunk_id, **meta)
            self._chunk_meta[chunk.chunk_id] = meta

        # Add edges
        for edge in edges:
            self.graph.add_edge(
                edge.from_chunk_id,
                edge.to_chunk_id,
                edge_type       = edge.edge_type.value,
                from_symbol     = edge.from_symbol,
                to_symbol       = edge.to_symbol,
                from_file       = edge.from_file,
                to_file         = edge.to_file,
                is_cross_file   = edge.is_cross_file,
                is_cross_domain = edge.is_cross_domain,
                is_external     = edge.is_external,
                weight          = edge.weight,
            )

        logger.debug(
            "[DependencyGraph] Built: %d nodes, %d edges",
            self.graph.number_of_nodes(), self.graph.number_of_edges(),
        )

    # ── Analysis ─────────────────────────────────────────────────────────────

    def analyse(self) -> None:
        """
        Run all architectural checks.  Cache results so repeated queries are O(1).

        Checks:
            ARCH001 find_cycles()           → circular deps
            ARCH002 find_layer_violations() → cross-domain CALLS
            ARCH003 find_orphans()          → unreachable code
            ARCH004 find_high_coupling()    → God objects
        """
        self._cycles        = self.find_cycles()
        self._violations    = self.find_layer_violations()
        self._orphans       = self.find_orphans()
        self._high_coupling = self.find_high_coupling()
        logger.info(
            "[DependencyGraph] %d nodes, %d edges, %d cycles, %d violations",
            self.graph.number_of_nodes(),
            self.graph.number_of_edges(),
            len(self._cycles),
            len(self._violations),
        )

    # ── Query methods ─────────────────────────────────────────────────────────

    def get_dependencies(self, chunk_id: str, depth: int = 1) -> List[Dict]:
        """
        Outgoing traversal — what this chunk depends on.

        Used by the reviewer agent to expand context before an LLM call.

        Returns:
            List of node metadata dicts for all reachable successors.
        """
        if chunk_id not in self.graph:
            return []
        successors = nx.dfs_successors(self.graph, chunk_id, depth_limit=depth)
        ids = {nid for neighbours in successors.values() for nid in neighbours}
        return [self._chunk_meta[i] | {"chunk_id": i} for i in ids if i in self._chunk_meta]

    def get_dependents(self, chunk_id: str, depth: int = 1) -> List[Dict]:
        """
        Incoming traversal — what depends on this chunk (blast-radius analysis).

        Returns:
            List of node metadata dicts for all reachable predecessors.
        """
        if chunk_id not in self.graph:
            return []
        predecessors = nx.dfs_successors(self.graph.reverse(copy=False), chunk_id, depth_limit=depth)
        ids = {nid for neighbours in predecessors.values() for nid in neighbours}
        return [self._chunk_meta[i] | {"chunk_id": i} for i in ids if i in self._chunk_meta]

    def find_cycles(self) -> List[List[str]]:
        """
        Detect circular dependency chains.

        Capped at 1000 results to prevent OOM on pathological graphs.
        Maps to ReviewIssue rule_violated="ARCH001".
        """
        try:
            cycles = list(nx.simple_cycles(self.graph))
        except Exception as exc:
            logger.warning("[DependencyGraph] Cycle detection error: %s", exc)
            return []
        if len(cycles) > 1000:
            logger.warning(
                "[DependencyGraph] Truncating cycle list at 1000 (found %d)", len(cycles)
            )
        return cycles[:1000]

    def find_layer_violations(self) -> List[Dict]:
        """
        Find CALLS edges that violate the clean-architecture direction rule.

        Forbidden directions (single-label callers/callees only):
            presentation → data   (skips domain)
            data → presentation   (reverse dependency)

        Multi-label handling (Priority 5): if either the caller or callee
        carries multiple layer labels (stored as a comma-joined string in
        node attributes), the edge is NOT flagged — cross-cutting files
        (mappers, adapters) are legitimate bridges between layers.

        Falls back to checking is_cross_domain when layer attributes are
        missing (backward compat with graphs built without layer info).

        Maps to ReviewIssue rule_violated="ARCH002".
        """
        # Forbidden caller-layer → callee-layer pairs
        FORBIDDEN: set = {
            ("presentation", "data"),
            ("data", "presentation"),
        }

        violations = []
        for u, v, data in self.graph.edges(data=True):
            if data.get("edge_type") != "CALLS":
                continue

            from_node_data = self.graph.nodes.get(u, {})
            to_node_data   = self.graph.nodes.get(v, {})

            from_layers_raw = from_node_data.get("layers", "")
            to_layers_raw   = to_node_data.get("layers", "")

            # Parse multi-label: stored as comma-joined string in node attrs.
            # If the node has more than one layer, it is cross-cutting → skip.
            from_labels = [l for l in from_layers_raw.split(",") if l] if from_layers_raw else []
            to_labels   = [l for l in to_layers_raw.split(",")   if l] if to_layers_raw   else []

            from_layer = from_node_data.get("layer", "unknown")
            to_layer   = to_node_data.get("layer", "unknown")

            # Cross-cutting files are not violators
            if len(from_labels) > 1 or len(to_labels) > 1:
                continue

            # Direction-aware violation check (single-label only)
            is_layer_violation = (from_layer, to_layer) in FORBIDDEN

            # Fallback: cross-domain check (backward compat)
            is_cross_domain_violation = (
                from_layer == "unknown"
                and to_layer == "unknown"
                and data.get("is_cross_domain", False)
            )

            if is_layer_violation or is_cross_domain_violation:
                violations.append({
                    "from_file":   data.get("from_file", ""),
                    "to_file":     data.get("to_file", ""),
                    "from_symbol": data.get("from_symbol", ""),
                    "to_symbol":   data.get("to_symbol", ""),
                    "from_layer":  from_layer,
                    "to_layer":    to_layer,
                    "description": (
                        f"{data.get('from_file', '')} [{from_layer}] → "
                        f"{data.get('to_file', '')} [{to_layer}] "
                        f"(ARCH002: forbidden layer direction)"
                    ),
                })
        return violations

    def find_orphans(self) -> List[Dict]:
        """
        Find nodes with no incoming edges — potentially dead / unused code.

        Excludes MODULE and IMPORT chunks (they are root nodes by design).
        Excludes dunder methods (__ prefix / suffix).
        Maps to ReviewIssue rule_violated="ARCH003".
        """
        skip_types = {"module", "import"}
        orphans    = []
        for node_id in self.graph.nodes:
            if self.graph.in_degree(node_id) == 0:
                meta = self._chunk_meta.get(node_id, {})
                if meta.get("chunk_type") in skip_types:
                    continue
                name = meta.get("symbol_name", "")
                if name.startswith("__") and name.endswith("__"):
                    continue
                orphans.append(meta | {"chunk_id": node_id})
        return orphans

    def find_high_coupling(self, threshold: int = HIGH_COUPLING_THRESHOLD) -> List[Dict]:
        """
        Find nodes with out-degree ≥ threshold — God functions/classes.

        Maps to ReviewIssue rule_violated="ARCH004".
        """
        high = []
        for node_id in self.graph.nodes:
            degree = self.graph.out_degree(node_id)
            if degree >= threshold:
                meta = self._chunk_meta.get(node_id, {})
                high.append(meta | {"chunk_id": node_id, "out_degree": degree})
        return sorted(high, key=lambda x: -x["out_degree"])

    def get_stats(self) -> Dict[str, Any]:
        """
        Return a summary dict written into state["ingestion_stats"].
        """
        return {
            "total_nodes":        self.graph.number_of_nodes(),
            "total_edges":        self.graph.number_of_edges(),
            "cycles_found":       len(self._cycles),
            "layer_violations":   len(self._violations),
            "orphans_found":      len(self._orphans),
            "high_coupling":      len(self._high_coupling),
            "density":            nx.density(self.graph),
            "external_calls":     sum(
                1 for _, _, d in self.graph.edges(data=True)
                if d.get("is_external", False)
            ),
            "layer_distribution": dict(Counter(
                d.get("layer", "unknown")
                for _, d in self.graph.nodes(data=True)
            )),
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        """
        Serialise the graph to JSON (NetworkX node-link format).

        Args:
            path: File path to write, e.g. workspace/runs/{run_id}/graphs/dependency_graph.json
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = nx.node_link_data(self.graph)
        path.write_text(json.dumps(data, indent=2))
        logger.debug("[DependencyGraph] Saved to %s", path)

    def load(self, path: str | Path) -> None:
        """
        Restore graph from a previously saved JSON file.

        Args:
            path: File path written by save().
        """
        path = Path(path)
        data = json.loads(path.read_text())
        self.graph = nx.node_link_graph(data)
        # Rebuild _chunk_meta from node attributes
        self._chunk_meta = {
            node_id: attrs
            for node_id, attrs in self.graph.nodes(data=True)
        }
        logger.debug(
            "[DependencyGraph] Loaded %d nodes, %d edges from %s",
            self.graph.number_of_nodes(), self.graph.number_of_edges(), path,
        )
