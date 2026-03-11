from git import Repo
class CodeFileScanner(object):
    def __init__(self):
        self.repo = Repo()

    def scan(self, path):

