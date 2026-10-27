"""The bot's one DynamoDB table (partition key `pk`, TTL attribute `ttl`):

- EVENT#<event_id>            a Slack event already taken, so Slack's retries don't answer twice (1 day)
- THREAD#<channel>#<ts>       a thread the bot is in, where follow-ups need no @mention (7 days since last reply)
- USAGE#<yyyy-mm-dd>          tokens spent that UTC day, against the daily budget (3 days)
- ONCE#<what>#<yyyy-mm-dd>    something done at most once a day, like raising the out-of-credit alert (2 days)
- TRIAGE#<cooldown key>        where the last triage of this kind of alert was posted, while its cooldown runs
- FIXCTX#<channel>#<ts>        what a fix in that triage thread would work from: the repo, the alert, the triage (7 days)
- FIXRUN#<channel>#<ts>        a fix running for that thread, so a second click doesn't start another (2 hours)
- FIXES#<yyyy-mm-dd>           fixes started that UTC day, against the daily cap (3 days)
- MEMPROP#<note id>            a team note the bot proposed, waiting for someone to Save it (7 days)
- MEMORY#<note id>             a saved team note (kept until someone forgets it)
"""
import json
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from bot import config

DAY = 24 * 60 * 60
EVENT_TTL = DAY
THREAD_TTL = 7 * DAY
USAGE_TTL = 3 * DAY
ONCE_TTL = 2 * DAY
FIX_CONTEXT_TTL = 7 * DAY
# A fix run is capped well under this by the workflow's timeout; the callback clears it sooner.
FIX_RUN_TTL = 2 * 60 * 60
MEMORY_PROPOSAL_TTL = 7 * DAY
MEMORY_FIELDS = ("scope", "note", "channel", "thread_ts", "saved_by", "saved_at", "link", "edited_by", "edited_at")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, table_name: str = config.TABLE_NAME):
        self._table = boto3.resource("dynamodb", region_name=config.REGION).Table(table_name)

    def _put_once(self, pk: str, ttl: int) -> bool:
        try:
            self._table.put_item(
                Item={"pk": pk, "ttl": int(time.time()) + ttl},
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def claim_event(self, event_id: str) -> bool:
        """True the first time an event id is seen."""
        return self._put_once(f"EVENT#{event_id}", EVENT_TTL)

    def claim_once_today(self, what: str) -> bool:
        return self._put_once(f"ONCE#{what}#{_today()}", ONCE_TTL)

    def track_thread(self, channel: str, thread_ts: str) -> None:
        self._table.put_item(Item={"pk": f"THREAD#{channel}#{thread_ts}", "ttl": int(time.time()) + THREAD_TTL})

    def is_tracked_thread(self, channel: str, thread_ts: str) -> bool:
        item = self._table.get_item(Key={"pk": f"THREAD#{channel}#{thread_ts}"}).get("Item")
        # TTL deletion lags by up to a couple of days, so an expired row still on the table doesn't count.
        return bool(item) and int(item["ttl"]) > time.time()

    def recent_triage(self, cooldown_key: str) -> dict | None:
        """The last triage of this kind of alert, while its cooldown runs: {channel, ts}."""
        item = self._table.get_item(Key={"pk": f"TRIAGE#{cooldown_key}"}).get("Item")
        if not item or int(item["ttl"]) <= time.time():
            return None
        return {"channel": item["channel"], "ts": item["ts"]}

    def record_triage(self, cooldown_key: str, channel: str, ts: str, cooldown_seconds: int) -> None:
        self._table.put_item(Item={"pk": f"TRIAGE#{cooldown_key}", "channel": channel, "ts": ts,
                                   "ttl": int(time.time()) + cooldown_seconds})

    def save_fix_context(self, channel: str, thread_ts: str, context: dict) -> None:
        self._table.put_item(Item={"pk": f"FIXCTX#{channel}#{thread_ts}", "context": json.dumps(context),
                                   "ttl": int(time.time()) + FIX_CONTEXT_TTL})

    def fix_context(self, channel: str, thread_ts: str) -> dict | None:
        item = self._table.get_item(Key={"pk": f"FIXCTX#{channel}#{thread_ts}"}).get("Item")
        if not item or int(item["ttl"]) <= time.time():
            return None
        return json.loads(item["context"])

    def claim_fix_run(self, channel: str, thread_ts: str) -> bool:
        """True unless a fix is already running for this thread."""
        try:
            self._table.put_item(
                Item={"pk": f"FIXRUN#{channel}#{thread_ts}", "ttl": int(time.time()) + FIX_RUN_TTL},
                ConditionExpression="attribute_not_exists(pk) OR #ttl < :now",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":now": int(time.time())},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release_fix_run(self, channel: str, thread_ts: str) -> None:
        self._table.delete_item(Key={"pk": f"FIXRUN#{channel}#{thread_ts}"})

    def claim_fix_slot(self, daily_cap: int) -> bool:
        """Counts a fix against today's cap; False, and nothing counted, once the cap is reached."""
        # The condition below lets the day's first fix through whatever the cap, since its counter doesn't exist yet.
        if daily_cap <= 0:
            return False
        try:
            self._table.update_item(
                Key={"pk": f"FIXES#{_today()}"},
                UpdateExpression="ADD fixes :one SET #ttl = if_not_exists(#ttl, :ttl)",
                ConditionExpression="attribute_not_exists(fixes) OR fixes < :cap",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":one": 1, ":cap": daily_cap, ":ttl": int(time.time()) + USAGE_TTL},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def tokens_today(self) -> int:
        item = self._table.get_item(Key={"pk": f"USAGE#{_today()}"}).get("Item")
        return int(item["tokens"]) if item else 0

    def add_tokens(self, tokens: int) -> None:
        self._table.update_item(
            Key={"pk": f"USAGE#{_today()}"},
            UpdateExpression="ADD tokens :t SET #ttl = if_not_exists(#ttl, :ttl)",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":t": tokens, ":ttl": int(time.time()) + USAGE_TTL},
        )

    def save_memory_proposal(self, note_id: str, proposal: dict) -> None:
        self._table.put_item(Item={"pk": f"MEMPROP#{note_id}", **proposal,
                                   "ttl": int(time.time()) + MEMORY_PROPOSAL_TTL})

    def memory_proposal(self, note_id: str) -> dict | None:
        item = self._table.get_item(Key={"pk": f"MEMPROP#{note_id}"}).get("Item")
        if not item or int(item["ttl"]) <= time.time():
            return None
        return {k: item[k] for k in ("scope", "note", "channel", "thread_ts")}

    def delete_memory_proposal(self, note_id: str) -> None:
        self._table.delete_item(Key={"pk": f"MEMPROP#{note_id}"})

    def save_memory(self, note_id: str, note: dict) -> None:
        self._table.put_item(Item={"pk": f"MEMORY#{note_id}",
                                   **{k: note[k] for k in MEMORY_FIELDS if note.get(k) is not None}})

    def edit_memory(self, note_id: str, changes: dict) -> bool:
        """Changes some of a saved note's fields; False if there's no such note."""
        names = {f"#{k}": k for k in changes}
        try:
            self._table.update_item(
                Key={"pk": f"MEMORY#{note_id}"},
                UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in changes),
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={f":{k}": v for k, v in changes.items()},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def delete_memory(self, note_id: str) -> bool:
        """False if there was no such note."""
        return "Attributes" in self._table.delete_item(Key={"pk": f"MEMORY#{note_id}"}, ReturnValues="ALL_OLD")

    def memories(self) -> dict[str, dict]:
        """Every saved team note by id. A scan, but the table only holds a few thousand short-lived rows."""
        notes, params = {}, {"FilterExpression": Attr("pk").begins_with("MEMORY#")}
        while True:
            page = self._table.scan(**params)
            for item in page["Items"]:
                notes[item["pk"].removeprefix("MEMORY#")] = {k: item.get(k) for k in MEMORY_FIELDS}
            if "LastEvaluatedKey" not in page:
                return notes
            params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
