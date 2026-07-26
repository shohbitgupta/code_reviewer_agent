"""
Stage 5 — Report Builder

Produces three output artefacts from the ReviewState:

  1. report dict   — structured, JSON-serialisable summary
  2. report.json   — written to workspace/reports/<run_id>/report.json
  3. report.html   — standalone HTML report (no external CDN)

Each issue card shows:
  • Severity badge + rule ID + title (header)
  • Code snippet with line numbers and highlighted violation line(s)
  • "What's wrong" — full description from the reviewer
  • "How to fix"   — concrete suggestion

Source code is read directly from local_repo_path (set by Stage 1) so
every issue shows the exact lines that triggered it, with 3 lines of
context above and below.
"""

from __future__ import annotations

import html as _html
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.models import ReviewComment, ReviewIssue

logger = logging.getLogger(__name__)

_REPORT_ROOT = Path("./workspace/reports")
_CTX_LINES   = 3      # context lines above and below the violation
_MAX_LINE_LEN = 120   # truncate very long lines in the snippet

_SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
_SEV_COLOR = {
    "CRITICAL": "#dc2626",
    "HIGH":     "#ea580c",
    "MEDIUM":   "#ca8a04",
    "LOW":      "#2563eb",
    "INFO":     "#6b7280",
}
_SEV_BG = {
    "CRITICAL": "#fef2f2",
    "HIGH":     "#fff7ed",
    "MEDIUM":   "#fefce8",
    "LOW":      "#eff6ff",
    "INFO":     "#f9fafb",
}
_SEV_BORDER = {
    "CRITICAL": "#fca5a5",
    "HIGH":     "#fdba74",
    "MEDIUM":   "#fde047",
    "LOW":      "#93c5fd",
    "INFO":     "#d1d5db",
}
# Highlight row colour inside the dark code block
_SEV_HIGHLIGHT_BG = {
    "CRITICAL": "rgba(220,38,38,.18)",
    "HIGH":     "rgba(234,88,12,.16)",
    "MEDIUM":   "rgba(202,138,4,.14)",
    "LOW":      "rgba(37,99,235,.14)",
    "INFO":     "rgba(107,114,128,.12)",
}


