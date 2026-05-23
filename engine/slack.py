"""
Slack webhook notifications.
All sends are fire-and-forget (non-blocking).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional
import urllib.request
import urllib.error
import json

logger = logging.getLogger("slack")

WEBHOOK_URL = os.getenv("FORGE_SLACK_WEBHOOK", "")
# Slack user IDs to mention on critical alerts
ONCALL_TAGS = os.getenv("FORGE_SLACK_ONCALL", "")  # e.g. "<@U12345> <@U67890>"


def _send(payload: dict) -> None:
    """Send payload to Slack webhook in a background thread."""
    if not WEBHOOK_URL:
        return

    def _do_send():
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                WEBHOOK_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _ = resp.read()
        except Exception as exc:
            logger.warning("Slack notification failed: %s", exc)

    t = threading.Thread(target=_do_send, daemon=True)
    t.start()


def notify_pipeline_started(run_id: str, pipeline_name: str) -> None:
    _send({
        "text": f":rocket: *Pipeline started*",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":rocket: *Pipeline started*\n"
                        f"*Pipeline:* `{pipeline_name}`\n"
                        f"*Run ID:* `{run_id}`"
                    ),
                },
            }
        ],
    })


def notify_pipeline_finished(
    run_id: str,
    pipeline_name: str,
    status: str,
    duration_secs: float,
    failing_job: Optional[str] = None,
) -> None:
    if status == "succeeded":
        emoji = ":white_check_mark:"
        color = "#36a64f"
    else:
        emoji = ":x:"
        color = "#e01e5a"

    text = (
        f"{emoji} *Pipeline {status}*\n"
        f"*Pipeline:* `{pipeline_name}`\n"
        f"*Run ID:* `{run_id}`\n"
        f"*Duration:* {duration_secs:.1f}s"
    )
    if failing_job:
        text += f"\n*Failing job:* `{failing_job}`"

    _send({
        "attachments": [
            {
                "color": color,
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": text},
                    }
                ],
            }
        ]
    })


def notify_integrity_failure(
    run_id: str,
    artifact: str,
    expected_sha: str,
    actual_sha: str,
) -> None:
    oncall = f"\n{ONCALL_TAGS}" if ONCALL_TAGS else ""
    _send({
        "attachments": [
            {
                "color": "#ff0000",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f":rotating_light: *INTEGRITY FAILURE* {oncall}\n"
                                f"*Run ID:* `{run_id}`\n"
                                f"*Artifact:* `{artifact}`\n"
                                f"*Expected SHA-256:* `{expected_sha}`\n"
                                f"*Actual SHA-256:* `{actual_sha}`"
                            ),
                        },
                    }
                ],
            }
        ]
    })


def notify_resolution_failure(
    run_id: str,
    pipeline_name: str,
    detail: str,
) -> None:
    _send({
        "attachments": [
            {
                "color": "#ff9900",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f":warning: *Resolution failure*\n"
                                f"*Pipeline:* `{pipeline_name}`\n"
                                f"*Run ID:* `{run_id}`\n"
                                f"*Detail:* {detail}"
                            ),
                        },
                    }
                ],
            }
        ]
    })