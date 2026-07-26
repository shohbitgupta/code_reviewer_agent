"""
Supplementary extension-to-language map for file types that require
special handling beyond what the primary detector in
`stage1_ingestion/language_detector.py` covers.

`language_detector.py` owns the authoritative extension map for all
supported languages. This file exists only for overrides or additions
that are config-driven rather than code-driven — for example, marking
a particular extension as a first-class citizen in the review pipeline
without touching the detector source.

Usage::

    from configs.code_file_type_config import LANGUAGE_FILE_TYPE_CONFIG
    lang = LANGUAGE_FILE_TYPE_CONFIG.get(".swift")  # "swift"

Adding a new language: prefer extending the extension map in
`stage1_ingestion/language_detector.py` directly. Only use this file
for lightweight, non-code overrides.
"""

LANGUAGE_FILE_TYPE_CONFIG: dict[str, str] = {
    ".swift": "swift",
    ".dart":  "dart",
    ".rs":    "rust",
}
