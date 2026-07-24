"""
Stage 4 — Comment Grouper

Clusters ReviewIssue objects into CommentGroup objects so that nearby
violations on the same file become a single formatted comment rather than
a comment storm.

Grouping rules
──────────────
1. Issues are partitioned by file_path first.
2. Within a file, issues are split into two severity buckets:
     - "high":    CRITICAL or HIGH
     - "medium":  MEDIUM, LOW, or INFO
   A low-severity issue never shares a comment with a CRITICAL one — the
   LLM polish logic treats them very differently and mixed bodies are confusing.
3. Within each bucket, issues are sorted by start_line.  A greedy sweep
   opens a new group whenever the current issue is more than LINE_PROXIMITY
   lines away from the last issue added to the open group.
4. Each CommentGroup knows its anchor line (lowest start_line), span
   (highest end_line), and the dominant severity (highest in the group).

CommentGroup is a plain dataclass — it is the currency passed between
grouper → budget → formatter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List

from core.models import ReviewIssue

# Issues within this many lines of each other are merged into one comment.
LINE_PROXIMITY = 5

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
_HIGH_BUCKET    = {"CRITICAL", "HIGH"}


@dataclass
class CommentGroup:
    """
    A cluster of related ReviewIssue objects destined for a single comment.

    Attributes:
        group_id:   Stable UUID for cache keying in writer.py.
        file_path:  Common file for all issues in this group.
        line:       Anchor line (lowest start_line across issues).
        end_line:   Last line (highest end_line across issues).
        severity:   Dominant (highest) severity string.
        bucket:     "high" | "medium" — drives LLM-polish vs. template path.
        issues:     Ordered list of ReviewIssue objects.
    """
    group_id:  str
    file_path: str
    line:      int
    end_line:  int
    severity:  str
    bucket:    str
    issues:    List[ReviewIssue] = field(default_factory=list)

    @property
    def issue_ids(self) -> List[str]:
        return [i.issue_id for i in self.issues]

    @property
    def language(self) -> str:
        return self.issues[0].language if self.issues else ""


class CommentGrouper:
    """
    Groups a flat list of ReviewIssue objects into CommentGroup objects.

    Usage::

        groups = CommentGrouper().group(issues)
    """

    def group(self, issues: List[ReviewIssue]) -> List[CommentGroup]:
        """
        Partition issues into CommentGroups.

        Returns groups sorted by (file_path, line) for deterministic output.
        """
        # Partition by file → bucket
        by_file_bucket: Dict[tuple, List[ReviewIssue]] = {}
        for issue in issues:
            bucket = "high" if issue.severity in _HIGH_BUCKET else "medium"
            key    = (issue.file_path, bucket)
            by_file_bucket.setdefault(key, []).append(issue)

        groups: List[CommentGroup] = []
        for (file_path, bucket), bucket_issues in by_file_bucket.items():
            bucket_issues.sort(key=lambda i: i.start_line)
            groups.extend(self._sweep(file_path, bucket, bucket_issues))

        groups.sort(key=lambda g: (g.file_path, g.line))
        return groups

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _sweep(
        file_path: str,
        bucket:    str,
        issues:    List[ReviewIssue],
    ) -> List[CommentGroup]:
        """Greedy sweep — O(N).  Opens a new group when gap > LINE_PROXIMITY."""
        if not issues:
            return []

        result:      List[CommentGroup] = []
        open_issues: List[ReviewIssue]  = [issues[0]]
        open_end:    int                = issues[0].end_line

        for issue in issues[1:]:
            if issue.start_line - open_end <= LINE_PROXIMITY:
                open_issues.append(issue)
                open_end = max(open_end, issue.end_line)
            else:
                result.append(_make_group(file_path, bucket, open_issues))
                open_issues = [issue]
                open_end    = issue.end_line

        result.append(_make_group(file_path, bucket, open_issues))
        return result


def _make_group(
    file_path: str,
    bucket:    str,
    issues:    List[ReviewIssue],
) -> CommentGroup:
    dominant = min(issues, key=lambda i: _SEVERITY_ORDER.get(i.severity, 99))
    return CommentGroup(
        group_id  = str(uuid.uuid4()),
        file_path = file_path,
        line      = min(i.start_line for i in issues),
        end_line  = max(i.end_line   for i in issues),
        severity  = dominant.severity,
        bucket    = bucket,
        issues    = list(issues),
    )
