This map is the only cross-repo picture: what talks to what and where each thing lives. Each repo's own CLAUDE.md
(read_doc) is where the detail is.

## Accounts and stages

- dev (443370708724) and prod (241533121172), both us-east-1. Stack names start with `Dev-` or `Prod-`.
- pipelines (043309336849) runs the CodePipelines: one per repo, deploying Dev, then a manual ApproveProd, then Prod.
  Pipelines: RegistryPipeline, ControllerPipeline, LauncherPipeline, ClassifierPipeline, MarketDataPipeline,
  OrchestratorPipeline, ETLPipelinesPipeline (gnome-pipelines), SharedInfraPipeline, WebsitePipeline, BotPipeline.
- Strategy sessions run on EC2 and may run in other regions (close to their venue).

## Services and their repos

| Repo | What it is | Stacks |
|---|---|---|
| gnome-registry | The source of truth: Postgres (security master, strategies, sessions, ledger, risk, treasury, alerts) behind the Registry API | ApiStack, DatabaseStack, AlertingStack, MonitoringStack, StrategySessionMonitorStack, VenuePollerStack, MarkPollerStack, SettlementSweeperStack, ExchangeSyncStack |
| gnome-controller | The web UI (React) plus its own API: backtests, research, service config, system health, deploy approvals. Owns the Cognito user pool | Controller*Stack |
| gnome-launcher | Decides when to launch and stop strategy sessions (launch rules, triggers, Slack approvals) | LauncherStack, LauncherSlackInteractionStack, LauncherMonitoringStack |
| gnome-classifier | Pulls venue markets (Kalshi, Polymarket, Hyperliquid, ...), normalizes them into the security master, links related contracts, settles and syncs fees. ECS workers: Fetch, Normalize, Embed, Relationships, Notify | ClassifierStack, ClassifierMonitoringStack |
| gnome-orchestrator | The Java process a strategy session runs: market data, strategy, OMS and gateways in one JVM on an EC2 instance | OrchestratorAmiStack, OrchestratorEc2Stack-<region>, OrchestratorEcsStack-<region>, OrchestratorStorageStack |
| gnome-market-data | Market data collection and storage: collectors per region, merger, transformer, gap detector, quality checks, inventory | MarketData*Stack, CollectorRegionalStack-<region> |
| gnome-pipelines | Data pipelines (datasets for research and models) | PipelinesDataStack, PipelinesRunStack |
| gnome-shared-infra | Shared infrastructure: the Slack integration, account-wide monitoring | SharedInfraSlackStack, SharedInfraMonitoringStack |
| gnome-bot | This bot | BotStack, BotMonitoringStack |

Libraries (no stacks): gnome-core, gnome-schemas, gnome-logging, gnome-networking, gnome-sequencer, gnome-gateways
(venue connections, also used by the market data collectors), gnome-oms (orders, positions, risk checks, kill
switches, the ledger writer), gnome-strategies (the strategy framework only: StrategyAgent, graceful exit, the Python
bridge), gnome-backtest, gnomepy (the Python client and backtest bridge), gnome-shared-cdk (shared CDK constructs,
including the API authorizer).

The live strategies themselves are Python classes in gnomepy-research (`gnomepy_research.sessions.<name>.strategy`);
a session's config names one in `strategy.class`.

## How things flow

- Launch: a trigger (classifier notification, schedule, or a person) reaches the launcher; a matching rule makes a
  launch request, which may need Slack approval; the launcher calls the registry's `strategy-sessions/launch`, which
  starts an EC2 instance running the orchestrator. The session heartbeats to the registry and writes fills, orders
  and marks to the ledger.
- PnL is computed from the ledger (fills and marks), per session, strategy or the firm, in `paper` or `live` mode.
- Risk: policies in the registry; the OMS checks them and can halt its own strategy (kill switch).
- Treasury: the venue poller reads each venue account's balances and positions and reconciles them with the ledger.
- Alerts: anything can raise one (gnomepy `send_alert` to the alerting stack's ingest API). The registry's alert
  router posts new alerts to Slack (#gnome-alerts-prod / #gnome-alerts-dev) and incident.io for paging severities.
  Alerts carry a type, severity, status (PENDING, FIRING, RESOLVED) and often a runbook URL.
- Deploys: push to main runs the repo's pipeline; Dev deploys first, Prod waits for ApproveProd.

## Where to look

- A session's logs: `session_logs` (its instance), or log group `/gnome/orchestrator/<region>`.
- A Lambda's logs: `/aws/lambda/<Stage>-<Stack>-<Function>...`; find exact names with `list_log_groups`.
- Classifier worker logs: log groups named `Prod-ClassifierStack-<Worker>Task...`.
- Firing alarms, queues, scheduled jobs, the database, pipelines and the EC2 fleet: `system_health`.
- Why a deploy failed: `recent_deploys`, then `stack_status` on the failed stack.
- Where spend went, who changed what, or any resource's state (instances, queues, buckets, Lambdas): `aws_read`
  (Cost Explorer `GetCostAndUsage` grouped by SERVICE or USAGE_TYPE; CloudTrail `LookupEvents`; `Describe*`/`List*`).

## Glossary

- Session: one run of a strategy (id like `0057e834-...`), with a mode, an instance and a status
  (SUBMITTED, STARTING, RUNNING, STOPPING, STOPPED, FAILED).
- Listing: one instrument on one exchange (a listing id). Security: the instrument itself.
- Mode: `live` trades real money, `paper` is simulated against live market data.
- Scaled integers: prices and money (notional, cost, PnL, fees, balances) are times 1,000,000,000; sizes and
  quantities (net_quantity, fill and order quantities, lot sizes) are times 1,000,000.
