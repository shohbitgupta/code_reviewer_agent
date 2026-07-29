"""
Bare-minimum web UI for the code reviewer pipeline.

One page: paste a GitHub repo URL, click "Review", poll status, view the
generated HTML report when done. No auth, no persistent history — this is a
thin FastAPI wrapper around orchestration.graph.ReviewPipeline (the same
entry point main.py's CLI uses), meant for testing the pipeline from a host
that can actually reach your configured LLM backend (see core/config.py's
MODEL_TYPE=LITELLM — that gateway is only reachable from networks with a
route to it, e.g. over VPN; deploying this app elsewhere does not change
that, since the app's own outbound calls still need that route).

Run locally:
    uvicorn webapp.app:app --reload --port 8000

Deploy: see Dockerfile at the repo root.
"""

from __future__ import annotations

import html
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

app = FastAPI(title="Code Reviewer Agent")

# In-memory run store — no persistence, no auth, single-process only.
# {run_id: {status, repo_url, created_at, error, report_path, summary}}
_RUNS: Dict[str, Dict[str, Any]] = {}
_RUNS_LOCK = threading.Lock()


def _is_github_url(url: str) -> bool:
    return url.strip().lower().startswith("https://github.com/")


def _run_pipeline(run_id: str, repo_url: str) -> None:
    """
    Runs in a background thread — mirrors main.py's _run_full_review(), minus
    CLI-only concerns (PR SHAs, terminal colour output). Any exception here
    is caught and stored on the run record rather than killing the thread
    silently.
    """
    with _RUNS_LOCK:
        _RUNS[run_id]["status"] = "running"

    try:
        from core import config as app_config
        from orchestration.graph import ReviewPipeline
        from orchestration.state import make_state
        from tools.budget_guard import BudgetGuard
        from tools.event_spine import EventSpine
        from tools.llm_client import LLMClientFactory

        event_spine = EventSpine(run_id=run_id)
        budget_guard = BudgetGuard(
            max_review_cost_usd=app_config.MAX_REVIEW_COST_USD,
            daily_budget_usd=app_config.DAILY_BUDGET_USD,
        )
        llm_client = LLMClientFactory.create(budget_guard=budget_guard, event_spine=event_spine)
        logger.info("[Run %s] LLM client: provider=%s model=%s", run_id, llm_client.provider, llm_client.model_name)

        state = make_state(repo_url=repo_url, run_id=run_id)
        pipeline = ReviewPipeline(
            llm_client=llm_client,
            skip_qdrant=True,  # bare-minimum deployment target has no Qdrant instance
            event_spine=event_spine,
        )

        t0 = time.monotonic()
        state = pipeline.run(state)
        elapsed = round(time.monotonic() - t0, 1)

        with _RUNS_LOCK:
            record = _RUNS[run_id]
            if "error" in state:
                record["status"] = "error"
                record["error"] = state["error"]
            else:
                record["status"] = "done"
                record["report_path"] = state.get("report_path", "")
                report = state.get("report", {})
                record["summary"] = report.get("summary", {})
            record["elapsed_seconds"] = elapsed

    except Exception as exc:
        logger.exception("[Run %s] Pipeline crashed", run_id)
        with _RUNS_LOCK:
            _RUNS[run_id]["status"] = "error"
            _RUNS[run_id]["error"] = str(exc)


_PAGE_HEAD = """\
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Code Reviewer Agent</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 640px; margin: 3rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.4rem; }
  input[type=text] { width: 100%; padding: 0.6rem; font-size: 1rem; box-sizing: border-box; }
  button { padding: 0.6rem 1.2rem; font-size: 1rem; margin-top: 0.75rem; cursor: pointer; }
  .status { margin-top: 1.5rem; padding: 1rem; border-radius: 6px; background: #f4f4f4; }
  .status.error { background: #fde8e8; }
  .status.done { background: #e8fdf0; }
  .muted { color: #666; font-size: 0.9rem; }
  a.report-link { display: inline-block; margin-top: 0.5rem; }
</style>
</head>
<body>
<h1>Code Reviewer Agent</h1>
<p class="muted">Paste a GitHub repo URL and run the 5-stage review pipeline.</p>
"""

_PAGE_TAIL = "</body></html>"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE_HEAD + """
<form id="f">
  <input type="text" id="repo_url" name="repo_url" placeholder="https://github.com/owner/repo" required>
  <button type="submit">Review</button>
</form>
<div id="out"></div>
<script>
const f = document.getElementById('f');
const out = document.getElementById('out');
f.addEventListener('submit', async (e) => {
  e.preventDefault();
  const repo_url = document.getElementById('repo_url').value;
  out.innerHTML = '<div class="status">Starting…</div>';
  const resp = await fetch('/review', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({repo_url}),
  });
  if (!resp.ok) { out.innerHTML = '<div class="status error">' + await resp.text() + '</div>'; return; }
  const {run_id} = await resp.json();
  poll(run_id);
});
async function poll(run_id) {
  const resp = await fetch('/api/status/' + run_id);
  const data = await resp.json();
  render(data, run_id);
  if (data.status === 'queued' || data.status === 'running') {
    setTimeout(() => poll(run_id), 2000);
  }
}
function render(data, run_id) {
  let cls = data.status === 'error' ? 'error' : (data.status === 'done' ? 'done' : '');
  let body = '<b>Status:</b> ' + data.status;
  if (data.status === 'error') body += '<br><b>Error:</b> ' + data.error;
  if (data.status === 'done') {
    body += '<br><b>Elapsed:</b> ' + data.elapsed_seconds + 's';
    if (data.summary) body += '<br><b>Issues found:</b> ' + (data.summary.total_issues ?? '—');
    body += '<br><a class="report-link" href="/report/' + run_id + '" target="_blank">Open HTML report →</a>';
  }
  out.innerHTML = '<div class="status ' + cls + '">' + body + '</div>';
}
</script>
""" + _PAGE_TAIL


@app.post("/review")
async def review(payload: Dict[str, str]):
    repo_url = (payload.get("repo_url") or "").strip()
    if not _is_github_url(repo_url):
        return JSONResponse({"error": "repo_url must be a https://github.com/... URL"}, status_code=400)

    run_id = str(uuid.uuid4())[:8]
    with _RUNS_LOCK:
        _RUNS[run_id] = {
            "status": "queued",
            "repo_url": html.escape(repo_url),
            "created_at": time.time(),
        }

    thread = threading.Thread(target=_run_pipeline, args=(run_id, repo_url), daemon=True)
    thread.start()

    return {"run_id": run_id}


@app.get("/api/status/{run_id}")
def status(run_id: str):
    with _RUNS_LOCK:
        record = _RUNS.get(run_id)
    if record is None:
        return JSONResponse({"error": "unknown run_id"}, status_code=404)
    return record


@app.get("/report/{run_id}")
def report(run_id: str):
    with _RUNS_LOCK:
        record = _RUNS.get(run_id)
    if record is None:
        return JSONResponse({"error": "unknown run_id"}, status_code=404)
    report_path = record.get("report_path")
    if not report_path or not os.path.isfile(report_path):
        return JSONResponse({"error": "report not ready or missing"}, status_code=404)
    return FileResponse(report_path, media_type="text/html")
