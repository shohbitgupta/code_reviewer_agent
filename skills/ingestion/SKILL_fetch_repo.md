# SKILL — Step 1a: Fetch Repo

## Purpose

Clone a remote Git repository to local disk, or pull the latest changes if it
has already been cloned. This is the entry point of the entire ingestion pipeline.

---

## Implemented In

`tools/git_tool.py` → class `GitExecutor`

---

## Input / Output

```python
executor = GitExecutor(repo_url=state["repo_url"])
result = executor.clone_or_pull()

# result fields:
result.local_repo_path   # str — absolute path on disk
result.repo_name         # str — derived from URL slug
result.commit_sha        # str — HEAD commit SHA after clone/pull
result.is_fresh_clone    # bool — True if first clone, False if pulled
```

---

## Class Interface

```python
class GitExecutor:
    LOCAL_WORKSPACE = "./workspace/repos"

    def __init__(self, repo_url: str):
        self.repo_url  = repo_url
        self.repo_name = self._slug_from_url(repo_url)  # "owner__repo"
        self.local_path = f"{self.LOCAL_WORKSPACE}/{self.repo_name}"

    def clone_or_pull(self) -> CloneResult:
        """Clone if not present, git pull if already cloned."""
        if not Path(self.local_path).exists():
            return self._clone()
        return self._pull()

    def _clone(self) -> CloneResult:
        cmd = ["git", "clone", "--depth=1", self.repo_url, self.local_path]
        ...

    def _pull(self) -> CloneResult:
        cmd = ["git", "-C", self.local_path, "pull", "--ff-only"]
        ...

    def _get_commit_sha(self) -> str:
        cmd = ["git", "-C", self.local_path, "rev-parse", "HEAD"]
        ...
```

---

## Clone Strategy

| Scenario | Action | Why |
|---|---|---|
| First run | `git clone --depth=1` | Shallow clone — avoids downloading full history |
| Subsequent runs | `git pull --ff-only` | Incremental — only fetch new commits |
| Auth required (private repo) | Use `GITHUB_TOKEN` in URL | See Auth section below |
| Large monorepo | Add `--filter=blob:none` | Blobless clone — skips large binary objects |

---

## Auth for Private Repos

```python
# Inject GITHUB_TOKEN into URL at clone time:
token = os.getenv("GITHUB_TOKEN")
if token:
    # https://github.com/owner/repo  →  https://TOKEN@github.com/owner/repo
    auth_url = repo_url.replace("https://", f"https://{token}@")

# Never log the auth_url — it contains the token
```

---

## Repo Name Slug

```python
def _slug_from_url(self, url: str) -> str:
    # https://github.com/owner/repo.git → "owner__repo"
    path = url.rstrip("/").rstrip(".git").split("/")
    return f"{path[-2]}__{path[-1]}"
```

---

## CloneResult Schema

```python
@dataclass
class CloneResult:
    local_repo_path: str
    repo_name:       str
    commit_sha:      str
    is_fresh_clone:  bool
    clone_duration:  float   # seconds
```

---

## Error Handling

| Error | Cause | Handling |
|---|---|---|
| `GitCommandError` | Invalid URL, network failure | Raise — caught in ingestion_agent |
| `AuthenticationError` | Private repo, no token | Raise with message: "Set GITHUB_TOKEN env var" |
| `DiskFullError` | No disk space | Raise `OSError` — caught in ingestion_agent |
| `git pull` conflict | Local changes in workspace | Run `git fetch && git reset --hard origin/HEAD` |

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Full clone (`git clone` without `--depth=1`) | Always use `--depth=1` for first clone |
| Logging the auth URL | Mask token in any log output |
| Hardcoding repo path | Use `LOCAL_WORKSPACE / repo_name` — configurable |
| Running git as subprocess without timeout | Set `timeout=120` on `subprocess.run()` |
| Not capturing stderr | Capture stderr for useful error messages on failure |

---

## Validation Checklist

- [ ] `result.local_repo_path` is an existing directory after clone
- [ ] `result.commit_sha` is a 40-char hex string
- [ ] `Path(result.local_repo_path, ".git").exists()` is True
- [ ] Second call to `clone_or_pull()` runs `git pull`, not `git clone`
- [ ] Auth token never appears in log output
- [ ] `[GitExecutor] Cloned {repo_name} @ {sha[:8]}` printed to console
