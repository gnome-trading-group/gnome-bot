import hashlib
import hmac

import pytest

from bot import slack


@pytest.mark.parametrize("markdown,expected", [
    ("**Up $12** today", "*Up $12* today"),
    ("## Summary\nok", "*Summary*\nok"),
    ("- one\n- **two**\n  * nested", "• one\n• *two*\n  • nested"),
    ("see [the runbook](https://x.example/r)", "see <https://x.example/r|the runbook>"),
    ("*already* _slack_ `code`", "*already* _slack_ `code`"),
])
def test_turns_markdown_into_slack_formatting(markdown, expected):
    assert slack.mrkdwn(markdown) == expected


def test_signature_check_accepts_slacks_scheme(aws):
    body, ts = '{"a":1}', "1700000000"
    sig = "v0=" + hmac.new(b"signing-secret", f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    assert slack.verify_signature(body, ts, sig, now=1700000100)
    assert not slack.verify_signature(body, ts, sig, now=1700001000)
    assert not slack.verify_signature(body + " ", ts, sig, now=1700000100)
    assert not slack.verify_signature(body, "not-a-number", sig)
