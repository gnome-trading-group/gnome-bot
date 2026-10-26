"""Read-only views of the running system: the controller's health page, the launcher, CloudWatch logs,
CloudFormation stacks, and deploy pipelines (in the pipelines account, through a read-only role)."""
import re
import time
from typing import Literal

import boto3
from anthropic.lib.tools import ToolError, beta_tool

from bot import config
from bot.tools.common import bounded, call, text

# Only the system's own logs: Lambdas, strategy sessions and the classifier's workers.
LOG_GROUP = re.compile(r"^(/aws/lambda/|/gnome/|Prod-|Dev-)[A-Za-z0-9_./#-]{1,400}$")
STACK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
REGIONS = Literal["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-west-2", "eu-central-1",
                  "ap-northeast-1", "ap-northeast-2", "ap-southeast-1", "ap-southeast-2", "ap-south-1", "sa-east-1"]
LaunchStatus = Literal["PENDING_APPROVAL", "APPROVED", "REJECTED", "SCHEDULED", "CANCELLED", "LAUNCHING", "LAUNCHED",
                       "FAILED", "PENDING_SHUTDOWN_APPROVAL", "SHUTDOWN_APPROVED", "SHUTDOWN_REJECTED", "STOPPING",
                       "STOPPED", "STOP_FAILED", "EXPIRED"]
RuleType = Literal["classifier_event", "cs2", "direct_launch"]
MAX_LOG_EVENTS = 200


@beta_tool
def system_health(section: Literal["alarms", "jobs", "queues", "database", "pipelines", "fleet"]) -> str:
    """The controller's System Health page, one section at a time, across every region: CloudWatch alarms by service,
    scheduled jobs with their last run and errors, SQS queues with their oldest message, the database, deploy
    pipelines, or the EC2 fleet (unhealthy first).

    Args:
        section: which part of the page.
    """
    health = call("controller", "system/health", {"section": section})
    if section == "alarms":
        health = _alarms_not_ok(health)
    return text(health)


def _alarms_not_ok(health: dict) -> dict:
    """Most of the page is alarms in OK, which would crowd out the ones that matter: keep the rest and count the OK."""
    services = []
    for service in health.get("services") or []:
        alarms = service.get("alarms") or []
        not_ok = [a for a in alarms if a.get("state") != "OK"]
        services.append({**service, "alarms": not_ok, "okCount": len(alarms) - len(not_ok)})
    return {**health, "services": services}


@beta_tool
def launch_requests(status: LaunchStatus | None = None, rule_type: RuleType | None = None, limit: int = 20) -> str:
    """The launcher's launch and shutdown requests, newest first: what triggered them, the rule, status, session and
    any launch error.

    Args:
        status: only this status.
        rule_type: only this kind of rule.
        limit: 1 to 100.
    """
    return text(call("launcher", "launch-requests", {
        "status": status, "rule_type": rule_type, "limit": bounded(limit, 1, 100, "limit")}))


@beta_tool
def launch_rules() -> str:
    """Every launch rule (archived included): what it launches, when, its approval path and limits."""
    return text(call("launcher", "launch-rules"))


@beta_tool
def recent_triggers(rule_type: RuleType | None = None, limit: int = 30) -> str:
    """Triggers the launcher received, newest first, with each rule's outcome and why it did or didn't launch.

    Args:
        rule_type: only this kind of rule.
        limit: 1 to 100.
    """
    return text(call("launcher", "triggers", {"rule_type": rule_type, "limit": bounded(limit, 1, 100, "limit")}))


@beta_tool
def search_logs(log_group: str, filter_pattern: str | None = None, minutes: int = 60,
                region: REGIONS = "us-east-1") -> str:
    """Recent CloudWatch log events from one log group, newest last, at most 200.

    Args:
        log_group: the full log group name, e.g. /aws/lambda/Prod-ApiStack-... (find names with list_log_groups).
        filter_pattern: a CloudWatch Logs filter pattern, e.g. "ERROR" or "?Traceback ?Exception".
        minutes: how far back, 1 to 1440.
        region: where the log group lives (strategy sessions may run outside us-east-1).
    """
    if not LOG_GROUP.match(log_group):
        raise ToolError("log_group must be one of the system's log groups (/aws/lambda/, /gnome/, Prod-, Dev-)")
    logs = boto3.client("logs", region_name=region)
    params = {"logGroupName": log_group, "startTime": int((time.time() - bounded(minutes, 1, 1440, "minutes") * 60) * 1000),
              "limit": MAX_LOG_EVENTS}
    if filter_pattern:
        params["filterPattern"] = filter_pattern
    try:
        events = logs.filter_log_events(**params)["events"]
    except logs.exceptions.ResourceNotFoundException as error:
        raise ToolError(f"No log group {log_group} in {region}") from error
    return text([{"t": e["timestamp"], "stream": e["logStreamName"], "message": e["message"].rstrip()}
                 for e in events[-MAX_LOG_EVENTS:]])


