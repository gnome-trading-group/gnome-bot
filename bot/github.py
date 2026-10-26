"""The gnome-bot GitHub App, installed on every org repo. The bot reads repo files, docs and code with it, and starts
its own fix workflow; the workflow, not the bot, writes branches and PRs. Installation tokens last an hour."""
import base64
import time

import jwt
import requests

from bot import config
from bot.aws_secrets import secret_json

API = "https://api.github.com"
BOT_REPO = "gnome-bot"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
EXPIRY_MARGIN_SECONDS = 300

_token: str | None = None
_token_expires_at = 0.0


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


def _app_jwt() -> str:
    app = secret_json(config.GITHUB_APP_SECRET)
    now = int(time.time())
    # Backdated a minute for clock drift; GitHub allows at most ten minutes.
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app["client_id"]}, app["private_key"], algorithm="RS256")


def installation_token() -> str:
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at - EXPIRY_MARGIN_SECONDS:
        return _token
    app_auth = {**HEADERS, "Authorization": f"Bearer {_app_jwt()}"}
    installation = requests.get(f"{API}/orgs/{config.GITHUB_ORG}/installation", headers=app_auth, timeout=10)
    installation.raise_for_status()
    response = requests.post(f"{API}/app/installations/{installation.json()['id']}/access_tokens", headers=app_auth,
                             timeout=10)
    response.raise_for_status()
    _token = response.json()["token"]
    _token_expires_at = time.time() + 3600
    return _token


def _get(path: str, params: dict | None = None, accept: str | None = None):
    headers = {**HEADERS, "Authorization": f"token {installation_token()}"}
    if accept:
        headers["Accept"] = accept
    response = requests.get(f"{API}{path}", headers=headers, params=params, timeout=15)
    if response.status_code >= 400:
        raise GitHubError(response.status_code, response.json().get("message", response.text)[:300])
    return response.json()


def dispatch_workflow(workflow: str, inputs: dict[str, str]) -> str:
    """Starts a gnome-bot workflow on main; returns the run's page, or the workflow's runs page if GitHub doesn't say."""
    headers = {**HEADERS, "Authorization": f"token {installation_token()}"}
    response = requests.post(f"{API}/repos/{config.GITHUB_ORG}/{BOT_REPO}/actions/workflows/{workflow}/dispatches",
                             headers=headers, json={"ref": "main", "inputs": inputs, "return_run_details": True},
                             timeout=15)
    if response.status_code >= 400:
        raise GitHubError(response.status_code, response.text[:300])
    details = response.json() if response.content else {}
    return details.get("html_url") or f"https://github.com/{config.GITHUB_ORG}/{BOT_REPO}/actions/workflows/{workflow}"


def _send(method: str, path: str, body: dict) -> dict:
    headers = {**HEADERS, "Authorization": f"token {installation_token()}"}
    response = requests.request(method, f"{API}{path}", headers=headers, json=body, timeout=15)
    if response.status_code >= 400:
        raise GitHubError(response.status_code, response.text[:300])
    return response.json() if response.content else {}


def react_to_comment(repo: str, comment_id: str, review_comment: bool, content: str = "eyes") -> None:
    """A reaction on a PR comment: one in the conversation, or an inline review comment."""
    kind = "pulls" if review_comment else "issues"
    _send("POST", f"/repos/{config.GITHUB_ORG}/{repo}/{kind}/comments/{comment_id}/reactions", {"content": content})


def comment_on_pr(repo: str, pr_number: str, body: str) -> str:
    """Posts a comment in a PR's conversation; returns its id."""
    return str(_send("POST", f"/repos/{config.GITHUB_ORG}/{repo}/issues/{pr_number}/comments", {"body": body})["id"])


def edit_pr_comment(repo: str, comment_id: str, body: str) -> None:
    _send("PATCH", f"/repos/{config.GITHUB_ORG}/{repo}/issues/comments/{comment_id}", {"body": body})


def read_file(repo: str, path: str) -> str:
    """A file's text from the repo's default branch."""
    data = _get(f"/repos/{config.GITHUB_ORG}/{repo}/contents/{path}")
    if isinstance(data, list):
        raise GitHubError(400, f"{path} is a directory: {', '.join(item['name'] for item in data[:50])}")
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")


def list_dir(repo: str, path: str) -> list[str]:
    data = _get(f"/repos/{config.GITHUB_ORG}/{repo}/contents/{path}")
    if not isinstance(data, list):
        raise GitHubError(400, f"{path} is a file")
    return [item["name"] + ("/" if item["type"] == "dir" else "") for item in data]


def search_code(query: str, repo: str | None = None) -> list[dict]:
    scope = f"repo:{config.GITHUB_ORG}/{repo}" if repo else f"org:{config.GITHUB_ORG}"
    data = _get("/search/code", {"q": f"{query} {scope}", "per_page": 20},
                accept="application/vnd.github.text-match+json")
    return [{"repo": item["repository"]["name"], "path": item["path"],
             "matches": [m["fragment"] for m in item.get("text_matches", [])][:2]}
            for item in data.get("items", [])]
