"""The system's knowledge on GitHub: each repo's CLAUDE.md (what it is, how it's built, what it deploys and how to
operate it) and its docs/, plus code search and file reads for questions the docs don't answer."""
import re
import time

from anthropic.lib.tools import ToolError, beta_tool

from bot import github
from bot.tools.common import bounded, text

REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
PATH = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_./ -]{1,300}$")
DOC_PATH = re.compile(r"^(?!.*\.\.)(CLAUDE\.md|docs/[A-Za-z0-9_./-]+\.md)$")
DOC_CACHE_SECONDS = 15 * 60
MAX_FILE_LINES = 400

_doc_cache: dict[tuple[str, str], tuple[float, str]] = {}


def _repo(repo: str) -> str:
    if not REPO.match(repo):
        raise ToolError(f"Not a repo name: {repo!r}")
    return repo


def _github(fn, *args):
    try:
        return fn(*args)
    except github.GitHubError as error:
        raise ToolError(str(error)) from error


@beta_tool
def read_doc(repo: str, path: str = "CLAUDE.md") -> str:
    """A repo's operating notes: its CLAUDE.md (build, conventions, what it deploys, alarms and what to check) or a doc
    under docs/ that the CLAUDE.md links to, such as a runbook. Start here for anything about how a service works.

    Args:
        repo: e.g. gnome-registry, gnome-launcher, gnome-oms.
        path: CLAUDE.md, or docs/....md.
    """
    _repo(repo)
    if not DOC_PATH.match(path):
        raise ToolError("path must be CLAUDE.md or a .md file under docs/ (use read_file for anything else)")
    cached = _doc_cache.get((repo, path))
    if cached and time.time() - cached[0] < DOC_CACHE_SECONDS:
        return text(cached[1])
    content = _github(github.read_file, repo, path)
    _doc_cache[(repo, path)] = (time.time(), content)
    return text(content)


@beta_tool
def search_code(query: str, repo: str | None = None) -> str:
    """GitHub code search across the org's repos (default branches), with matching fragments.

    Args:
        query: GitHub code search terms, e.g. "FetchCycleFailures" or "def send_alert language:python".
        repo: only search this repo.
    """
    if len(query) > 200:
        raise ToolError("query is too long")
    return text(_github(github.search_code, query, _repo(repo) if repo else None))


@beta_tool
def read_file(repo: str, path: str, start_line: int = 1, end_line: int = 200) -> str:
    """Lines of a file from a repo's default branch (at most 400 at a time), or a directory's entries.

    Args:
        repo: the repo name.
        path: the file or directory path within the repo.
        start_line: first line, from 1.
        end_line: last line.
    """
    _repo(repo)
    path = path.strip("/")
    if path and not PATH.match(path):
        raise ToolError(f"Not a repo path: {path!r}")
    try:
        content = github.read_file(repo, path)
    except github.GitHubError as error:
        if "is a directory" in str(error) or not path:
            return text(_github(github.list_dir, repo, path))
        raise ToolError(str(error)) from error
    lines = content.splitlines()
    start = bounded(start_line, 1, max(len(lines), 1), "start_line")
    end = min(max(end_line, start), start + MAX_FILE_LINES - 1, len(lines))
    numbered = "\n".join(f"{n}: {line}" for n, line in enumerate(lines[start - 1:end], start))
    return text(f"{repo}/{path} lines {start}-{end} of {len(lines)}\n{numbered}")


TOOLS = [read_doc, search_code, read_file]
