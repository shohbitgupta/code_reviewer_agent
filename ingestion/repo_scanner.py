import os
from ingestion.code_file_scanner import CodeFileScanner
from configs.code_file_type_config import LANGUAGE_FILE_TYPE_CONFIG

class RepoScanner(object):
    def __init__(self, repo):
        self.repo = repo
        self.code_file_scanner = CodeFileScanner()

    def read_all_directory(self, path):
        grouped_files = {}
        for root, dirs, files in os.walk(path):
            for file_name in files:
                ext = os.path.splitext(file_name)[1]
                if ext in LANGUAGE_FILE_TYPE_CONFIG:
                    language = LANGUAGE_FILE_TYPE_CONFIG[ext]
                    file_path = os.path.join(root, file_name)
                    if language not in grouped_files:
                        grouped_files[language] = []
                    grouped_files[language].append(file_path)
        return grouped_files

    def read_file(self, file_path):
        with open(file_path, "r") as f:
            return f.read()
