# SKILL — Step 1f: File Parser

## Purpose

Parse each source file into a `ParsedFile` containing a flat list of `ParsedSymbol`
objects — one per logical code unit (function, class, method, import block).
This is the foundation for semantic chunking in Step 1g.

The parser dispatches to a **language-specific backend**:
- Python → built-in `ast` module (full fidelity, zero dependencies)
- JS / TS / Java → Tree-sitter (preferred) with regex heuristics as fallback

Files marked `is_parseable=False` in Step 1e are skipped — the chunker will apply
sliding window directly.

---

## Implemented In

`ingestion/file_parser.py` → class `FileParser`

---

## Input / Output

```python
parser = FileParser()
parsed_file: ParsedFile = parser.parse(file_meta: FileMeta)
```

### ParsedFile Schema
```python
@dataclass
class ParsedFile:
    file_meta:     FileMeta
    symbols:       List[ParsedSymbol]  # empty list if parse failed
    raw_lines:     List[str]           # ALWAYS populated regardless of parse outcome
    parse_success: bool
    parse_error:   Optional[str]       # error message if parse_success=False
```

### ParsedSymbol Schema
```python
@dataclass
class ParsedSymbol:
    symbol_type:  str            # "function"|"method"|"class_head"|"import"|"block"
    name:         str
    start_line:   int            # 1-indexed
    end_line:     int
    source:       str            # raw source text of this symbol
    parent_name:  Optional[str]  # class name for methods; None for top-level
    decorators:   List[str]      # ["@staticmethod", "@login_required"]
    calls:        List[str]      # function/method names called inside this symbol
    imports:      List[str]      # import paths (import symbols only)
    bases:        List[str]      # base class names (class symbols only)
```

---

## Parser Strategy Per Language

### Python — Built-in `ast` Module

Uses Python's standard library `ast` module. Zero external dependencies.
Handles all valid Python syntax including decorators, async/await, type hints,
walrus operator, dataclasses, and nested functions.

**Extraction targets:**

| AST Node | Symbol Type | Notes |
|---|---|---|
| `ast.ClassDef` | `class_head` | Signature + docstring only; methods extracted separately |
| Methods inside `ClassDef` | `method` | `parent_name` set to class name |
| `ast.FunctionDef` (top-level) | `function` | |
| `ast.AsyncFunctionDef` | `function` | Async treated same as sync |
| `ast.Import` / `ast.ImportFrom` | `import` | Contiguous imports grouped into ONE symbol |
| `ast.Call` inside fn/method | — | Name appended to `calls[]` of parent symbol |

**Two-pass class handling (critical):**
1. Emit one `class_head` symbol — class signature + docstring only, NOT the full body
2. Emit each method as an independent symbol with `parent_name` set to class name

This ensures methods can be retrieved and reviewed independently of their class.

```python
# Call extraction — walk AST for Call nodes:
for node in ast.walk(func_node):
    if isinstance(node, ast.Call):
        name = _get_call_name(node)   # handles obj.method and plain func()
        if name and name not in SKIP_CALLS:
            symbol.calls.append(name)
```

---

### JavaScript / TypeScript — Tree-sitter (Preferred) + Regex Fallback

**Tree-sitter extraction targets:**
- `import_declaration` / `import_statement` → `import`
- `class_declaration` → `class_head`
- `function_declaration` / `arrow_function` with identifier → `function`
- `method_definition` inside class → `method`
- `call_expression` → appended to `calls[]`

**Regex fallback** (when Tree-sitter not installed):
- `import X from 'y'` / `const X = require('y')` → `import`
- `class ClassName [extends Base]` → `class_head`
- `function funcName(` → `function`
- `const funcName = (async)? (...) =>` → `function`
- Block end: brace-depth counter `{` / `}` to find closing brace

> Limitation: regex fallback does not extract `calls[]` reliably.

---

### Java — Tree-sitter (Preferred) + Regex Fallback

**Tree-sitter extraction targets:**
- `import_declaration` → `import`
- `class_declaration` / `interface_declaration` → `class_head`
- `method_declaration` / `constructor_declaration` → `method`
- `method_invocation` → appended to `calls[]`

