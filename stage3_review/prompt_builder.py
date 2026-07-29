"""
Stage 3 — Prompt Builder

Assembles the system prompt (set once) and per-chunk user prompt.

Design principles:
  - Standards block:  CRITICAL rules first; includes bad/good examples for
                      HIGH+ rules only (keeps prompt tight).
  - Pre-flagged block: explicitly lists mechanical violations so the LLM
                       skips re-detecting them and focuses on judgment calls.
  - Code block:       truncated at MAX_CONTENT_LINES with a notice so the
                      LLM knows it's seeing a window, not the whole function.
  - Context block:    produced by format_context_for_prompt() — only
                      non-empty sections are emitted.
  - Output contract:  brief, explicit description of the tool the LLM must call.

Bundle prompts (build_user_bundle): several small, same-file, baseline-only
chunks reviewed in a single call — see stage3_review/bundler.py. Each chunk is
numbered and self-contained (own header/context/pre-flagged block/code); the
model must tag every finding with chunk_index so it can be attributed back to
the right chunk.
"""

from __future__ import annotations

from typing import List, Optional

from core.models import CodeChunk, RuleViolation

# Maximum source lines sent in a single prompt.  Chunks larger than this are
# trimmed from the bottom with a "[truncated — N lines total]" notice.
MAX_CONTENT_LINES = 150

# ── System prompt (invariant across the run) ──────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior code reviewer embedded in an automated code-review pipeline.

Your job:
  1. Review the code chunk shown against the CODING STANDARDS provided.
  2. Identify real, specific violations — not generic style preferences.
  3. Call the `report_issues` tool with your findings.

Rules for your response:
  - Cite the exact line number where each violation occurs.
  - Use the rule_id from the CODING STANDARDS list (e.g. "SW005", "GEN001").
  - Write the `suggestion` as a concrete code change, not generic advice.
  - Do NOT re-flag violations already listed under PRE-FLAGGED VIOLATIONS.
  - For violations listed under KNOWN ARCHITECTURAL ISSUES, confirm them and
    add file-specific detail — do not invent new ARCH violations not in that list.
  - Set confidence: 1.0 = certain, 0.7 = likely, 0.5 = possible.
    Only report issues with confidence ≥ 0.5.
  - If no violations are found, call report_issues with an empty issues list.
  - Never output free text. Always call report_issues — even for zero findings.\
"""

# Appended to SYSTEM_PROMPT for bundle calls only (see build_user_bundle).
BUNDLE_SYSTEM_ADDENDUM = """\

You are reviewing MULTIPLE small, independent code chunks in this single call, \
numbered CHUNK 0, CHUNK 1, etc. — NOT one continuous file.
  - Every finding MUST set chunk_index to the CHUNK NUMBER (0-based) it belongs to.
  - Line numbers are relative to that chunk's own header, not a shared file offset.
  - Do not report cross-chunk violations — judge each chunk independently.\
