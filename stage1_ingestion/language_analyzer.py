"""
LanguageAnalyzerOrchestrator — main entry point for all Tier 1 Language
Analyzer passes.

Call order
----------
  Pass 1: LayerClassifier.classify()  — file_path → architectural layer
  Pass 2: Resolver.resolve()          — calls[], bases[], imports[] → resolved
  Pass 3: Language-specific enrich()  — pattern enrichment per language

Usage::

    from stage1_ingestion.symbol_table import ProjectSymbolTable
    from stage1_ingestion.language_analyzer import LanguageAnalyzerOrchestrator

    symbol_table = ProjectSymbolTable()
    symbol_table.build(parsed_files)

    orchestrator    = LanguageAnalyzerOrchestrator(symbol_table=symbol_table)
    analysis_result = orchestrator.analyze(parsed_files)
"""
from __future__ import annotations

import logging
from typing import Dict, List

from stage1_ingestion.analyzers.base import AnalysisResult, LanguageAnalyzer
from stage1_ingestion.analyzers.call_resolver import Resolver
from stage1_ingestion.analyzers.dart_analyzer import DartAnalyzer
from stage1_ingestion.analyzers.kotlin_analyzer import KotlinAnalyzer
from stage1_ingestion.analyzers.layer_classifier import LayerClassifier
from stage1_ingestion.analyzers.rust_analyzer import RustAnalyzer
from stage1_ingestion.analyzers.swift_analyzer import SwiftAnalyzer
from core.models import ParsedFile
from stage1_ingestion.symbol_table import ProjectSymbolTable

logger = logging.getLogger(__name__)


class LanguageAnalyzerOrchestrator:
    """
    Runs all Tier 1 Language Analyzer passes in order:
      Pass 1: LayerClassifier    — file_path → layer
      Pass 2: Resolver.calls     — calls[] → ResolvedCall[]
      Pass 3: Resolver.bases     — bases[] → resolved_bases
      Pass 4: Resolver.imports   — imports[] → resolved_imports
      Pass 5: Language-specific  — pattern enrichment per language
    """

    def __init__(self, symbol_table: ProjectSymbolTable) -> None:
        self.symbol_table = symbol_table
        self._analyzers: Dict[str, LanguageAnalyzer] = {
            "dart":   DartAnalyzer(),
            "kotlin": KotlinAnalyzer(),
            "rust":   RustAnalyzer(),
            "swift":  SwiftAnalyzer(),
        }

    def analyze(self, parsed_files: List[ParsedFile]) -> AnalysisResult:
        """
        Run all passes and return the complete AnalysisResult.

        Parameters
        ----------
        parsed_files:
            All ParsedFile objects for the project, as produced by the
            file parser step.

        Returns
        -------
        AnalysisResult
            Populated with resolved calls, layer assignments, resolved bases,
            resolved imports, external deps, and aggregate counters.
        """
        # ── Pass 1: layer classification ──────────────────────────────────
        logger.info("[LanguageAnalyzer] Pass 1: LayerClassifier")
        classifier = LayerClassifier()
        # layer_map now returns Dict[str, List[str]] (multi-label, Priority 5).
        # The primary label is layer_map[fp][0]; the full list is layer_map[fp].
        layer_map = classifier.classify(parsed_files, self.symbol_table)

        # Propagate primary layer back into the symbol table for graph builder.
        for file_path, layers in layer_map.items():
            primary = layers[0] if layers else "unknown"
            self.symbol_table.set_layer(file_path, primary)

        # Build AnalysisResult shell.
        # layer_map values are List[str]; callers that expect str use [0].
        result = AnalysisResult(
            resolved_calls={},
            layer_map=layer_map,
            resolved_bases={},
            resolved_imports={},
            external_deps={},
        )

        # ── Passes 2–4: call / base / import resolution ───────────────────
        logger.info("[LanguageAnalyzer] Pass 2: Resolver (calls + bases + imports)")
        resolver = Resolver()
        resolver.resolve(parsed_files, self.symbol_table, layer_map, result)

        # ── Pass 5: language-specific enrichment ──────────────────────────
        by_language: Dict[str, List[ParsedFile]] = {}
        for pf in parsed_files:
            lang = pf.file_meta.language
            by_language.setdefault(lang, []).append(pf)

        for lang, files in by_language.items():
            analyzer = self._analyzers.get(lang)
            if analyzer:
                logger.info(
                    "[LanguageAnalyzer] Pass 3 (%s): %s",
                    lang,
                    analyzer.__class__.__name__,
                )
                try:
                    analyzer.enrich(files, self.symbol_table, result)
                except Exception:
                    logger.exception(
                        "[LanguageAnalyzer] Unhandled error in %s analyzer",
                        lang,
                    )

        # ── Summary log ───────────────────────────────────────────────────
        logger.info(
            "[LanguageAnalyzer] layers=%d calls_resolved=%d calls_external=%d "
            "bases_resolved=%d imports_resolved=%d",
            len(layer_map),
            result.total_calls_resolved,
            result.total_calls_external,
            sum(len(v) for v in result.resolved_bases.values()),
            sum(len(v) for v in result.resolved_imports.values()),
        )
        return result
