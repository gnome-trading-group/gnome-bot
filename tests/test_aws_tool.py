import json

import boto3
import pytest
from anthropic.lib.tools import ToolError

from bot.tools import common
from bot.tools.aws import MAX_COST_EXPLORER_CALLS, REDACTED, aws_read, redact


@pytest.fixture
def run(aws):
    common.start_run(20_000)


def read(**args):
    return json.loads(aws_read.call(args))


def test_reads_any_allowed_service_and_drops_the_response_metadata(run):
    boto3.client("sqs").create_queue(QueueName="work")
    out = read(service="sqs", operation="ListQueues")
    assert out["QueueUrls"][0].endswith("/work") and "ResponseMetadata" not in out


@pytest.mark.parametrize("service,operation", [
    ("secretsmanager", "GetSecretValue"),
    ("secretsmanager", "BatchGetSecretValue"),
    ("ssm", "GetParameter"),
    ("ecr", "GetAuthorizationToken"),
    ("apigateway", "GetApiKeys"),
    ("ec2", "TerminateInstances"),
    ("s3", "PutObject"),
    ("dynamodb", "DeleteItem"),
    ("sts", "GetSessionToken"),
    ("ec2", "Describe"),
])
def test_refuses_writes_secrets_and_credentials(run, service, operation):
    with pytest.raises(ToolError):
        aws_read.call({"service": service, "operation": operation})


def test_an_unknown_operation_or_bad_params_come_back_as_tool_errors(run):
    with pytest.raises(ToolError, match="no operation"):
        aws_read.call({"service": "ec2", "operation": "DescribeUnicorns"})
    with pytest.raises(ToolError):
        aws_read.call({"service": "sqs", "operation": "GetQueueAttributes", "params": {"Nope": 1}})


def test_blanks_lambda_environments_and_ec2_user_data(run):
    ec2 = boto3.client("ec2")
    instance = ec2.run_instances(ImageId="ami-12c6146b", MinCount=1, MaxCount=1,
                                 UserData="API_KEY=hunter2")["Instances"][0]["InstanceId"]
    out = read(service="ec2", operation="DescribeInstanceAttribute",
               params={"InstanceId": instance, "Attribute": "userData"})
    assert out["UserData"] == REDACTED and "hunter2" not in json.dumps(out)


def test_redacts_nested_environments_wherever_they_appear():
    config = {"Functions": [{"FunctionName": "f", "Environment": {"Variables": {"KEY": "secret"}}}],
              "containerDefinitions": [{"name": "c", "environment": [{"name": "K", "value": "v"}]}]}
    assert redact(config) == {"Functions": [{"FunctionName": "f", "Environment": REDACTED}],
                              "containerDefinitions": [{"name": "c", "environment": REDACTED}]}


def test_reads_an_s3_objects_text(run):
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="gnome-bot-test")
    s3.put_object(Bucket="gnome-bot-test", Key="k.json", Body=b'{"rows": 3}')
    assert read(service="s3", operation="GetObject", params={"Bucket": "gnome-bot-test", "Key": "k.json"})["Body"] == '{"rows": 3}'


CE_PARAMS = {"TimePeriod": {"Start": "2026-10-20", "End": "2026-10-27"}, "Granularity": "DAILY",
             "Metrics": ["UnblendedCost"]}


def test_cost_explorer_calls_are_capped_per_answer(run):
    for _ in range(MAX_COST_EXPLORER_CALLS):
        aws_read.call({"service": "ce", "operation": "GetCostAndUsage", "params": CE_PARAMS})
    with pytest.raises(ToolError, match="limited"):
        aws_read.call({"service": "ce", "operation": "GetCostAndUsage", "params": CE_PARAMS})
    common.start_run(20_000)
    aws_read.call({"service": "ce", "operation": "GetCostAndUsage", "params": CE_PARAMS})
