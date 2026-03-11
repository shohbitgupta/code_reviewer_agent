import os
from ingestion.code_file_scanner import CodeFileScanner
from configs.code_file_type_config import LANGUAGE_FILE_TYPE_CONFIG

class RepoScanner(object):
    """
    Scans a locally cloned repository and organises its files
    by programming language based on LANGUAGE_FILE_TYPE_CONFIG.
    """

    def __init__(self, repo):
        self.repo = repo
        self.code_file_scanner = CodeFileScanner()

    def read_all_directory(self, path):
        """
        Recursively walks the given directory and groups all files
        by language based on their extension.

        Args:
            path (str): Root directory path to scan.

        Returns:
            tuple: A tuple of two elements:
                - dict: Keys are language names, values are lists of file paths
                        for extensions present in LANGUAGE_FILE_TYPE_CONFIG.
                - list: File paths whose extensions are not present in LANGUAGE_FILE_TYPE_CONFIG.
        """
        grouped_files = {}
        ungrouped_files = []
        for root, dirs, files in os.walk(path):
            for file_name in files:
                ext = os.path.splitext(file_name)[1]
                file_path = os.path.join(root, file_name)
                if ext in LANGUAGE_FILE_TYPE_CONFIG:
                    language = LANGUAGE_FILE_TYPE_CONFIG[ext]
                    if language not in grouped_files:
                        grouped_files[language] = []
                    grouped_files[language].append(file_path)
                else:
                    ungrouped_files.append(file_path)
        return grouped_files, ungrouped_files

    def read_file(self, file_path):
        """
        Reads and returns the content of the given file.

        Args:
            file_path (str): Path to the file to read.

        Returns:
            str: Full text content of the file.
        """
        with open(file_path, "r") as f:
            return f.read()
