"""`@gnome-trading-bot` in a pull request comment, anywhere in the org: an org member asks for a change to the PR or a
question about it, and gnome-bot's `pr-assist.yml` workflow has Claude Code answer on the PR and, if it made changes,
push them to the PR's branch.

The GitHub App's webhook delivers every PR comment (conversation and inline review comments) to /github/webhook,
signed with the App's webhook secret. Only comments that mention it (`TRIGGER`), by an org member (the payload's
`author_association`, so the App needs no members permission), from a person rather than a bot, start anything.

What the person sees, all on the PR: an :eyes: reaction on their comment at once, then one new reply comment with the
answer and the run's link when the run finishes (posted by the run, or by the callback if the run died first), so
GitHub notifies them. A request that won't run gets that reply straight away, saying why."""
import hashlib
import hmac
import logging
import re

from bot import config, github
from bot.aws_secrets import secret_string
from bot.runtime_config import BotConfig
from bot.store import Store

logger = logging.getLogger(__name__)

PR_WORKFLOW = "pr-assist.yml"
# The App's own name (its bot user is gnome-trading-bot[bot]), which GitHub offers in mention autocomplete once the
# App has commented on a PR. `@gnome-bot` also works, though GitHub links it to an unrelated org of that name.
TRIGGER = re.compile(r"(?<![\w-])@(gnome-trading-bot(\[bot\])?|gnome-bot)(?![\w-])", re.IGNORECASE)
ORG_MEMBERS = {"OWNER", "MEMBER"}
# GitHub caps a dispatch's inputs at 65,535 characters in all.
MAX_COMMENT_CHARS = 20_000


def verify_signature(body: str, signature: str) -> bool:
    secret = secret_string(config.GITHUB_WEBHOOK_SECRET)
    expected = "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def request_from(event_type: str, payload: dict) -> dict | None:
    """The PR request a webhook delivery carries, or None when it isn't one gnome-bot acts on."""
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    if event_type == "issue_comment":
        issue = payload.get("issue") or {}
        if not issue.get("pull_request"):
            return None
        number = issue.get("number")
    elif event_type == "pull_request_review_comment":
        number = (payload.get("pull_request") or {}).get("number")
    else:
        return None
    sender = payload.get("sender") or {}
    if sender.get("type") != "User" or comment.get("author_association") not in ORG_MEMBERS:
        return None
    body = comment.get("body") or ""
    if not TRIGGER.search(body):
        return None
    repo = (payload.get("repository") or {}).get("name")
    if not repo or (payload.get("repository") or {}).get("owner", {}).get("login") != config.GITHUB_ORG:
        return None
    return {"kind": "pr_comment", "repo": repo, "pr_number": str(number), "comment": body[:MAX_COMMENT_CHARS],
            "comment_url": comment.get("html_url", ""), "comment_id": str(comment.get("id", "")),
            "review_comment": event_type == "pull_request_review_comment", "requested_by": sender.get("login", "")}


def run_key(repo: str, pr_number: str) -> tuple[str, str]:
    # The PR's in-flight slot shares the fix runs' records; a repo name and PR number can't collide with a Slack thread.
    return f"pr:{repo}", pr_number


def _addressed(request: dict, text: str) -> str:
    return f"@{request['requested_by']}, re [your comment]({request['comment_url']}): {text}"


def _reply(request: dict, text: str) -> None:
    try:
        github.comment_on_pr(request["repo"], request["pr_number"], _addressed(request, text))
    except Exception:
        logger.warning("Couldn't reply on %s#%s", request["repo"], request["pr_number"], exc_info=True)


def handle(cfg: BotConfig, store: Store, request: dict) -> None:
    """Starts pr-assist for a PR comment, within the caps, and says on the PR what happened."""
    repo, number = request["repo"], request["pr_number"]
    if not cfg.feature_flags.fix_enabled:
        _reply(request, "I'm switched off for code changes right now (feature flag `fix_enabled`).")
        return
    if not store.claim_fix_run(*run_key(repo, number)):
        _reply(request, "I'm already working on this PR; I'll reply when that's done, then you can ask again.")
        return
    if not store.claim_fix_slot(cfg.limits.max_fixes_per_day):
        store.release_fix_run(*run_key(repo, number))
        _reply(request, f"I've already done {cfg.limits.max_fixes_per_day} runs today, my daily limit; "
                        f"try again tomorrow (UTC).")
        return

    try:
        github.react_to_comment(repo, request["comment_id"], request.get("review_comment", False))
    except Exception:
        logger.warning("Couldn't react to the comment on %s#%s", repo, number, exc_info=True)
    try:
        run_url = github.dispatch_workflow(PR_WORKFLOW, {
            "repo": repo,
            "pr_number": number,
            "comment": request["comment"],
            "comment_url": request["comment_url"],
            "requested_by": request["requested_by"],
            "provider": cfg.fix_provider,
            "model": cfg.model_for(cfg.fix_provider, "fix"),
            "callback_url": config.CALLBACK_URL,
        })
    except Exception:
        logger.exception("Couldn't start pr-assist for %s#%s", repo, number)
        store.release_fix_run(*run_key(repo, number))
        _reply(request, ":x: I couldn't start the run; it's in gnome-bot's logs.")
        return
    logger.info("Started pr-assist on %s#%s for %s (%s)", repo, number, request["requested_by"], run_url)


def report_result(result: dict, store: Store) -> None:
    """Frees the PR for the next request. The run normally replied with its answer itself; when it died before that,
    the reply saying so is posted here."""
    store.release_fix_run(*run_key(result["repo"], str(result["pr_number"])))
    if result.get("replied"):
        return
    request = {"repo": result["repo"], "pr_number": str(result["pr_number"]),
               "requested_by": result.get("requested_by", ""), "comment_url": result.get("comment_url", "")}
    _reply(request, f":x: The run failed before it could answer ([run]({result['run_url']})).")