"""


class PromptBuilder:
    """
    Builds the per-chunk user prompt from a CodeChunk, its ReviewContext,
    the relevant coding standards, and any pre-flagged violations.
    """

    def build_user(
        self,
        chunk:              CodeChunk,
        context_section:    str,            # output of format_context_for_prompt()
        rules_section:      str,            # output of build_review_prompt_rules()
        pre_flagged:        List[RuleViolation],
    ) -> str:
        """
        Assemble the full per-chunk user prompt: standards, chunk header,
        context, pre-flagged violations, source code, and closing instruction.

        Args:
            chunk:           The chunk being reviewed.
            context_section: Pre-rendered context block from
                             format_context_for_prompt() (may be empty).
            rules_section:   Pre-rendered rules block from
                             build_review_prompt_rules().
            pre_flagged:     Mechanical violations already found for this chunk;
                             listed so the LLM does not re-flag them.

        Returns:
            The complete user-message string to send to the LLM.
        """
        parts: List[str] = []

        # ── 1. Coding standards ───────────────────────────────────────────────
        parts.append(rules_section)

        # ── 2. Chunk header ───────────────────────────────────────────────────
        header_lines = [
            "═" * 60,
            "CHUNK UNDER REVIEW",
            f"File     : {chunk.file_path}",
            f"Lines    : {chunk.start_line}–{chunk.end_line}",
            f"Layer    : {chunk.layer}",
            f"Type     : {chunk.chunk_type.value}",
            f"Symbol   : {chunk.symbol_name}",
            f"Language : {chunk.language}",
        ]
        if chunk.layers and len(chunk.layers) > 1:
            header_lines.append(f"All layers: {', '.join(chunk.layers)}")
        header_lines.append("═" * 60)
        parts.append("\n".join(header_lines))

        # ── 3. Context (parent, deps, similar, arch issues) ───────────────────
        if context_section.strip():
            parts.append(context_section)

        # ── 4. Pre-flagged violations block ───────────────────────────────────
        if pre_flagged:
            pf_lines = [
                "PRE-FLAGGED VIOLATIONS (already detected — do NOT re-flag these):"
            ]
            for v in pre_flagged:
                pf_lines.append(
                    f"  [{v.rule_id}] {v.severity}  line {v.line}: {v.description}"
                )
            parts.append("\n".join(pf_lines))

        # ── 5. Source code (truncated if needed) ──────────────────────────────
        content_lines = chunk.content.splitlines()
        truncated = len(content_lines) > MAX_CONTENT_LINES
        visible   = content_lines[:MAX_CONTENT_LINES]

        code_block = [f"CODE TO REVIEW (```{chunk.language}):"]
        code_block.extend(visible)
        if truncated:
            remaining = len(content_lines) - MAX_CONTENT_LINES
            code_block.append(
                f"... [{remaining} more lines — only the first "
                f"{MAX_CONTENT_LINES} lines are shown]"
            )
        code_block.append("```")
        parts.append("\n".join(code_block))

        # ── 6. Closing instruction ─────────────────────────────────────────────
        parts.append(
            "Review the code above against the standards. "
            "Call report_issues with all violations you find."
        )

        return "\n\n".join(parts)

    def build_user_bundle(
        self,
        chunks:           List[CodeChunk],
        context_sections: List[str],
        rules_section:    str,
    ) -> str:
        """
        Assemble one prompt covering several small, same-file, baseline-only
        chunks (see stage3_review/bundler.py) — one call instead of N.

        Args:
            chunks:           The bundle's member chunks, in order. Every
                             member shares the same file/language/rule set,
                             so rules_section is built once, not per-chunk.
            context_sections: format_context_for_prompt() output per chunk,
                             same order/length as chunks.
            rules_section:    Pre-rendered rules block, shared by the whole
                             bundle (build_review_prompt_rules() with the
                             bundle's baseline categories).

        Returns:
            The complete user-message string to send to the LLM.
        """
        parts: List[str] = [rules_section]

        for i, (chunk, context_section) in enumerate(zip(chunks, context_sections)):
            header_lines = [
                "═" * 60,
                f"CHUNK {i} UNDER REVIEW",
                f"File     : {chunk.file_path}",
                f"Lines    : {chunk.start_line}–{chunk.end_line}",
                f"Layer    : {chunk.layer}",
                f"Type     : {chunk.chunk_type.value}",
                f"Symbol   : {chunk.symbol_name}",
                f"Language : {chunk.language}",
                "═" * 60,
            ]
            parts.append("\n".join(header_lines))

            if context_section.strip():
                parts.append(context_section)

            if chunk.pre_flagged_violations:
                pf_lines = [
                    f"PRE-FLAGGED VIOLATIONS FOR CHUNK {i} (already detected — do NOT re-flag these):"
                ]
                for v in chunk.pre_flagged_violations:
                    pf_lines.append(
                        f"  [{v.rule_id}] {v.severity}  line {v.line}: {v.description}"
                    )
                parts.append("\n".join(pf_lines))

            content_lines = chunk.content.splitlines()
            truncated = len(content_lines) > MAX_CONTENT_LINES
            visible = content_lines[:MAX_CONTENT_LINES]

            code_block = [f"CODE TO REVIEW FOR CHUNK {i} (```{chunk.language}):"]
            code_block.extend(visible)
            if truncated:
                remaining = len(content_lines) - MAX_CONTENT_LINES
                code_block.append(
                    f"... [{remaining} more lines — only the first "
                    f"{MAX_CONTENT_LINES} lines are shown]"
                )
            code_block.append("```")
            parts.append("\n".join(code_block))

        parts.append(
            f"Review all {len(chunks)} chunks above against the standards. "
            "Call report_issues with all violations you find, setting chunk_index "
            "to the 0-based CHUNK NUMBER each finding belongs to."
        )

        return "\n\n".join(parts)
