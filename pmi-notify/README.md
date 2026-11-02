# pmi-notify

A small library that sends failure notifications to a Slack incoming webhook.
Services build a notifier from the environment and call it from their error
paths. There are three entry points: the worker job runner, the queue
consumer, and the API's catch-all exception handler.

- `SLACK_WEBHOOK_URL` empty: notifications are logged and skipped, never
  raised.
- `SLACK_ENVIRONMENT` is added to every message.
- Includes a decorator for wrapping functions and a debug runner
  (`python -m pmi_notify.run_debug`).

Infrastructure alarms take a separate path: CloudWatch → SNS → Lambda (see
[`infra/terraform`](../infra/terraform/)).

```bash
../.venv/bin/pytest -q
```
