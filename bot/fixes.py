"""Fix PRs: a triage that names repos offers "Attempt fix", and so does an answer to someone asking for a code change;
a click (or "fix this" in the thread) starts gnome-bot's
`fix.yml` workflow, where one Claude Code session works across those repos on a GitHub runner and opens a draft PR in
each repo it changed, cross-linked. The bot never writes code or pushes; it only starts the run and reports back what
the run says.

The run reports its result to /github/callback, proving it is that run with a GitHub OIDC token (audience
`gnome-bot-callback`), so there is no shared secret to keep in step between AWS and GitHub."""
import json
import logging
import re

import jwt

from bot import config, github, slack
from bot.runtime_config import BotConfig
from bot.store import Store

logger = logging.getLogger(__name__)

FIX_WORKFLOW = "fix.yml"
FIX_ACTION_ID = "bot_fix"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
OIDC_AUDIENCE = "gnome-bot-callback"
FIX_WORKFLOW_REF = f"{config.GITHUB_ORG}/{github.BOT_REPO}/.github/workflows/{FIX_WORKFLOW}@refs/heads/main"
# GitHub caps a dispatch's inputs at 65,535 characters in all; the task is the only long one.
MAX_TASK_CHARS = 30_000
# One session sees every repo a fix touches; more than this is a redesign, not a fix.
MAX_REPOS = 3
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
REPO_LINE = re.compile(r"\n*^Repos?:\s*(.+?)\s*$", re.MULTILINE)
# Slack refuses button labels over 75 characters.
MAX_BUTTON_LABEL = 75
FIX_REQUEST = re.compile(r"^\s*(please\s+)?(attempt\s+a?\s*fix|fix\s+(this|it))\b", re.IGNORECASE)

_jwks: jwt.PyJWKClient | None = None


def split_repo_line(triage_text: str) -> tuple[str, list[str]]:
    """The triage reply without its closing `Repo: a, b` line, and the repos it named in the order they'd ship (empty
    for `none`, or when the line names something that isn't a repo)."""
    matches = list(REPO_LINE.finditer(triage_text))
    if not matches:
        return triage_text, []
    match = matches[-1]
    names = [name.strip().strip("`") for name in match.group(1).split(",")]
    repos = [] if names == ["none"] else list(dict.fromkeys(n for n in names if REPO_NAME.match(n)))[:MAX_REPOS]
    return triage_text[:match.start()].rstrip(), repos


def fix_button(channel: str, thread_ts: str, repos: list[str]) -> dict:
    label = f"Attempt fix ({', '.join(repos)})"
    return {"type": "actions", "elements": [{
        "type": "button", "action_id": FIX_ACTION_ID,
        "text": {"type": "plain_text", "text": label if len(label) <= MAX_BUTTON_LABEL else "Attempt fix"},
        "value": json.dumps({"channel": channel, "thread_ts": thread_ts}),
    }]}


def is_fix_request(text: str) -> bool:
    return bool(FIX_REQUEST.match(slack.MENTION.sub("", text)))


def request_fix(cfg: BotConfig, store: Store, channel: str, thread_ts: str, user_id: str) -> None:
    """Starts a fix for a triage thread, within the caps, and says in the thread what happened."""
    if not cfg.feature_flags.fix_enabled:
        slack.post(channel, thread_ts, "Fixes are switched off (feature flag `fix_enabled`).")
        return
    context = store.fix_context(channel, thread_ts)
    if context is None:
        slack.post(channel, thread_ts, "I don't have a triage to work from in this thread any more; I only keep them "
                                       "for 7 days.")
        return
    if not store.claim_fix_run(channel, thread_ts):
        slack.post(channel, thread_ts, "A fix for this thread is already running; I'll post here when it's done.")
        return
    if not store.claim_fix_slot(cfg.limits.max_fixes_per_day):
        store.release_fix_run(channel, thread_ts)
        slack.post(channel, thread_ts, f"I've already started {cfg.limits.max_fixes_per_day} fixes today, my daily "
                                       f"limit; try again tomorrow (UTC).")
        return

    requested_by = slack.user_email(user_id) or slack.user_name(user_id)
    # From an alert's triage, or from someone asking for a change in Slack and the bot's look into it.
    if "alert" in context:
        task = f"Alert:\n{json.dumps(context['alert'], indent=1)}\n\nTriage:\n{context['triage']}"
    else:
        task = f"Requested in Slack:\n{context['request']}\n\ngnome-bot's look into it:\n{context['triage']}"
    task = task[:MAX_TASK_CHARS]
    try:
        run_url = github.dispatch_workflow(FIX_WORKFLOW, {
            "repos": ",".join(context["repos"]),
            "task": task,
            "channel": channel,
            "thread_ts": thread_ts,
            "requested_by": requested_by,
            "provider": cfg.fix_provider,
            "model": cfg.model_for(cfg.fix_provider, "fix"),
            "callback_url": config.CALLBACK_URL,
        })
    except Exception:
        logger.exception("Couldn't start a fix for %s/%s", channel, thread_ts)
        store.release_fix_run(channel, thread_ts)
        slack.post(channel, thread_ts, "I couldn't start the fix workflow; it's in my logs.")
        return
    where = ", ".join(f"`{repo}`" for repo in context["repos"])
    slack.post(channel, thread_ts, f":hammer_and_wrench: Working on a fix in {where} for <@{user_id}>. "
                                   f"I'll post the draft PRs here (<{run_url}|run>).")
    logger.info("Started a fix in %s for %s/%s by %s", context["repos"], channel, thread_ts, requested_by)


def verify_run_token(token: str, run_id: str) -> dict:
    """The claims of a GitHub OIDC token from gnome-bot's fix workflow on main, for this run; raises otherwise."""
    global _jwks
    _jwks = _jwks or jwt.PyJWKClient(f"{OIDC_ISSUER}/.well-known/jwks")
    key = _jwks.get_signing_key_from_jwt(token)
    claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=OIDC_AUDIENCE, issuer=OIDC_ISSUER)
    if claims.get("job_workflow_ref") != FIX_WORKFLOW_REF:
        raise jwt.InvalidTokenError(f"not the fix workflow on main: {claims.get('job_workflow_ref')}")
    if str(claims.get("run_id")) != str(run_id):
        raise jwt.InvalidTokenError("token is for another run")
    return claims


def report_result(result: dict, store: Store) -> None:
    """Posts what the fix run did in the triage thread it was for, and frees the thread for another fix."""
    channel, thread_ts = result["channel"], result["thread_ts"]
    store.release_fix_run(channel, thread_ts)
    summary = (result.get("summary") or "").strip()
    run = f"<{result['run_url']}|run>"
    prs = result.get("prs") or []
    if result["status"] == "pr_opened" and prs:
        links = "\n".join(f"• `{pr['repo']}`: <{pr['url']}|{pr.get('title') or 'draft PR'}>" for pr in prs)
        order = "" if len(prs) == 1 else "\nMerge in this order; each PR says why."
        text = f":white_check_mark: Draft PR{'s' if len(prs) > 1 else ''} ({run}):\n{links}{order}"
    elif result["status"] == "no_change":
        text = f"I didn't find a change I'd stand behind ({run})."
    else:
        text = f":x: The fix run failed ({run})."
    slack.post(channel, thread_ts, slack.mrkdwn(f"{text}\n{summary}" if summary else text))
