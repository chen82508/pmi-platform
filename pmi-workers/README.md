# pmi-workers

Background execution for the platform, built on the Postgres queue in
`pmi_core.queue`. There is no Redis, Celery or Temporal.

- **Scheduler.** `supercronic` runs [`cron/crontab`](cron/crontab). Every beat
  only *enqueues* a job (`pmi-workers enqueue <job>`), so a slow job never
  blocks the scheduler.
- **Consumers.** `pmi-workers worker --queue <lane>` claims jobs with
  `FOR UPDATE SKIP LOCKED` and wakes on `LISTEN/NOTIFY`. Each job declares its
  lane in `@register(name, queue=...)`:

  | Lane | Jobs | Why separate |
  |---|---|---|
  | `default` | scoring and per-market re-evaluation | High volume, short |
  | `snapshot` | page snapshots written to `ts_page_cache` | Page freshness must not wait behind scoring |
  | `batch` | whole-registry jobs: `hourly`, `score-all`, drift re-evaluation, tagging | Serialised so two long scans never contend |
  | `long` | `daily`, embeddings, workflows (backtests) | Multi-hour jobs |

  `tests/test_registry_queues.py` pins the lane table. A lane with no running
  consumer never executes its jobs.

- **Durable workflows.** Backtests run as checkpointed steps
  (`core_workflow_runs` / `core_workflow_steps`). After a crash, the queue
  retry replays the completed steps from cache.

```bash
pmi-workers list                 # registered jobs
pmi-workers run score-all        # run a job inline
pmi-workers enqueue hourly       # queue a job
pmi-workers worker --queue default
```

Failures are reported through `pmi-notify` (Slack) when `SLACK_WEBHOOK_URL`
is set.
