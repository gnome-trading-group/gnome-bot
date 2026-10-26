"""The GitHub App's webhook: every PR comment in the org arrives here, signed with the App's webhook secret. GitHub
gives a delivery 10 seconds, so this only checks it and queues the ones gnome-bot acts on; the worker starts the run."""
import base64
import json
import logging

import boto3

from bot import config, pr_assist

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_sqs = None


def handler(event, context):
    global _sqs
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    if not pr_assist.verify_signature(body, headers.get("x-hub-signature-256", "")):
        return {"statusCode": 401, "body": "invalid signature"}
    request = pr_assist.request_from(headers.get("x-github-event", ""), json.loads(body))
    if request:
        _sqs = _sqs or boto3.client("sqs", region_name=config.REGION)
        _sqs.send_message(QueueUrl=config.WORK_QUEUE_URL, MessageBody=json.dumps(request))
        logger.info("Queued @gnome-bot on %s#%s by %s", request["repo"], request["pr_number"], request["requested_by"])
    return {"statusCode": 202, "body": ""}
