import os

from git import Repo, GitCommandError, Commit


class GitExecutor(object):
    """
      Fetch the repo from git-hub
      of given repo url
    """
    LOCAL_WORKSPACE = './workspace/repos/project'

    def __init__(self, repo_url):
        self.repo_url = repo_url
        self.repo_name = repo_url.split('/')[-1]

    def __add__(self, other):
        return GitExecutor(self.repo_url + other.repo_name)

    def __str__(self):
        return self.repo_name

    def clone_repo(self, to_directory: str = LOCAL_WORKSPACE):
        try:
            if not os.path.exists(to_directory):
                os.makedirs(to_directory)
            Repo.clone_from(self.repo_url, to_directory)
            print(f"Repository cloned to {to_directory}")
        except GitCommandError as e:
            print(f"Error cloning repository from location: {e}")