@beta_tool
def list_log_groups(prefix: str, region: REGIONS = "us-east-1") -> str:
    """Log group names starting with `prefix`, to find the one search_logs needs.

    Args:
        prefix: e.g. /aws/lambda/Prod-ApiStack, /gnome/orchestrator, Prod-ClassifierStack.
        region: which region to list.
    """
    if not LOG_GROUP.match(prefix + "x"):
        raise ToolError("prefix must start with /aws/lambda/, /gnome/, Prod- or Dev-")
    groups = boto3.client("logs", region_name=region).describe_log_groups(logGroupNamePrefix=prefix, limit=50)
    return text([g["logGroupName"] for g in groups["logGroups"]])


@beta_tool
def stack_status(stack_name: str) -> str:
    """A CloudFormation stack's status and its most recent failed events with their reasons, for why a deploy broke.

    Args:
        stack_name: e.g. Prod-ApiStack, Dev-ControllerFrontendStack.
    """
    if not STACK_NAME.match(stack_name):
        raise ToolError(f"Not a stack name: {stack_name!r}")
    cfn = boto3.client("cloudformation", region_name=config.REGION)
    try:
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    except cfn.exceptions.ClientError as error:
        raise ToolError(str(error)) from error
    events = cfn.describe_stack_events(StackName=stack_name)["StackEvents"][:100]
    failures = [{"at": e["Timestamp"], "resource": e["LogicalResourceId"], "status": e["ResourceStatus"],
                 "reason": e.get("ResourceStatusReason")} for e in events if "FAILED" in e["ResourceStatus"]][:15]
    return text({"name": stack["StackName"], "status": stack["StackStatus"], "reason": stack.get("StackStatusReason"),
                 "lastUpdated": stack.get("LastUpdatedTime"), "recentFailures": failures})


_pipelines: tuple[object, float] | None = None


def _pipelines_client():
    """A CodePipeline client in the pipelines account, renewed before the assumed role's hour runs out."""
    global _pipelines
    if _pipelines and time.time() < _pipelines[1]:
        return _pipelines[0]
    credentials = boto3.client("sts").assume_role(
        RoleArn=config.PIPELINES_READ_ROLE_ARN, RoleSessionName="gnome-bot")["Credentials"]
    client = boto3.client("codepipeline", region_name="us-east-1", aws_access_key_id=credentials["AccessKeyId"],
                          aws_secret_access_key=credentials["SecretAccessKey"],
                          aws_session_token=credentials["SessionToken"])
    _pipelines = (client, credentials["Expiration"].timestamp() - 300)
    return client


@beta_tool
def recent_deploys(pipeline: str | None = None) -> str:
    """Deploy pipelines (one per repo, Dev then ApproveProd then Prod): each stage's latest status, or one pipeline's
    recent executions with their commit messages.

    Args:
        pipeline: a pipeline name like RegistryPipeline or ControllerPipeline; leave out for all of them.
    """
    if not config.PIPELINES_READ_ROLE_ARN:
        raise ToolError("Deploy history isn't available in this stage")
    try:
        client = _pipelines_client()
        if pipeline is None:
            names = [p["name"] for p in client.list_pipelines()["pipelines"]]
            states = {}
            for name in names:
                stages = client.get_pipeline_state(name=name)["stageStates"]
                states[name] = {s["stageName"]: (s.get("latestExecution") or {}).get("status") for s in stages}
            return text(states)
        if not STACK_NAME.match(pipeline):
            raise ToolError(f"Not a pipeline name: {pipeline!r}")
        executions = client.list_pipeline_executions(pipelineName=pipeline, maxResults=10)["pipelineExecutionSummaries"]
        return text([{"id": e["pipelineExecutionId"], "status": e["status"], "started": e.get("startTime"),
                      "commit": [r.get("revisionSummary") for r in e.get("sourceRevisions", [])]}
                     for e in executions])
    except ToolError:
        raise
    except Exception as error:
        raise ToolError(f"Couldn't read the pipelines: {error}") from error


TOOLS = [system_health, launch_requests, launch_rules, recent_triggers, search_logs, list_log_groups, stack_status,
         recent_deploys]