class ReportBuilder:
    """
    Builds the three Stage 5 report artefacts (dict, JSON file, HTML file)
    for a single pipeline run — see module docstring for the full contract.

    Args:
        run_id:    Unique run identifier; used as the output directory name
                   under workspace/reports/.
        repo_name: "owner/repo" string shown in the report header.
        pr_number: PR number shown in the report header (0 = local run).
    """

    def __init__(self, run_id: str, repo_name: str = "", pr_number: int = 0) -> None:
        self._run_id    = run_id
        self._repo_name = repo_name
        self._pr_number = pr_number
        self._out_dir   = _REPORT_ROOT / run_id
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._file_cache: Dict[str, List[str]] = {}   # path → lines

    # ── Public ────────────────────────────────────────────────────────────────

    def build(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build the report dict from *state* and write report.json + report.html
        to workspace/reports/<run_id>/.

        Args:
            state: Shared ReviewState dict (see module docstring for the
                   keys consumed).

        Returns:
            The report dict, with "json_path" and "html_path" keys added
            pointing at the written files.
        """
        issues:   List[ReviewIssue]   = state.get("issues",   [])
        comments: List[ReviewComment] = state.get("comments", [])
        ingestion_stats  = state.get("ingestion_stats",  {})
        review_stats     = state.get("review_stats",     {})
        comment_stats    = state.get("comment_stats",    {})
        ingestion_quality = state.get("ingestion_quality")  # IngestionQualityReport | None

        # Root of the cloned repo — used to read source for snippets
        local_repo_path: str = state.get("local_repo_path", "")

        report = self._build_report_dict(
            issues, comments,
            ingestion_stats, review_stats, comment_stats,
            local_repo_path,
            ingestion_quality=ingestion_quality,
        )

        json_path = self._out_dir / "report.json"
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        html_path = self._out_dir / "report.html"
        html_path.write_text(self._render_html(report), encoding="utf-8")

        report["json_path"] = str(json_path)
        report["html_path"] = str(html_path)
        logger.info("[ReportBuilder] Report written → %s", self._out_dir)
        return report

    # ── Snippet extraction ────────────────────────────────────────────────────

    def _file_lines(self, local_repo_path: str, file_path: str) -> List[str]:
        """Return all lines of a source file (cached).  Empty list on error."""
        key = file_path
        if key in self._file_cache:
            return self._file_cache[key]
        lines: List[str] = []
        if local_repo_path and file_path:
            try:
                p = Path(local_repo_path) / file_path
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                pass
        self._file_cache[key] = lines
        return lines

    def _extract_snippet(
        self,
        local_repo_path: str,
        file_path:       str,
        start_line:      int,
        end_line:        int,
        severity:        str,
    ) -> List[Dict]:
        """
        Return a list of line dicts for rendering the code block.

        Each dict: {line_num, code, is_issue}
        """
        all_lines = self._file_lines(local_repo_path, file_path)
        if not all_lines:
            return []

        # Convert to 0-indexed; clamp
        s = max(0, start_line - 1 - _CTX_LINES)
        e = min(len(all_lines), end_line + _CTX_LINES)

        result = []
        for idx in range(s, e):
            raw  = all_lines[idx].expandtabs(4)
            code = raw[:_MAX_LINE_LEN] + ("…" if len(raw) > _MAX_LINE_LEN else "")
            result.append({
                "line_num": idx + 1,
                "code":     code,
                "is_issue": start_line <= (idx + 1) <= end_line,
            })
        return result

    # ── Report dict ───────────────────────────────────────────────────────────

    def _build_report_dict(
        self,
        issues:            List[ReviewIssue],
        comments:          List[ReviewComment],
        ingestion_stats:   Dict,
        review_stats:      Dict,
        comment_stats:     Dict,
        local_repo_path:   str,
        ingestion_quality: object = None,
    ) -> Dict[str, Any]:
        """
        Aggregate issues, comments, and per-stage stats into the
        JSON-serialisable report dict, attaching a rendered code snippet
        (via _extract_snippet) to every issue and grouping issues by file.
        """
        by_sev      = Counter(i.severity for i in issues)
        by_cat      = Counter(i.category for i in issues)
        rule_counts = Counter(i.rule_id  for i in issues).most_common(10)

        by_file: Dict[str, List[Dict]] = defaultdict(list)
        for i in issues:
            d = i.to_dict()
            d["snippet"] = self._extract_snippet(
                local_repo_path, i.file_path, i.start_line, i.end_line, i.severity
            )
            by_file[i.file_path].append(d)

        files_section = [
            {"path": fp, "issue_count": len(v), "issues": v}
            for fp, v in sorted(by_file.items())
        ]

        return {
            "run_id":       self._run_id,
            "repo_name":    self._repo_name,
            "pr_number":    self._pr_number,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ingestion_quality": (
                ingestion_quality.to_dict() if ingestion_quality is not None else None
            ),
            "summary": {
                "total_issues":      len(issues),
                "by_severity":       dict(by_sev),
                "by_category":       dict(by_cat),
                "top_rules":         [{"rule_id": r, "count": c} for r, c in rule_counts],
                "files_with_issues": len(by_file),
                "ingestion_stats":   ingestion_stats,
                "review_stats":      review_stats,
                "comment_stats":     comment_stats,
            },
            "files":    files_section,
            "comments": [c.to_dict() for c in comments],
        }

    # ── HTML top-level ────────────────────────────────────────────────────────

    def _render_html(self, report: Dict) -> str:
        """
        Render the full standalone HTML report page: header, optional
        Quality Judge card, severity breakdown, run statistics, top
        violated rules, and the per-file issue accordion.
        """
        summary   = report["summary"]
        by_sev    = summary.get("by_severity", {})
        total     = summary.get("total_issues", 0)
        top_rules = summary.get("top_rules", [])
        files     = report.get("files", [])
        i_stats   = summary.get("ingestion_stats", {})
        r_stats   = summary.get("review_stats", {})
        quality   = report.get("ingestion_quality")
        ts        = report.get("generated_at", "")[:19].replace("T", " ") + " UTC"
        pr_badge  = f"PR #{report['pr_number']}" if report['pr_number'] else "Local run"
        repo      = _html.escape(report.get("repo_name") or "—")

        total_color = "#dc2626" if by_sev.get("CRITICAL", 0) else (
                      "#ea580c" if by_sev.get("HIGH", 0)     else "#22c55e")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Code Review — {repo}</title>
<style>
{self._css()}
</style>
</head>
<body>

<!-- ── Header ── -->
<div class="header">
  <div>
    <h1>&#128269; Code Review Report</h1>
    <div class="meta">
      <strong>{repo}</strong>&nbsp;·&nbsp;{pr_badge}
      &nbsp;·&nbsp;Run&nbsp;<code>{report['run_id']}</code>
      &nbsp;·&nbsp;{ts}
    </div>
  </div>
  <div style="text-align:right">
    <div style="font-size:34px;font-weight:800;color:{total_color}">{total}</div>
    <div style="font-size:11px;color:#94a3b8">total issues</div>
  </div>
</div>

<div class="container">

{self._quality_card(quality) if quality else ""}

<!-- ── Severity cards ── -->
<div class="section">
  <div class="section-header"><span class="icon">🎯</span>Severity Breakdown</div>
  <div class="sev-grid">{self._sev_cards(by_sev, total)}</div>
</div>

<!-- ── Stats ── -->
<div class="section">
  <div class="section-header"><span class="icon">📊</span>Run Statistics</div>
  <div class="stats-row">{self._stats_row(i_stats, r_stats)}</div>
</div>

<!-- ── Top rules ── -->
{"" if not top_rules else f'''<div class="section">
  <div class="section-header"><span class="icon">📋</span>Top Violated Rules</div>
  <div class="rules-table">{self._top_rules(top_rules)}</div>
</div>'''}

<!-- ── Issues by file ── -->
<div class="section">
  <div class="section-header">
    <span class="icon">📁</span>Issues by File
    <span class="section-meta">{len(files)} file(s) with findings</span>
  </div>
  {self._files_section(files) if files else
   '<div class="no-issues"><div class="ni-icon">✅</div>No issues found — great code!</div>'}
</div>

</div>
<div class="footer">Generated by <strong>code-reviewer-agent</strong></div>

<script>
// Expand the first file accordion automatically
document.addEventListener('DOMContentLoaded',()=>{{
  const d=document.querySelector('.file-item details');
  if(d) d.open=true;
}});
</script>
</body></html>"""

    # ── CSS ───────────────────────────────────────────────────────────────────

    def _css(self) -> str:
        return """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#f1f5f9;color:#1e293b;font-size:14px;line-height:1.6}
code{font-family:'JetBrains Mono','Fira Code','Cascadia Code',monospace}

/* ── Header ── */
.header{background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);
  color:#f8fafc;padding:28px 40px;display:flex;justify-content:space-between;
  align-items:center}
.header h1{font-size:22px;font-weight:700;letter-spacing:-.3px}
.header .meta{font-size:12px;color:#94a3b8;margin-top:4px}
.header code{font-size:11px;background:#334155;padding:1px 6px;border-radius:4px}

/* ── Layout ── */
.container{max-width:1160px;margin:0 auto;padding:24px 20px}
.section{background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.07);
  margin-bottom:20px;overflow:hidden}
.section-header{padding:14px 20px;border-bottom:1px solid #e2e8f0;font-weight:600;
  font-size:13px;color:#475569;display:flex;align-items:center;gap:8px}
.section-header .icon{font-size:15px}
.section-meta{margin-left:auto;font-size:12px;color:#94a3b8;font-weight:400}

/* ── Severity cards ── */
.sev-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:12px;padding:18px}
.sev-card{border-radius:8px;padding:14px 12px;text-align:center;
  border:1.5px solid transparent;cursor:default;transition:transform .1s}
.sev-card:hover{transform:translateY(-2px)}
.sev-card .num{font-size:30px;font-weight:800;line-height:1}
.sev-card .label{font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.6px;margin-top:4px;opacity:.75}

/* ── Stats ── */
.stats-row{display:flex;flex-wrap:wrap}
.stat{flex:1;min-width:130px;padding:16px 20px;border-right:1px solid #f1f5f9}
.stat:last-child{border:none}
.stat .val{font-size:22px;font-weight:700;color:#0f172a}
.stat .key{font-size:11px;color:#94a3b8;text-transform:uppercase;
  letter-spacing:.4px;margin-top:2px}

/* ── Top rules ── */
.rules-table{padding:4px 20px 16px}
.rule-row{display:flex;align-items:center;gap:12px;padding:9px 0;
  border-bottom:1px solid #f8fafc}
.rule-row:last-child{border:none}
.rule-id{font-family:monospace;font-size:12px;background:#f1f5f9;padding:2px 8px;
  border-radius:4px;min-width:72px;text-align:center;font-weight:600;color:#334155}
.rule-bar{flex:1;background:#e2e8f0;border-radius:99px;height:8px;overflow:hidden}
.rule-fill{height:100%;border-radius:99px;
  background:linear-gradient(90deg,#6366f1,#818cf8)}
.rule-count{font-size:12px;font-weight:600;color:#64748b;min-width:28px;text-align:right}

/* ── File accordion ── */
.file-item{border-bottom:1px solid #f1f5f9}
.file-item:last-child{border:none}
details summary{padding:11px 20px;cursor:pointer;list-style:none;
  display:flex;align-items:center;gap:10px;background:#fafafa;
  user-select:none;transition:background .1s}
details summary::-webkit-details-marker{display:none}
details[open]>summary{background:#eef2ff;border-bottom:1px solid #e0e7ff}
details summary:hover{background:#eef2ff}
.file-path{font-family:monospace;font-size:12px;color:#1e40af;font-weight:600;flex:1}
.file-count{font-size:11px;padding:2px 9px;border-radius:99px;
  background:#e0e7ff;color:#3730a3;font-weight:700}
.chevron{color:#94a3b8;font-size:11px;transition:transform .18s}
details[open] .chevron{transform:rotate(90deg)}

/* ── Issues list ── */
.issues-list{padding:14px 18px;display:flex;flex-direction:column;gap:16px}

/* ── Issue card ── */
.issue-card{border-radius:8px;border:1px solid #e2e8f0;overflow:hidden;
  transition:box-shadow .15s}
.issue-card:hover{box-shadow:0 3px 12px rgba(0,0,0,.09)}

/* card header */
.ic-header{display:flex;align-items:center;gap:8px;padding:10px 14px;
  flex-wrap:wrap}
.sev-badge{font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.5px;padding:3px 9px;border-radius:4px;white-space:nowrap}
.ic-title{font-weight:600;font-size:13px;flex:1;min-width:140px}
.rule-chip{font-family:monospace;font-size:11px;padding:2px 7px;
  border-radius:4px;background:#f1f5f9;color:#475569;font-weight:600}
.cat-chip{font-size:10px;padding:2px 7px;border-radius:4px;
  background:#f0fdf4;color:#166534;font-weight:500;text-transform:uppercase}
.line-chip{font-size:11px;color:#94a3b8;white-space:nowrap}

/* ── Code block ── */
.code-block{background:#0f172a;overflow:hidden}
.cb-header{display:flex;align-items:center;gap:8px;padding:7px 14px;
  background:#1e293b;border-bottom:1px solid #334155}
.cb-file{font-family:monospace;font-size:11px;color:#94a3b8;flex:1}
.cb-lang{font-size:10px;padding:1px 6px;border-radius:3px;
  background:#334155;color:#94a3b8;text-transform:uppercase;letter-spacing:.4px}
.cb-no-src{padding:10px 14px;font-size:11px;color:#475569;
  font-style:italic;background:#0f172a}
pre.code-lines{margin:0;padding:8px 0;overflow-x:auto}
.cl{display:flex;align-items:stretch;min-width:0}
.cl .ln{flex:0 0 44px;text-align:right;padding:1px 10px 1px 0;
  font-family:monospace;font-size:12px;color:#475569;
  user-select:none;border-right:2px solid transparent}
.cl .marker{flex:0 0 16px;text-align:center;font-size:11px;
  color:transparent;padding-top:1px}
.cl .lc{flex:1;padding:1px 14px 1px 8px;font-family:monospace;
  font-size:12px;color:#e2e8f0;white-space:pre}
/* highlighted (issue) line */
.cl.hi .ln{color:#f87171;border-right-color:#ef4444}
.cl.hi .marker{color:#f87171}
.cl.hi .lc{color:#fef2f2}

/* ── Issue sections (why / fix) ── */
.ic-body{display:flex;flex-direction:column;gap:0}
.ic-section{padding:10px 14px;border-top:1px solid #f1f5f9}
.ic-section-label{font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.6px;margin-bottom:5px;display:flex;align-items:center;gap:5px}
.ic-section-body{font-size:13px;color:#374151;line-height:1.65}

/* No issues */
.no-issues{text-align:center;padding:40px;color:#94a3b8}
.ni-icon{font-size:44px;margin-bottom:10px}

/* ── Quality Judge card ── */
.qj-body{display:flex;gap:0;align-items:flex-start}
.qj-score-wrap{flex:0 0 120px;display:flex;flex-direction:column;
  align-items:center;gap:10px;padding:20px 16px;border-right:1px solid #f1f5f9}
.qj-score-ring{width:76px;height:76px;border-radius:50%;border:4px solid;
  display:flex;flex-direction:column;align-items:center;justify-content:center}
.qj-score-num{font-size:24px;font-weight:800;line-height:1}
.qj-score-sub{font-size:10px;color:#94a3b8}
.qj-decision{font-size:11px;font-weight:700;letter-spacing:.5px;padding:4px 12px;
  border-radius:99px;text-transform:uppercase}
.qj-dims{flex:1;padding:8px 0}
.qj-dim-row{display:flex;align-items:center;gap:10px;padding:7px 16px;
  border-bottom:1px solid #f8fafc}
.qj-dim-row:last-child{border:none}
.qj-dim-id{font-family:monospace;font-size:11px;color:#64748b;
  min-width:44px;font-weight:600}
.qj-dim-name{font-size:12px;font-weight:600;min-width:160px;color:#334155}
.qj-bar-wrap{flex:0 0 100px;background:#f1f5f9;border-radius:4px;
  height:14px;overflow:hidden}
.qj-bar{height:100%;border-radius:4px;transition:width .3s}
.qj-dim-msg{flex:1;font-size:11px;color:#64748b}
.qj-status-chip{font-size:10px;font-weight:700;padding:2px 8px;
  border-radius:4px;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}
.qj-fix{padding:10px 16px 14px;font-size:12px;color:#92400e;
  background:#fffbeb;border-top:1px solid #fde68a}

/* Footer */
.footer{text-align:center;color:#94a3b8;font-size:12px;padding:18px}
"""

    # ── Section renderers ─────────────────────────────────────────────────────

    def _quality_card(self, quality: Optional[Dict]) -> str:
        """Render the Ingestion Quality Judge score card as an HTML section."""
        if not quality:
            return ""

        score    = quality.get("overall_score", 0)
        decision = quality.get("decision", "PROCEED")
        dims     = quality.get("dimensions", [])
        top_fix  = _html.escape(quality.get("top_fix_hint", ""))
        lang     = _html.escape(quality.get("primary_language", ""))

        dec_color = {"PROCEED": "#16a34a", "WARN": "#ca8a04", "ABORT": "#dc2626"}.get(decision, "#6b7280")
        dec_bg    = {"PROCEED": "#f0fdf4", "WARN": "#fefce8", "ABORT": "#fef2f2"}.get(decision, "#f9fafb")
        dec_icon  = {"PROCEED": "✓", "WARN": "⚠", "ABORT": "✗"}.get(decision, "")

        # Score ring (simple CSS circle)
        ring_color = dec_color

        dim_rows = []
        for d in dims:
            st = d.get("status", "SKIP")
            st_color = {"PASS": "#16a34a", "WARN": "#ca8a04", "FAIL": "#dc2626", "SKIP": "#94a3b8"}.get(st, "#6b7280")
            st_bg    = {"PASS": "#f0fdf4", "WARN": "#fefce8", "FAIL": "#fef2f2", "SKIP": "#f8fafc"}.get(st, "#f9fafb")
            bar_pct  = d.get("score", 0)
            bar_col  = st_color
            dim_rows.append(
                f'<div class="qj-dim-row">'
                f'<span class="qj-dim-id">{_html.escape(d.get("id",""))}</span>'
                f'<span class="qj-dim-name">{_html.escape(d.get("name",""))}</span>'
                f'<div class="qj-bar-wrap"><div class="qj-bar" style="width:{bar_pct}%;background:{bar_col}30;'
                f'border-left:3px solid {bar_col}"></div></div>'
                f'<span class="qj-dim-msg">{_html.escape(d.get("message",""))}</span>'
                f'<span class="qj-status-chip" style="background:{st_bg};color:{st_color}">{st}</span>'
                f'</div>'
            )

        fix_html = (
            f'<div class="qj-fix">&#128161; <strong>Top fix:</strong> {top_fix}</div>'
            if top_fix and decision != "PROCEED" else ""
        )

        return f"""
<!-- ── Ingestion Quality Judge ── -->
<div class="section">
  <div class="section-header">
    <span class="icon">🔬</span>Ingestion Quality Judge
    <span class="section-meta">Step 1k+2{f" · {lang} thresholds" if lang and lang != "default" else ""}</span>
  </div>
  <div class="qj-body">
    <div class="qj-score-wrap">
      <div class="qj-score-ring" style="border-color:{ring_color}">
        <div class="qj-score-num" style="color:{ring_color}">{score}</div>
        <div class="qj-score-sub">/ 100</div>
      </div>
      <div class="qj-decision" style="background:{dec_bg};color:{dec_color}">
        {dec_icon}&nbsp;{decision}
      </div>
    </div>
    <div class="qj-dims">
      {"".join(dim_rows)}
    </div>
  </div>
  {fix_html}
</div>"""

    def _sev_cards(self, by_sev: Dict, total: int) -> str:
        """Render the severity-count card grid, plus a trailing TOTAL card."""
        parts = []
        for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
            n   = by_sev.get(s, 0)
            col = _SEV_COLOR[s]
            bg  = _SEV_BG[s]
            parts.append(
                f'<div class="sev-card" style="background:{bg};border-color:{col}30">'
                f'<div class="num" style="color:{col}">{n}</div>'
                f'<div class="label" style="color:{col}">{s}</div>'
                f'</div>'
            )
        parts.append(
            f'<div class="sev-card" style="background:#f0fdf4;border-color:#86efac">'
            f'<div class="num" style="color:#16a34a">{total}</div>'
            f'<div class="label" style="color:#16a34a">TOTAL</div>'
            f'</div>'
        )
        return "".join(parts)

    def _top_rules(self, top_rules: List[Dict]) -> str:
        """Render the top violated rules as horizontal bar rows, scaled to the most frequent rule."""
        if not top_rules:
            return ""
        max_count = top_rules[0]["count"] or 1
        rows = []
        for r in top_rules:
            pct = int(100 * r["count"] / max_count)
            rows.append(
                f'<div class="rule-row">'
                f'<span class="rule-id">{_html.escape(r["rule_id"])}</span>'
                f'<div class="rule-bar">'
                f'<div class="rule-fill" style="width:{pct}%"></div></div>'
                f'<span class="rule-count">{r["count"]}</span>'
                f'</div>'
            )
        return "".join(rows)

    def _stats_row(self, i_stats: Dict, r_stats: Dict) -> str:
        """Render the run-statistics strip (files parsed, chunks indexed/reviewed, cache hits, ingestion time)."""
        items = [
            (i_stats.get("files_parseable",    "—"), "Files parsed"),
            (i_stats.get("chunks_total",        "—"), "Chunks indexed"),
            (r_stats.get("chunks_reviewed",     "—"), "Chunks reviewed"),
            (r_stats.get("cache_hits",          "—"), "LLM cache hits"),
            (r_stats.get("pre_flagged",         "—"), "Pre-flagged"),
            (f"{i_stats.get('duration_seconds','—')}s", "Ingestion time"),
        ]
        return "".join(
            f'<div class="stat"><div class="val">{v}</div>'
            f'<div class="key">{k}</div></div>'
            for v, k in items
        )

    def _files_section(self, files: List[Dict]) -> str:
        """Render one collapsible <details> accordion item per file with findings."""
        parts = []
        for f in files:
            issues_html = self._issues_list(f["issues"], f["path"])
            path_esc    = _html.escape(f["path"])
            parts.append(
                f'<div class="file-item"><details>'
                f'<summary>'
                f'<span class="chevron">&#9658;</span>'
                f'<span class="file-path">{path_esc}</span>'
                f'<span class="file-count">{f["issue_count"]}</span>'
                f'</summary>'
                f'<div class="issues-list">{issues_html}</div>'
                f'</details></div>'
            )
        return "".join(parts)

    def _issues_list(self, issues: List[Dict], file_path: str) -> str:
        """Sort a file's issues by severity and render each as an issue card."""
        sorted_issues = sorted(
            issues, key=lambda i: _SEV_ORDER.get(i.get("severity", "INFO"), 99)
        )
        return "".join(self._issue_card(i, file_path) for i in sorted_issues)

    # ── Issue card ────────────────────────────────────────────────────────────

    def _issue_card(self, i: Dict, file_path: str) -> str:
        """
        Render one issue as a card: severity/rule/title header with
        category and layer chips, the code snippet block, and the
        "what's wrong" / "how to fix" sections.
        """
        sev       = i.get("severity",    "INFO")
        rule      = _html.escape(i.get("rule_id",     ""))
        title     = _html.escape(i.get("title",       ""))
        desc      = _html.escape(i.get("description", ""))
        sug       = _html.escape(i.get("suggestion",  ""))
        category  = _html.escape(i.get("category",    ""))
        layer     = _html.escape(i.get("layer",       ""))
        start     = i.get("start_line", 0)
        end       = i.get("end_line",   start)
        lang      = i.get("language",   "")
        snippet   = i.get("snippet",    [])

        col    = _SEV_COLOR.get(sev,  "#6b7280")
        bg     = _SEV_BG.get(sev,    "#f9fafb")
        border = _SEV_BORDER.get(sev, "#e2e8f0")

        line_label = f"line {start}" if start == end else f"lines {start}–{end}"

        # Chips row
        cat_chip  = f'<span class="cat-chip">{category}</span>'  if category else ""
        layer_chip = f'<span class="cat-chip" style="background:#eff6ff;color:#1e40af">{layer}</span>' if layer and layer != "unknown" else ""

        # Code block
        code_block_html = self._code_block(snippet, file_path, lang, sev)

        # Why section
        why_html = ""
        if desc:
            why_html = (
                f'<div class="ic-section">'
                f'<div class="ic-section-label" style="color:{col}">'
                f'<span>❓</span> What\'s wrong'
                f'</div>'
                f'<div class="ic-section-body">{desc}</div>'
                f'</div>'
            )

        # Fix section
        fix_html = ""
        if sug:
            fix_html = (
                f'<div class="ic-section" style="background:#fafffe">'
                f'<div class="ic-section-label" style="color:#16a34a">'
                f'<span>💡</span> How to fix'
                f'</div>'
                f'<div class="ic-section-body" '
                f'style="font-family:\'JetBrains Mono\',monospace;font-size:12px">'
                f'{sug}'
                f'</div>'
                f'</div>'
            )

        return (
            f'<div class="issue-card" '
            f'style="border-left:4px solid {col};border-color:{border};'
            f'border-left-color:{col}">'
            # Header
            f'<div class="ic-header" style="background:{bg}">'
            f'<span class="sev-badge" '
            f'style="background:{col}20;color:{col}">{sev}</span>'
            f'<span class="rule-chip">{rule}</span>'
            f'<span class="ic-title">{title}</span>'
            f'{cat_chip}{layer_chip}'
            f'<span class="line-chip">📍 {line_label}</span>'
            f'</div>'
            # Code block
            f'{code_block_html}'
            # Why + Fix
            f'<div class="ic-body">{why_html}{fix_html}</div>'
            f'</div>'
        )

    def _code_block(
        self,
        snippet:   List[Dict],
        file_path: str,
        lang:      str,
        severity:  str,
    ) -> str:
        """
        Render the dark-themed code snippet block, with the violation
        line(s) highlighted in a severity-tinted background. Renders a
        "source not available" placeholder when *snippet* is empty.
        """
        hl_bg = _SEV_HIGHLIGHT_BG.get(severity, "rgba(107,114,128,.12)")
        file_short = _html.escape(Path(file_path).name if file_path else "")
        lang_esc   = _html.escape(lang.upper() if lang else "")

        if not snippet:
            return (
                f'<div class="code-block">'
                f'<div class="cb-header">'
                f'<span class="cb-file">{_html.escape(file_path)}</span>'
                f'</div>'
                f'<div class="cb-no-src">Source not available for this run</div>'
                f'</div>'
            )

        lang_chip = f'<span class="cb-lang">{lang_esc}</span>' if lang_esc else ""
        header    = (
            f'<div class="cb-header">'
            f'<span class="cb-file">{file_short}</span>'
            f'{lang_chip}'
            f'</div>'
        )

        lines_html = []
        for row in snippet:
            ln   = row["line_num"]
            code = _html.escape(row["code"])
            hi   = row["is_issue"]

            if hi:
                lines_html.append(
                    f'<div class="cl hi" style="background:{hl_bg}">'
                    f'<span class="ln">{ln}</span>'
                    f'<span class="marker">►</span>'
                    f'<span class="lc">{code}</span>'
                    f'</div>'
                )
            else:
                lines_html.append(
                    f'<div class="cl">'
                    f'<span class="ln">{ln}</span>'
                    f'<span class="marker"></span>'
                    f'<span class="lc">{code}</span>'
                    f'</div>'
                )

        return (
            f'<div class="code-block">'
            f'{header}'
            f'<pre class="code-lines">{"".join(lines_html)}</pre>'
            f'</div>'
        )
