---
name: pmi-async-loop-safety
description: >-
  Mandatory review rule for async Python in this repo. Read before writing,
  reviewing or refactoring anything that runs on an asyncio event loop:
  pmi-api routes and the /ask agent, the pmi-workers queue worker and its
  LISTEN/NOTIFY connection, the pmi-mcp server, and any long-lived loop or
  persistent connection. Covers the two ways a loop stalls (synchronous work
  blocking it; slow awaited work inside a reader causing backpressure), the
  required patterns, and a review checklist.
---

# Async event loop safety

**The rule:** code on an asyncio loop must never stall the loop, or a
connection's reader, for longer than that connection's or caller's budget.
Slow work runs somewhere else, and every external await is time-bounded.

## 1. Why this rule exists

The production version of this platform ran a WebSocket consumer whose
`async for message in ws:` body awaited a token refresh (a query averaging
155 s) and one DB transaction per message. Nothing blocked: every await
yielded. But while the body awaited, frames stopped being read, the library's
bounded incoming queue filled, it paused reading the socket, the server's PONG
sat unread, and the ping timeout closed the connection, about every 25
minutes, with no crash and green health checks.

**"It's async, so it can't block" is false.** Awaiting slow work in a reader is
as fatal as blocking the loop, and `run_in_executor` does not help: the work
was already non-blocking. The fix is to move it out of the reader.

## 2. Two failure modes

| | A. Loop blocking | B. Reader backpressure |
|---|---|---|
| Cause | Sync work on the loop: `requests`, sync DB drivers, `time.sleep`, large parses, sync file IO | Slow `await`s inside a read loop: DB, HTTP, refreshes, per-message writes |
| What stops | Every task on the loop | That connection's reading (including control frames) |
| Symptom | Timeouts everywhere at once; uvicorn stops answering | One connection drops with a ping/keepalive timeout; the rest is fine |
| Fix | `await asyncio.to_thread(fn, ...)`; a process pool for heavy CPU | Reader-only loop + bounded queue + separate worker tasks (§3.1) |

## 3. Required patterns

### 3.1 Persistent connections: reader-only loop, bounded queue

```python
async def consume(ws) -> None:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX)

    async def reader() -> None:            # only drains frames
        async for message in ws:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                dropped.inc()               # count; never await here

    async def writer() -> None:            # batched persistence, off the read path
        while True:
            batch = [await queue.get()]
            while not queue.empty() and len(batch) < BATCH_MAX:
                batch.append(queue.get_nowait())
            await persist(batch)            # one transaction per batch

    async def refresher() -> None:         # periodic loaders, own task, timed out
        while True:
            await asyncio.sleep(REFRESH_SEC)
            await asyncio.wait_for(reload(), timeout=REFRESH_TIMEOUT)

    async with asyncio.TaskGroup() as tg:  # a dead task tears the connection down
        tg.create_task(reader()); tg.create_task(writer()); tg.create_task(refresher())
```

- The reader awaits nothing but the socket: no DB, HTTP, sessions, or locks.
- Queues are bounded; overflow drops and counts. Never `await queue.put()` in
  the reader.
- A periodic job's interval must exceed its worst-case runtime, and it runs
  under a timeout.

The queue worker's LISTEN connection (`pmi-workers/pmi_workers/worker.py`)
follows this shape: the listener callback only sets an `asyncio.Event`; claiming
and running jobs happens in the worker loop.

### 3.2 Sync work goes to a thread

```python
rows = await asyncio.to_thread(parse_big_payload, raw)
```

Never call a sync DB driver, `requests`, `time.sleep`, or a multi-second
parse inside `async def`. In FastAPI, a route doing sync work is a plain `def`
(FastAPI threads it), not an `async def` that blocks.

### 3.3 Every external await is bounded

- **DB:** the connection-wide statement timeout, plus `pmi_api.db_guard.bounded`
  for a query that may scan more (it uses a savepoint with `SET LOCAL
  statement_timeout`, so only that query is cancelled).
- **HTTP:** explicit `httpx` timeouts: pmi-mcp's pmi-api calls use 60 s and
  the `/ask` agent's LLM call 120 s. The agent's MCP client session
  (`streamablehttp_client`) still runs on the library defaults; pass
  `timeout=` explicitly when you touch it.
- **Locks:** don't hold an `asyncio.Lock` across slow I/O on a request path;
  every waiter inherits the stall.
- **Background loaders:** `asyncio.wait_for` / `asyncio.timeout`.

### 3.4 Repeated queries are planned first

A query that a loop or a hot route runs repeatedly gets an `EXPLAIN` against
realistic volumes before it ships (`ts_price_snapshots` grows by market ×
snapshot interval; `core_markets` reached millions of rows in production).
Prefer reading denormalized columns (`core_markets.last_price`) over scanning
the time-series table per request.

## 4. Review checklist (copy into the PR)

- [ ] No sync I/O or multi-second CPU work inside `async def` (or it is in
      `to_thread` / an executor).
- [ ] Persistent-connection read loops only enqueue; no DB, HTTP or lock awaits.
- [ ] Reader → worker queues are bounded; overflow drops and is counted.
- [ ] Periodic loaders run in their own task, with interval > worst case and a timeout.
- [ ] Every DB / HTTP await in a long-lived loop or hot route has a timeout.
- [ ] Stream writes are batched, not one transaction per message.
- [ ] New repeated queries were `EXPLAIN`ed at realistic scale.
- [ ] A failing background task is loud (TaskGroup, or logged and notified via
      pmi-notify), never a silently dead task.

## 5. Diagnosing

- **Loop blocking:** gaps in a process's periodic logs across all its tasks;
  `py-spy dump --pid <pid>` shows what holds the loop. Locally,
  `PYTHONASYNCIODEBUG=1` logs any step slower than 100 ms.
- **Reader backpressure:** keepalive / ping-timeout disconnects on one
  connection while the rest of the process is healthy.
- **Slow awaited queries:** `pg_stat_statements` (enabled in the compose
  Postgres) ordered by `mean_exec_time` or `shared_blks_read`.
