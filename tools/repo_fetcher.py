import os

from git import Repo, GitCommandError


class GitExecutor(object):
    """
    Fetches a GitHub repository and clones it to a local directory.

    Attributes:
        LOCAL_WORKSPACE (str): Default local path where the repo is cloned.
        repo_url (str): Full GitHub URL of the repository.
        repo_name (str): Repository name parsed from the trailing component of the URL.
    """

    LOCAL_WORKSPACE = './workspace/repos/project'

    def __init__(self, repo_url):
        """
        Args:
            repo_url (str): Full GitHub URL of the repository to fetch.
        """
        self.repo_url = repo_url
        self.repo_name = repo_url.split('/')[-1]

    def __add__(self, other):
        return GitExecutor(self.repo_url + other.repo_name)

    def __str__(self):
        return self.repo_name

    def clone_repo(self, to_directory: str = LOCAL_WORKSPACE):
        """
        Clones the repository to the given local directory.
        Creates the directory if it does not already exist.

        Args:
            to_directory (str): Local path to clone the repository into.
                                Defaults to LOCAL_WORKSPACE.
        """
        try:
            if not os.path.exists(to_directory):
                os.makedirs(to_directory)
            Repo.clone_from(self.repo_url, to_directory)
            print(f"Repository cloned to {to_directory}")
        except GitCommandError as e:
            print(f"Error cloning repository from location: {e}")




