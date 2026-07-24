# SKILL — Step 1b: Store in Workspace

## Purpose

Create a structured, reproducible workspace directory for each pipeline run.
Every run gets its own isolated `run_id` directory so concurrent runs never
collide and past runs remain inspectable for debugging.

---

## Implemented In

`ingestion/workspace.py` → class `WorkspaceManager`

---

## Input / Output

```python
ws = WorkspaceManager(
    repo_name=state["repo_name"],
    local_repo_path=state["local_repo_path"],
    run_id=state.get("run_id"),          # optional — auto-generated if None
)
layout = ws.setup()

# layout fields:
layout.run_id         # str — "20260312_153042_abc123"
layout.run_dir        # Path — workspace/runs/{run_id}/
layout.raw_dir        # Path — symlink → repos/{repo_name}
layout.parsed_dir     # Path — workspace/runs/{run_id}/parsed/
layout.chunks_dir     # Path — workspace/runs/{run_id}/chunks/
layout.graphs_dir     # Path — workspace/runs/{run_id}/graphs/
layout.reports_dir    # Path — workspace/runs/{run_id}/reports/
```

---

## Directory Structure Created

```
workspace/
  repos/
    {repo_name}/                  ← cloned repo (Step 1a output — already exists)
  runs/
    {run_id}/
      raw/                        ← symlink → ../../repos/{repo_name}
      parsed/                     ← ParsedFile JSON outputs (Step 1f writes here)
      chunks/
        chunks.jsonl              ← one CodeChunk JSON per line (Step 1g writes)
      graphs/
        dependency_graph.json     ← NetworkX serialisation (Step 1j writes)
      reports/                    ← review_report.md + .json (Stage 5 writes)
      run_meta.json               ← run metadata (see below)
```

---

## Run ID Format

```python
import uuid
from datetime import datetime

def _generate_run_id(repo_name: str, commit_sha: str) -> str:
    ts    = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    short = commit_sha[:6]
    slug  = repo_name.replace("/", "_")[:20]
    return f"{ts}__{slug}__{short}"
    # Example: "20260312_153042__owner__repo__a3f9c1"
```

Using the commit SHA in the run ID means re-running on the same commit
produces a new run directory but both are easy to compare.

---

## run_meta.json

Written by WorkspaceManager at setup time. Gives full context for any
future debugging session.

```json
{
  "run_id":         "20260312_153042__owner__repo__a3f9c1",
  "repo_name":      "owner__repo",
  "repo_url":       "https://github.com/owner/repo",
  "commit_sha":     "a3f9c1d8...",
  "started_at":     "2026-03-12T15:30:42Z",
  "pipeline_version": "3.0"
}
```

---

## WorkspaceLayout Schema

```python
@dataclass
class WorkspaceLayout:
    run_id:      str
    run_dir:     Path
    raw_dir:     Path    # symlink to cloned repo
    parsed_dir:  Path
    chunks_dir:  Path
    graphs_dir:  Path
    reports_dir: Path
```

---

## Symlink vs Copy

`raw/` is a **symlink** to `repos/{repo_name}`, not a copy.

- Saves disk space — no duplication of source files
- Keeps `repos/{repo_name}` as the single source of truth
- Multiple runs share the same cloned tree (different run dirs, same raw source)

```python
raw_dir = run_dir / "raw"
if not raw_dir.exists():
    raw_dir.symlink_to(Path(local_repo_path).resolve())
```

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Creating workspace inside the cloned repo | Keep `workspace/` at project root, outside the repo |
| Using wall-clock timestamp only as run_id | Include commit SHA — makes runs traceable |
| Not writing `run_meta.json` | Write immediately at setup — helps debugging failed runs |
| Not creating all dirs up front | Call `mkdir(parents=True, exist_ok=True)` for all dirs |
| Absolute symlink path | Use `Path(local_repo_path).resolve()` to ensure valid absolute path |

---

## Validation Checklist

- [ ] `layout.run_dir.exists()` is True
- [ ] `layout.raw_dir.is_symlink()` is True and resolves to cloned repo
- [ ] `layout.parsed_dir.exists()` is True (empty at this step)
- [ ] `layout.chunks_dir.exists()` is True (empty at this step)
- [ ] `layout.graphs_dir.exists()` is True (empty at this step)
- [ ] `layout.reports_dir.exists()` is True (empty at this step)
- [ ] `(layout.run_dir / "run_meta.json").exists()` is True
- [ ] `[WorkspaceManager] Run {run_id} workspace ready` printed to console
