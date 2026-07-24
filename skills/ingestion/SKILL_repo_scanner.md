# SKILL — Step 1c: Stored Repository Scanner

## Purpose

Walk the cloned repository directory tree and build a complete raw file inventory
before any filtering or language detection. This gives downstream steps a full,
unfiltered picture of the repo structure — including files that will later be skipped.

---

## Implemented In

`ingestion/repo_scanner.py` → class `RepoScanner`

---

## Input / Output

```python
scanner = RepoScanner(
    raw_dir=workspace_layout.raw_dir,   # symlink → cloned repo
    repo_name=state["repo_name"],
)
inventory = scanner.scan()

# inventory fields:
inventory.all_files        # List[RawFileEntry] — every file found
inventory.dir_tree         # Dict[str, List[str]] — dir → child file names
inventory.total_files      # int
inventory.total_size_bytes # int
inventory.structure_hints  # StructureHints — monorepo? sub-packages?
```

---

## RawFileEntry Schema

```python
@dataclass
class RawFileEntry:
    path:          Path     # absolute path
    relative_path: str      # relative to repo root: "src/auth/login.py"
    size_bytes:    int
    last_modified: datetime
    extension:     str      # ".py", ".ts", "" for no extension
    is_dir:        bool     # always False (only files in list)
```

---

## What the Scanner Does

```
workspace/raw/   (the cloned repo)
      │
      ▼
os.walk() — traverse entire directory tree
      │
      ├── Record every file: path, size, mtime, extension
      │
      ├── Build dir_tree: dirname → [file names]  (for structure detection)
      │
      └── Detect StructureHints:
              is_monorepo     → True if multiple top-level package dirs
              sub_packages    → List[str] of sub-package root paths
              has_src_layout  → True if src/ directory present
              root_languages  → Set[str] rough language guess from extensions
```

---

## StructureHints Schema

```python
@dataclass
class StructureHints:
    is_monorepo:    bool
    sub_packages:   List[str]    # ["packages/core", "packages/api"]
    has_src_layout: bool         # src/ layout vs flat layout
    root_languages: Set[str]     # rough set {"python", "typescript"}
```

### Monorepo Detection Heuristics

```python
# Likely monorepo if any of:
# 1. Multiple directories at root each containing their own package.json / pyproject.toml
# 2. A top-level packages/ or apps/ or services/ directory
# 3. A pnpm-workspace.yaml, lerna.json, or nx.json exists at root

MONOREPO_MARKERS = [
    "pnpm-workspace.yaml", "lerna.json", "nx.json",
    "rush.json", "turbo.json",
]

MONOREPO_DIRS = ["packages", "apps", "services", "modules", "libs"]
```

---

## What NOT to Prune Here

The scanner intentionally records **everything** — including files that will later
be filtered by Step 1e (FileFilter). This separation of concerns means:

- Stats (total_files, total_size) reflect the real repo size
- `ingestion_stats["files_skipped"]` is computable as `total_files - parseable_files`
- Debugging is easier — you can see what was filtered and why

Do NOT apply skip rules in the scanner. That is Step 1e's job.

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Skipping hidden files/dirs | Record everything — FileFilter decides what to skip |
| Following symlinks blindly | Set `followlinks=False` in `os.walk()` to avoid infinite loops |
| Using glob instead of os.walk | `os.walk()` is more reliable for large/deep trees |
| Not recording `size_bytes` | Required for size threshold decisions in Step 1e |
| Crashing on permission errors | Wrap file stat in try/except, log and skip unreadable files |

---

## Performance Notes

- For repos > 100K files: `os.scandir()` is faster than `os.walk()` + `os.stat()`
- Avoid `Path.glob("**/*")` — does not handle very deep trees well
- Print progress every 10K files for large repos

---

## Validation Checklist

- [ ] `inventory.all_files` contains no directories (files only)
- [ ] `inventory.total_files` equals `len(inventory.all_files)`
- [ ] `entry.relative_path` for every entry starts with no leading `/`
- [ ] `.git/` entries are present in the raw inventory (not filtered here)
- [ ] `inventory.structure_hints.is_monorepo` is reasonable for the repo
- [ ] `[RepoScanner] Found {total_files} files ({total_size_bytes/1e6:.1f} MB)` printed
