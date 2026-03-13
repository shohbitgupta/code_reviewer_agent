# ingestion/parsers package — one module per supported language
from ingestion.parsers.python_parser import PythonParser
from ingestion.parsers.swift_parser import SwiftParser
from ingestion.parsers.rust_parser import RustParser
from ingestion.parsers.dart_parser import DartParser

__all__ = ["PythonParser", "SwiftParser", "RustParser", "DartParser"]
