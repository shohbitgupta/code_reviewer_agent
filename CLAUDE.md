# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Early-stage Python project for an AI-powered code reviewer agent. The agent is designed to fetch GitHub repositories and analyze their code.

## Dependencies

Uses `gitpython` for repository operations. Install dependencies with:

```bash
pip install gitpython
```

## Running the Project

```bash
python main.py
```

## Architecture

- **`main.py`** — Entry point (currently a placeholder; the agent orchestration logic goes here).
- **`tools/`** — Tool modules used by the agent:
  - `tools/repo_fetcher.py` — `GitExecutor` class that clones a GitHub repo by URL to a local directory using `gitpython`.

## Key Design Patterns

- Tools are encapsulated in classes under `tools/` and imported via `tools/__init__.py`.
- `GitExecutor` accepts a `repo_url` and exposes `clone_repo(to_directory)` to clone the repo locally. The repo name is parsed from the trailing path component of the URL.
