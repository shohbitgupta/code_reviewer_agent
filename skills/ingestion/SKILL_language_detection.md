# SKILL — Step 1d: Language Detection

## Purpose

Assign a language to every file in the raw inventory, group files into language
buckets, and flag special file types (binary, generated, minified, vendor) that
should be handled differently by downstream steps.

---

## Implemented In

`ingestion/language_detector.py` → class `LanguageDetector`

---

## Input / Output

```python
detector = LanguageDetector()
result = detector.detect(inventory: FileInventory)

# result fields:
result.language_map    # Dict[str, str]      — relative_path → language
result.language_stats  # Dict[str, int]      — language → file count
result.file_flags      # Dict[str, FileFlag] — relative_path → flags
```

---

## FileFlag Schema

```python
@dataclass
class FileFlag:
    language:    str       # "python" | "typescript" | ... | "unknown"
    is_binary:   bool
    is_generated:bool      # "DO NOT EDIT" header, .pb.go, _pb2.py, etc.
    is_minified: bool      # single long line > 500 chars, .min.js, .min.css
    is_vendor:   bool      # node_modules, vendor/, third_party/
    is_test:     bool      # *_test.py, *.test.ts, spec/ dirs
    is_config:   bool      # .yaml, .json, .toml, .ini, .env
```

---

## Detection Priority (applied in order)

```
1. Binary check      → read first 8KB, check for null bytes → is_binary=True
2. Vendor check      → path contains: node_modules/ vendor/ third_party/ .venv/
3. Generated check   → header contains "DO NOT EDIT" | "Code generated" |
                        filename matches: *_pb2.py *_pb.go *.generated.ts
4. Minified check    → first non-empty line > 500 chars OR filename *.min.js
5. Extension map     → see Language Map below
6. Content sniffing  → fallback for ambiguous extensions (e.g. .h → C or C++)
```

---

## Language Map (extension → language)

| Extension(s) | Language |
|---|---|
| `.py` | python |
| `.js` `.jsx` `.mjs` `.cjs` | javascript |
| `.ts` `.tsx` | typescript |
| `.java` | java |
| `.go` | go |
| `.rb` | ruby |
| `.rs` | rust |
| `.cs` | csharp |
| `.cpp` `.cc` `.cxx` `.h` `.hpp` | cpp |
| `.c` | c |
| `.kt` `.kts` | kotlin |
| `.swift` | swift |
| `.php` | php |
| `.sh` `.bash` `.zsh` | shell |
| `.sql` | sql |
| `.yaml` `.yml` | yaml |
| `.json` | json |
| `.toml` | toml |
| `.md` `.mdx` | markdown |
| `.html` `.htm` | html |
| `.css` `.scss` `.sass` | css |
| *(unrecognised)* | unknown |

---

## Content Sniffing (fallback for unknown extensions)

```python
SHEBANG_MAP = {
    "python":     ["#!/usr/bin/env python", "#!/usr/bin/python"],
    "javascript": ["#!/usr/bin/env node"],
    "shell":      ["#!/bin/bash", "#!/bin/sh", "#!/usr/bin/env bash"],
    "ruby":       ["#!/usr/bin/env ruby"],
}

def _sniff_language(self, path: Path) -> str:
    """Read first line and check shebang."""
    try:
        first_line = path.read_text(errors="ignore").split("\n")[0]
        for lang, shebangs in SHEBANG_MAP.items():
            if any(first_line.startswith(s) for s in shebangs):
                return lang
    except OSError:
        pass
    return "unknown"
```

---

## Test File Detection

```python
TEST_PATTERNS = [
    "*_test.py", "test_*.py",          # Python pytest
    "*.test.ts", "*.spec.ts",          # TypeScript Jest
    "*.test.js", "*.spec.js",          # JavaScript Jest
    "*Test.java", "*Spec.java",         # Java JUnit
]

TEST_DIRS = ["test/", "tests/", "spec/", "__tests__/", "e2e/"]
```

Test files are still reviewed — `is_test=True` flag is used by the reviewer
to apply different rules (e.g. test naming conventions, no production logic).

---

## Language Stats

```python
# Example output for a full-stack repo:
result.language_stats = {
    "typescript":  142,
    "python":       38,
    "yaml":         21,
    "json":         18,
    "markdown":      9,
    "shell":         4,
    "unknown":       2,
}
```

Passed into `ingestion_stats["language_breakdown"]` in the final run stats.

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Detecting language from extension alone on `.h` files | Content-sniff `.h` — could be C or C++ |
| Flagging all JSON as skippable | Only skip `package-lock.json`, `yarn.lock` etc. in Step 1e |
| Not flagging `.pb.go` as generated | Protobuf generated files cause false positive review issues |
| Treating minified files as parseable | Set `is_minified=True` — chunker must use sliding window only |
| Missing `.mjs`/`.cjs` module formats | Add to javascript extensions — common in modern Node.js |

---

## Validation Checklist

- [ ] Every file in `inventory.all_files` has an entry in `result.language_map`
- [ ] `sum(result.language_stats.values())` equals `inventory.total_files`
- [ ] Binary files have `is_binary=True` (verify with a `.png` or `.jar` file)
- [ ] `node_modules/` files have `is_vendor=True`
- [ ] `*.min.js` files have `is_minified=True`
- [ ] `*_pb2.py` files have `is_generated=True`
- [ ] Language stats printed to console: `[LanguageDetector] python:38 typescript:142 ...`