**Regex fallback:**
- `import com.example.X;` → `import`
- `(public|private|protected)? class Foo` → `class_head`
- `(public|private|protected) (static)? ReturnType methodName(` → `method`
- Guard: `if`, `for`, `while`, `switch` keywords excluded from method detection

---

### Unsupported / Unknown Languages

When `file_meta.language == "unknown"` or no parser exists:
- `parse_success = False`, `symbols = []`
- `raw_lines` still populated
- Chunker applies Layer 3 sliding window

---

## Parser Dispatch

```python
class FileParser:
    PARSERS = {
        "python":     "_parse_python",
        "javascript": "_parse_js_ts",
        "typescript": "_parse_js_ts",
        "java":       "_parse_java",
    }

    def parse(self, file_meta: FileMeta) -> ParsedFile:
        raw_lines = self._read_lines(file_meta.absolute_path)

        if not file_meta.is_parseable:
            return ParsedFile(file_meta, [], raw_lines,
                              parse_success=False,
                              parse_error="marked not parseable in Step 1e")

        parser_method = self.PARSERS.get(file_meta.language)
        if not parser_method:
            return ParsedFile(file_meta, [], raw_lines,
                              parse_success=False,
                              parse_error=f"no parser for language: {file_meta.language}")
        try:
            symbols = getattr(self, parser_method)(
                source="\n".join(raw_lines), raw_lines=raw_lines
            )
            return ParsedFile(file_meta, symbols, raw_lines, parse_success=True)
        except Exception as e:
            return ParsedFile(file_meta, [], raw_lines,
                              parse_success=False, parse_error=str(e))
```

---

## Tree-sitter Upgrade Path

Replacing regex parsers with Tree-sitter requires no downstream changes —
only `_parse_js_ts` and `_parse_java` are swapped out.
The `ParsedSymbol` contract stays identical.

```python
# requirements.txt additions:
# tree-sitter==0.20.4
# tree-sitter-javascript==0.20.1
# tree-sitter-typescript==0.20.2
# tree-sitter-java==0.20.2
```

---

## Symbols / Calls to Skip

```python
# Don't emit symbols for trivial magic methods
SKIP_SYMBOL_NAMES = {
    "__repr__", "__str__", "__eq__", "__hash__",
    "__len__", "__iter__", "__next__",
}

# Don't include stdlib builtins in calls[]
SKIP_CALLS = {
    "print", "len", "range", "enumerate", "zip", "map",
    "filter", "sorted", "list", "dict", "set", "tuple",
    "str", "int", "float", "bool", "type", "isinstance",
    "getattr", "setattr", "hasattr", "super",
}
```

---

## Persistence

Parsed files are written to `workspace/runs/{run_id}/parsed/` so Step 1g
can be re-run without re-parsing:

```python
cache_path = f"workspace/runs/{run_id}/parsed/{file_hash}.json"

if Path(cache_path).exists():
    return ParsedFile.from_dict(json.load(open(cache_path)))  # cache hit

result = parser.parse(file_meta)
json.dump(result.to_dict(), open(cache_path, "w"))            # cache write
```

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Crashing on syntax errors | Wrap `ast.parse()` in try/except → return `parse_success=False` |
| Missing `raw_lines` when parse fails | Read raw lines BEFORE attempting parse |
| Emitting one CLASS symbol with all methods | Two-pass: `class_head` + one symbol per method |
| Using `end_lineno` without version check | `end_lineno` added in Python 3.8 — check at startup |
| Including stdlib builtins in `calls[]` | Filter against `SKIP_CALLS` |
| One `import` symbol per statement | Group all contiguous imports into ONE symbol |

---

## Validation Checklist

- [ ] `ParsedFile.raw_lines` always populated (never empty, even on failure)
- [ ] `ParsedFile.symbols` is `[]` (not `None`) when `parse_success=False`
- [ ] Python files produce `class_head` + separate `method` symbols
- [ ] `ParsedSymbol.calls` populated for Python functions and methods
- [ ] `ParsedSymbol.imports` populated for import symbols
- [ ] `parent_name` set on all method symbols
- [ ] No unhandled exception escapes `FileParser.parse()`
- [ ] Files with `is_parseable=False` return immediately without parsing
- [ ] `[FileParser] Parsed {n} files, {m} symbols extracted` logged
