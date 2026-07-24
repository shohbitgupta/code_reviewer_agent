# SKILL — Step 1i: Dependency Extraction

## Purpose

Analyse `ParsedFile` symbols and the chunk map to extract **typed directed edges**
between `CodeChunk` nodes. These edges form the dependency graph built in Step 1j
and are used by the Review Agent for context expansion and architectural violation detection.

---

## Implemented In

`ingestion/dependency_extractor.py` → class `DependencyExtractor`

---

## Input / Output

```python
extractor = DependencyExtractor(repo_root="./workspace/repos/project")
edges: List[DependencyEdge] = extractor.extract(
    parsed_files = List[ParsedFile],
    chunk_map    = Dict[str, CodeChunk]   # keyed by chunk_id AND "file_path::symbol_name"
)
```

---

## Edge Types

### BELONGS_TO
Method chunk → parent class chunk.
Source: `ParsedSymbol.parent_name` on method symbols.

```
method: validate_token  ──BELONGS_TO──▶  class_head: AuthService
method: login           ──BELONGS_TO──▶  class_head: AuthService
```

### CALLS
Function/method → another function/method it calls.
Source: `ParsedSymbol.calls[]`.

```
method: login           ──CALLS──▶  method: validate_token
method: login           ──CALLS──▶  method: hash_password
function: process_order ──CALLS──▶  function: charge_card
```

Resolution: match called name (or `obj.method` tail) against chunk symbol names.
Unresolved calls (stdlib, third-party) are **silently dropped** — log the count.

### IMPORTS
File import chunk → MODULE chunk of the imported local file.
Source: `ParsedSymbol.imports[]` on import-type symbols.

```
import_chunk: auth/service.py  ──IMPORTS──▶  module_chunk: utils/crypto.py
import_chunk: api/views.py     ──IMPORTS──▶  module_chunk: services/user.py
```

Resolution: `"from utils.crypto import ..."` → normalise to `"utils/crypto.py"` →
check against `file_manifest`. Third-party imports are **dropped**.

### INHERITS
Class chunk → base class chunk.
Source: `ParsedSymbol.bases[]` on class_head symbols.

```
class_head: AdminUser  ──INHERITS──▶  class_head: BaseUser
class_head: APIView    ──INHERITS──▶  class_head: View
```

---

## DependencyEdge Schema

```python
@dataclass
class DependencyEdge:
    from_chunk_id:   str
    to_chunk_id:     str
    edge_type:       EdgeType       # BELONGS_TO | CALLS | IMPORTS | INHERITS
    from_symbol:     str            # "AuthService.login"
    to_symbol:       str            # "validate_token"
    from_file:       str
    to_file:         str
    is_cross_file:   bool           # from_file != to_file
    is_cross_domain: bool           # top-level dirs differ  (auth/ vs db/)
    raw_import:      Optional[str]  # original import statement string
    weight:          float          # 1.0 default; increase for hot paths
```

---

## Symbol Resolution

Resolution is attempted in priority order:

```
1. Exact key match:  chunk_map["file_path::symbol_name"]
2. Symbol name:      chunk where chunk.symbol_name == called_name
3. Tail strip:       "self.validate" → strip "self." → match "validate"
4. Drop:             unresolvable (stdlib, pip packages, external)
```

When multiple chunks share the same symbol name across files, prefer
chunks in the **same file** as the calling chunk first.

---

## Cross-Domain Detection

```python
# A dependency is cross-domain when top-level directories differ:
# auth/service.py  → db/repository.py     is_cross_domain = True
# auth/service.py  → auth/validators.py   is_cross_domain = False

def _is_cross_domain(file_a: str, file_b: str) -> bool:
    parts_a = Path(file_a).parts
    parts_b = Path(file_b).parts
    if len(parts_a) < 2 or len(parts_b) < 2:
        return False
    return parts_a[0] != parts_b[0]
```

Cross-domain CALLS edges are the primary signal for **layer violation detection** in Step 1j.

---

## Updating chunk_map with Edge Info

After extraction, back-populate edges onto chunks:

```python
for edge in edges:
    from_chunk = chunk_map.get(edge.from_chunk_id)
    to_chunk   = chunk_map.get(edge.to_chunk_id)
    if from_chunk:
        from_chunk.outgoing_edges.append(edge.to_chunk_id)
    if to_chunk:
        to_chunk.incoming_edges.append(edge.from_chunk_id)
```

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| `from_chunk_id == to_chunk_id` (self-edges) | Guard: skip if from == to |
| Adding edges whose nodes aren't in chunk_map | Check both chunk IDs exist before creating edge |
| IMPORTS edges to third-party packages | Verify path exists in file_manifest first |
| Missing `is_cross_file` flag | Set: `from_file != to_file` |
| Duplicate edges | Deduplicate by `(from_chunk_id, to_chunk_id, edge_type)` before returning |
| Resolving across stale chunk_map | chunk_map must be fully built (Step 1g complete) before extraction |

---

## Validation Checklist

- [ ] All BELONGS_TO edges: `from` = method chunk, `to` = class_head chunk
- [ ] No edge has `from_chunk_id == to_chunk_id`
- [ ] `is_cross_file = True` for all edges where `from_file != to_file`
- [ ] No IMPORTS edges point to third-party packages
- [ ] Duplicate edges removed before returning
- [ ] `outgoing_edges` / `incoming_edges` back-populated on CodeChunk objects
- [ ] `[DependencyExtractor] Extracted {n} edges ({m} unresolved dropped)` logged
