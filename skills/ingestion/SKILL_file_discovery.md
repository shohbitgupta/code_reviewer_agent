# SKILL — Step 1e: File Discovery

## Purpose

Apply skip rules to the full file inventory to produce a filtered `List[FileMeta]`
containing only files that should be parsed and chunked. Uses the `language_map`
from Step 1d to make language-aware skip decisions.

---

## Implemented In

`ingestion/file_filter.py` → class `FileFilter`

---

## Input / Output

```python
ff = FileFilter(
    inventory=raw_inventory,       # from Step 1c
    language_map=language_map,     # from Step 1d
    file_flags=file_flags,         # from Step 1d
    repo_name=state["repo_name"],
)
file_metas: List[FileMeta] = ff.run()
```

---

## FileMeta Schema

```python
@dataclass
class FileMeta:
    file_path:      str        # relative from repo root: "src/auth/login.py"
    absolute_path:  str
    language:       str        # from Step 1d language_map
    extension:      str
    size_bytes:     int
    line_count:     int
    last_modified:  datetime
    is_parseable:   bool       # False → chunker uses Layer 3 sliding window only
    repo_name:      str
```

---

## Filter Rules (applied in order — first match wins)

### Hard Skip — always exclude
```
Directories (never kept — scanner only records files):
  .git/          __pycache__/    node_modules/    .venv/ venv/ env/
  dist/          build/          .idea/           .vscode/
  coverage/      .mypy_cache/    .pytest_cache/   migrations/ (optional)
  Any directory starting with "."

Files (is_vendor=True from Step 1d):
  All files under vendor/ third_party/ external/

Binary files (is_binary=True from Step 1d):
  .png .jpg .jpeg .gif .ico .svg .pdf
  .zip .tar .gz .whl .egg
  .pyc .pyo .class .jar .war .so .dylib .dll .exe

Lock files:
  package-lock.json    yarn.lock    poetry.lock
  Pipfile.lock         composer.lock    Gemfile.lock

Generated files (is_generated=True from Step 1d):
  *_pb2.py    *_pb.go    *.generated.ts    *.auto.ts
```

### Size Thresholds
```
< 10 bytes     → skip (empty or near-empty file)
> 500,000 bytes → keep BUT set is_parseable=False
                  (chunker will use sliding window only)
```

### Minified Files (is_minified=True from Step 1d)
```
Keep BUT set is_parseable=False
Chunker uses sliding window — no AST attempted
```

### Language = "unknown"
```
Keep BUT set is_parseable=False
Chunker uses sliding window
```

---

## is_parseable Decision Table

| Condition | is_parseable | Chunking fallback |
|---|---|---|
| Normal source file, known language | True | AST (Layer 2) |
| File > 500KB | False | Sliding window (Layer 3) |
| Minified file | False | Sliding window (Layer 3) |
| language = "unknown" | False | Sliding window (Layer 3) |
| AST parse fails (set in Step 1f) | False | Sliding window (Layer 3) |

Note: `is_parseable` starts as True/False here in Step 1e, but Step 1f
(AST parsing) may further set it to False if the parse fails.

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Pruning dirs inside `os.walk` | Scanner already walked everything — filter by path prefix |
| Skipping ALL `.json` files | Only skip lock files — `package.json`, `tsconfig.json` should be kept |
| Skipping `migrations/` always | Make this configurable — some teams want migration SQL reviewed |
| Skipping test files | `is_test=True` files should be kept — review with different rules |
| Setting `is_parseable=False` for large files silently | Log a warning with filename and size |

---

## Validation Checklist

- [ ] No `.git/` files in `file_metas`
- [ ] No `node_modules/` files in `file_metas`
- [ ] No binary files (`.png`, `.jar`, `.pyc`) in `file_metas`
- [ ] No lock files (`package-lock.json`, `yarn.lock`) in `file_metas`
- [ ] Files > 500KB present with `is_parseable=False`
- [ ] Minified files present with `is_parseable=False`
- [ ] All `file_meta.file_path` values are relative (no leading `/`)
- [ ] `[FileFilter] {kept} files kept, {skipped} skipped` printed to console
