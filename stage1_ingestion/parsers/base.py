"""
Base interface for all language-specific parsers.

To add a new language:
  1. Create ingestion/parsers/{language}_parser.py
  2. Subclass BaseParser and implement parse()
  3. Register it in ingestion/file_parser.py REGISTRY

The FileParser dispatcher remains unchanged — it only calls parse(source, raw_lines).
"""

from abc import ABC, abstractmethod
from typing import List

from core.models import ParsedSymbol


class BaseParser(ABC):
    """
    Abstract base class for all language parsers.

    Each concrete parser is responsible for a single language family.
    It receives the raw source text and line list, and returns a flat
    list of ParsedSymbol objects.

    Parsers must never raise — catch exceptions internally and return
    an empty list so the chunker falls back to sliding window.
    """

    @property
    @abstractmethod
    def language(self) -> str:
        """The canonical language name this parser handles, e.g. "python"."""
        ...

    @abstractmethod
    def parse(self, source: str, raw_lines: List[str]) -> List[ParsedSymbol]:
        """
        Extract ParsedSymbol objects from source text.

        Args:
            source:    Full file content as a single string.
            raw_lines: Source split by newline (1-indexed when used with [i-1]).

        Returns:
            List[ParsedSymbol] — empty list on failure, never None.
        """
        ...
