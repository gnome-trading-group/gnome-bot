import functools
import json

import boto3

from bot import config


# Secrets are read once per warm Lambda; rotating one takes effect on the next cold start.
@functools.cache
def secret_string(name: str) -> str:
    client = boto3.client("secretsmanager", region_name=config.REGION)
    return client.get_secret_value(SecretId=name)["SecretString"]


def secret_json(name: str) -> dict:
    return json.loads(secret_string(name))
