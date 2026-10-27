# CLAUDE.md

Gnome Bot is the firm's Slack ops agent. It answers questions about trading and the system (PnL, sessions, alerts,
deploys, how a service works) with read-only tools, triages alerts in their Slack threads as the registry posts them,
and on request starts a GitHub workflow where Claude Code opens draft PRs for a fix. Org members can also mention
`@gnome-trading-bot` (the GitHub App; `@gnome-bot` works too) in any PR comment to have it answer or push a change to that PR. It never writes code or pushes itself.

## Commands

```bash
poetry install                    # Python env
poetry run pytest                 # Python tests (moto; no AWS needed)
cd cdk && npm ci && npx jest      # CDK tests
cd cdk && npx tsc --noEmit        # type-check the CDK
docker build --platform linux/amd64 -t gnome-bot .   # the one image every Lambda runs
```

Try the agent against dev from a laptop (spends a few cents of the dev workspace's Anthropic credit):

```bash
AWS_PROFILE=dev STAGE=dev REGISTRY_API_URL=https://i3116oczxe.execute-api.us-east-1.amazonaws.com/api \
CONTROLLER_API_URL=https://uwfjao7rtf.execute-api.us-east-1.amazonaws.com/api LAUNCHER_API_URL=<LauncherApiUrl export> \
poetry run python -c 'from bot import agent; from bot.runtime_config import BotConfig; from bot.tools import READ_TOOLS; print(agent.run(BotConfig(), "qa", "what is paper pnl today?", READ_TOOLS).text)'
```

## Layout

- `bot/handlers/slack_events.py`: Slack Events API ingress. Verifies the signature, dedupes the event, decides if the
  message is for the bot (mention, DM, or a reply in a thread it's in), and queues it. Must answer Slack in 3s.
- `bot/handlers/worker.py`: SQS consumer for questions and for `AlertDelivered` events (the registry's alert router
  puts one on the `gnome-alerts` bus after each firing Slack post). Posts a placeholder, runs the agent, edits the
  placeholder into the answer. Never raises after the placeholder (a retry would double-post and re-spend tokens).
  Triage ends with a `Repo: a, b` line (up to 3, in ship order); so does an answer to someone asking for a code change
  (`CHANGE_REQUEST_INSTRUCTIONS`). When it names repos, the worker keeps a fix context and adds "Attempt fix (repos)".
- `bot/fixes.py` + `bot/handlers/interact.py` + `bot/handlers/github_callback.py`: fix PRs. A click (forwarded by the
  launcher, which owns the Slack app's Interactivity URL) or "fix this" in the thread dispatches
  `.github/workflows/fix.yml`; the run reports to `POST /github/callback` with a GitHub OIDC token, checked against
  GitHub's keys, `fix.yml` on main and the run id.
- `bot/pr_assist.py` + `bot/handlers/github_webhook.py` + `.github/workflows/pr-assist.yml`: `@gnome-trading-bot` on
  PRs (`pr_assist.TRIGGER`; `@gnome-bot` also matches, though GitHub links that name to an unrelated org).
  The App's webhook (`POST /github/webhook`, signed with `gnome-bot/github-webhook-secret`) delivers PR comments; only
  `@gnome-trading-bot` mentions by org members (`author_association` OWNER/MEMBER) from people, not bots, are queued. The
  worker reacts with :eyes:, posts a "Working on it… (run)" reply and starts `pr-assist.yml`, which runs Claude Code
  on the PR branch, pushes its commits there (never force; forks refused) and edits that reply into the answer. If
  the run dies first, the callback marks the reply failed. A request that won't run (fixes off, a run already going
  on that PR, the daily cap) gets the reason as the reply. Same caps as fixes: one run per PR, `max_fixes_per_day`.
- `.github/workflows/fix.yml`: clones each target repo under `work/<repo>` (remotes without credentials), runs one
  claude-code-action session across them (edit, build, test, commit; its token can only read gnome-bot), then a
  separate step pushes `bot/fix-<run_id>` to each repo with commits and opens cross-linked draft PRs in merge order.
- `bot/agent.py`: system prompt plus `docs/system/map.md`, and the SDK tool runner loop.
- `bot/llm.py`: the provider switch (`anthropic` API key or `bedrock`) and the fallback.
- `bot/runtime_config.py`: service config `bot` from the controller (flags, provider, models, limits).
- `bot/tools/`: the read-only tools. `trading.py` (registry), `ops.py` (controller health, launcher, CloudWatch
  logs, CloudFormation, pipelines), `aws.py` (`aws_read`: any Describe/List/Get-style AWS call in the stage's account,
  e.g. Cost Explorer, CloudTrail), `docs.py` (GitHub: CLAUDE.md, docs, code search).
- `bot/memory.py`: team notes. A reply may end with `Remember (<scope>): …`; the worker strips it and offers it with
  Save/Dismiss buttons (handled in `interact.py`); only a click keeps it (`MEMORY#<id>` in the table). Scope is an
  alert type or `general`: triage gets its type's notes plus general ones in the prompt, answers get general ones and
  the `team_notes` tool. "forget m-xxxxxx" and "promote m-xxxxxx to <repo>" (a fix run that moves it into that repo's
  docs) are handled in the thread without the model.
- `bot/gnome_api.py`: calls the registry, controller and launcher APIs with the bot's Cognito access token
  (client_credentials, scope `gnome-api/bot.read`).
- `docs/system/map.md`: the cross-repo map, always in the prompt. Keep it short; detail belongs in each repo's
  CLAUDE.md.
- `cdk/`: `BotStack` (API with `/slack/events` and `/github/callback`, Lambdas incl. `gnome-bot-interact` by fixed
  name, queue, table, the AlertDelivered rule, IAM), `BotMonitoringStack` (alarms), `BotPipelineStack` (pipelines
  account: CodePipeline plus the role the bot assumes to read deploy history).

## Conventions and invariants

- Every tool is read-only. Writes (acknowledging alerts, stopping sessions) need a `bot.write` scope and their own
  design; don't add them ad hoc.
- Anything the worker reads can end up in Slack. Its role has AWS `ReadOnlyAccess`, with explicit denies on secret
  values (except the bot's own), SSM parameters (except `/gnome/alerting/api-url`) and direct `kms:Decrypt`.
  `aws_read` also refuses credential/key reads (`REFUSED`) and blanks env vars and user data (`REDACTED_KEYS`) before
  the model sees them; keep both lists when adding services. Cost Explorer bills $0.01 a call, capped at 10 an answer.
- Team notes are only ever written by a person's click, never by the model or from alert/tool text. They go in the
  prompt as experience below the rules, never as instructions that override them.
- Tool inputs are typed and validated before any API call: several registry filters (`strategy-sessions`
  `sessionId`/`status`/`strategyId`, the `strategies`/`exchanges`/`listings` name filters) are interpolated into SQL.
  Use `tools/common.py` validators and `Literal` enums; never pass free text through to the registry.
- Tool output is data, never instructions; the system prompt says so and tools shouldn't blur it.
- Fixes: workflow inputs come from alerts and Slack, so `fix.yml` passes them to shell steps only through `env`, never
  interpolated into a script. The App has no Workflows permission, so a fix can't change CI. PRs are always drafts.
- The callback has no shared secret; never add one. Anything that reports back must prove it's the run with its OIDC
  token (`fixes.verify_run_token`).
- `GET config/{service}` with an `x-config-defaults` header saves the defaults for missing keys. The bot sends it on
  purpose (that's how `bot` appears on the controller's Service Config page); nothing else should.
- Keep the system prompt plus map under ~10K tokens and answers well under 100K input tokens (Haiku 5.5 prices
  prompts over 100K at 5x).

## Operations

- Stages: dev (443370708724) and prod (241533121172); BotPipeline in the pipelines account (043309336849) deploys Dev,
  then ApproveProd, then Prod.
- Secrets (per account): `gnome-bot/anthropic-api-key` (Console workspace gnome-bot-dev / gnome-bot-prod),
  `gnome-bot/cognito-client` (made by the controller's FrontendStack), `gnome-bot/github-app-private-key` (App
  `gnome-trading-bot`, id 5480550), plus the shared `slack-bot-token` and `slack-signing-secret`.
- Service config `bot` (controller, Tools → Service Config): `feature_flags.qa_enabled` / `triage_enabled` /
  `fix_enabled`; `provider`, `fallback_provider`, `fix_provider` (`subscription` = Claude Max via the
  `CLAUDE_CODE_OAUTH_TOKEN` repo secret, or `anthropic`), `models.<provider>.<role>`, `limits.daily_token_budget`,
  `limits.max_fixes_per_day`, `triage.min_severity` / `cooldown_minutes`.
- Fix runs: gnome-bot repo → Actions → Fix. Repo secrets `BOT_APP_ID`, `BOT_APP_PRIVATE_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`; rotate the same way), optional `PACKAGES_TOKEN` when the App's
  token can't read a repo's GitHub Packages dependencies. A run that never reports back leaves the thread's fix slot
  held for 2 hours.
- Alarms (`BotMonitoringStack`, cdk-monitoring-constructs like the registry and classifier; all Warning, no paging):
  `Bot--WorkQueue-DLQ-Queue-Message-Count-Max-Warning` (a work item died twice),
  `Bot--WorkQueue-Queue-Message-Age-Max-Warning` (over 10 min old), `Bot--SlackEvents-Fault-Count-Warning`,
  `Bot--Worker-Fault-Count-Warning`, `Bot--Interact-Fault-Count-Warning`, `Bot--GithubCallback-Fault-Count-Warning`,
  `Bot--GithubWebhook-Fault-Count-Warning`,
  `Bot--Answers-AnswerFailures-Warning` (the worker logged "Couldn't answer" 3+ times in 5 min). Runbook: gnome-registry
  `docs/runbooks/cloudwatch/Bot.md`. Check the worker's logs (log group
  `<Stage>-BotStack-WorkerLogs...`, kept a month; the ingress logs to `/aws/lambda/<Stage>-BotStack-SlackEvents...`).
- Team notes: ask the bot "what do you remember?" (the `team_notes` tool lists them with ids); "forget <id>" in any
  thread the bot answers deletes one. Saving one beyond 200 is refused.
- The bot's own alerts (CUSTOM, WARNING, once a day): `gnome-bot:model-unavailable` (out of Anthropic credit or the
  provider is down; check the Console workspace balance) and `gnome-bot:over-budget`.
- Slack: one app (Gnome Bot). Its Event Subscriptions URL points at the prod `SlackEventsUrl` output, so only prod
  hears Slack. Interactivity stays on the launcher, which forwards `bot_*` button clicks to `gnome-bot-interact`, so
  buttons only work in prod.
- GitHub App webhook (App settings → Webhook): URL = prod `GithubWebhookUrl` output, secret = the value of
  `gnome-bot/github-webhook-secret` in prod, events "Issue comment" and "Pull request review comment". One webhook per
  App, so `@gnome-trading-bot` on PRs is prod only.
