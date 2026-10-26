import os

STAGE = os.environ.get("STAGE", "dev").lower()
REGION = os.environ.get("AWS_REGION", "us-east-1")

TABLE_NAME = os.environ.get("BOT_TABLE", "gnome-bot")
WORK_QUEUE_URL = os.environ.get("WORK_QUEUE_URL", "")

SLACK_BOT_TOKEN_SECRET = os.environ.get("SLACK_BOT_TOKEN_SECRET", "slack-bot-token")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "slack-signing-secret")
COGNITO_CLIENT_SECRET = os.environ.get("COGNITO_CLIENT_SECRET", "gnome-bot/cognito-client")
ANTHROPIC_API_KEY_SECRET = os.environ.get("ANTHROPIC_API_KEY_SECRET", "gnome-bot/anthropic-api-key")
GITHUB_APP_SECRET = os.environ.get("GITHUB_APP_SECRET", "gnome-bot/github-app-private-key")

REGISTRY_API_URL = os.environ.get("REGISTRY_API_URL", "")
CONTROLLER_API_URL = os.environ.get("CONTROLLER_API_URL", "")
LAUNCHER_API_URL = os.environ.get("LAUNCHER_API_URL", "")

GITHUB_ORG = os.environ.get("GITHUB_ORG", "gnome-trading-group")
PIPELINES_READ_ROLE_ARN = os.environ.get("PIPELINES_READ_ROLE_ARN", "")

CONTROLLER_UI_URL = {
    "dev": "https://controller-dev.gnometrading.group",
    "prod": "https://controller.gnometrading.group",
}.get(STAGE, "https://controller.gnometrading.group")
