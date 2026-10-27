"""Any read-only AWS API call in the stage's own account, for what the purpose-built tools don't cover: where spend
went (Cost Explorer), who changed what (CloudTrail), and the state of instances, queues, buckets and the rest.

The worker's role has AWS's ReadOnlyAccess, which is the real boundary. This tool narrows it further, because
whatever it returns can end up in a Slack message: only read operations, no operation that hands out a credential or
key, and values that hold secrets in plain text (Lambda and container environments, EC2 user data) are blanked
before the model sees them."""
import re

import boto3
from anthropic.lib.tools import ToolError, beta_tool
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError
from botocore.response import StreamingBody

from bot.tools import common
from bot.tools.common import text
from bot.tools.ops import REGIONS

SERVICES = frozenset({
    "acm", "apigateway", "athena", "autoscaling", "batch", "bedrock", "budgets", "ce", "cloudformation", "cloudfront",
    "cloudtrail", "cloudwatch", "codebuild", "codepipeline", "dynamodb", "ec2", "ecr", "ecs", "elasticache", "elbv2",
    "events", "glue", "health", "iam", "kms", "lambda", "logs", "pricing", "rds", "route53", "s3", "scheduler",
    "secretsmanager", "service-quotas", "sns", "sqs", "ssm", "stepfunctions",
})
READ_OPERATION = re.compile(r"^((Describe|List|Get|Lookup|Search|BatchGet)[A-Z]\w*|Query|Scan)$")
# Reads that return a credential, a key or a secret's value.
REFUSED = frozenset({
    "GetSecretValue", "BatchGetSecretValue", "GetRandomPassword", "GetAuthorizationToken", "GetDownloadUrlForLayer",
    "GetApiKey", "GetApiKeys", "GetPasswordData", "GetParameter", "GetParameters", "GetParametersByPath",
    "GetParameterHistory", "GetCredentialReport", "GetAccessKeyLastUsed", "GetSSHPublicKey",
})
# Keys whose values hold configuration that is often a secret in plain text.
REDACTED_KEYS = frozenset({"Environment", "environment", "environmentVariables", "UserData", "userData"})
REDACTED = "[redacted by gnome-bot]"
# Cost Explorer charges $0.01 a request.
MAX_COST_EXPLORER_CALLS = 10


def redact(value):
    if isinstance(value, dict):
        return {k: REDACTED if k in REDACTED_KEYS else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _body(body: StreamingBody) -> str:
    data = body.read(common.output_limit + 1)
    body.close()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return f"[binary object, first {len(data)} bytes not shown]"


@beta_tool
def aws_read(service: str, operation: str, params: dict | None = None, region: REGIONS = "us-east-1") -> str:
    """Calls one read-only AWS API in this stage's account and returns its response. Use it when no other tool covers
    the question: Cost Explorer for where spend went, CloudTrail for who created or changed something and when,
    CloudWatch metrics, and the current state of EC2, ECS, Lambda, S3, RDS, SQS and the rest. Returns one page; pass
    the response's NextToken (or the service's equivalent) in `params` for the next.

    Args:
        service: the boto3 service name, e.g. ce, cloudtrail, ec2, cloudwatch, lambda, s3, ecs, rds, sqs.
        operation: the API action as AWS names it, e.g. GetCostAndUsage, LookupEvents, DescribeInstances,
            GetMetricData, ListFunctions, GetObject. Only Describe/List/Get/Lookup/Search/BatchGet/Query/Scan calls.
        params: the action's request parameters as AWS documents them, e.g. {"TimePeriod": {"Start": "2026-10-20",
            "End": "2026-10-27"}, "Granularity": "DAILY", "Metrics": ["UnblendedCost"], "GroupBy": [{"Type":
            "DIMENSION", "Key": "USAGE_TYPE"}]}.
        region: the region to call. Cost Explorer, Budgets, IAM and Health answer from us-east-1 for the whole account.
    """
    if service not in SERVICES:
        raise ToolError(f"{service!r} isn't one of the services this tool reads: {', '.join(sorted(SERVICES))}")
    if not READ_OPERATION.match(operation) or operation in REFUSED:
        raise ToolError(f"{operation} isn't a read this tool makes (secrets, keys and credentials are never read)")
    client = boto3.client(service, region_name=region)
    names = {api: name for name, api in client.meta.method_to_api_mapping.items()}
    if operation not in names:
        raise ToolError(f"{service} has no operation {operation}")
    if service == "ce":
        if common.cost_explorer_calls >= MAX_COST_EXPLORER_CALLS:
            raise ToolError(f"Cost Explorer is limited to {MAX_COST_EXPLORER_CALLS} calls an answer (each costs "
                            f"$0.01); answer from what you have")
        common.cost_explorer_calls += 1
    try:
        response = getattr(client, names[operation])(**(params or {}))
    except (ClientError, ParamValidationError, BotoCoreError) as error:
        raise ToolError(str(error)) from error
    response.pop("ResponseMetadata", None)
    if isinstance(response.get("Body"), StreamingBody):
        response["Body"] = _body(response["Body"])
    return text(redact(response))


TOOLS = [aws_read]
