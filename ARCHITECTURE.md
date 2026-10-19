# Architecture

## Flow

```
 Submitter DM ──(1 message or album, buffered 2.5 s)──► bot
                                                        │ duplicate check (SHA-256, 30 days)
                                                        │ post #N saved as `pending`
                                                        ▼
                                     copyMessage(s) DM ──► STORAGE channel   (permanent snapshot)
                                                        │
                                     copyMessage(s) storage ──► APPROVAL group
                                                        │   + control message (reply) with
                                                        │     [✅ Approve] [❌ Reject]
                                                        ▼
                         approver clicks / /approve ──► `approved` (queue, ETA)  or  `rejected`
                                                        │
                       scheduler tick (20 s), gap MIN..MAX min
                                                        ▼
                                     copyMessage(s) storage ──► TARGET channel  ──► `published`
                                     (snapshot missing → rebuild from saved file_ids)
```

Nothing is ever *forwarded*: copies carry no "Forwarded from" header. Publishing always copies from the
storage snapshot, never from the submitter's DM, so edits made after approval can never reach the channel.

## Status lifecycle

```
                 ┌──── reject ────► rejected
                 │
 submit ──► pending ── approve ──► approved ("scheduled") ── claim ──► publishing ──► published
                 │                  ▲   │                               │
                 └─ cancel ─┐       │   └─ cancel ─► cancelled          ├─ 429 RetryAfter → approved (wait, not counted)
                            ▼       │                                   ├─ network error  → approved (backoff 1,2,4..30 min)
                        cancelled   │                                   ├─ >5 retries / permanent error → failed
                                    │                                   └─ timeout / stop mid-send → needs_review
                                    └──────── /retry (owner) ◄──── failed, needs_review
```

Every transition is one `find_one_and_update({_id, status: <expected>}, …)`. If the status already changed
(double click, two approvers, cancel racing the scheduler) the update matches nothing and the second
action is reported as "already …". Rejected/cancelled posts have no path to `publishing`.

## Data

| Collection | Content |
|---|---|
| `posts` | `_id` (#N), status, submitter, `items[]` (media_type, file_id, file_unique_id, caption + caption_entities, text + entities, file_size, extra for polls/locations…), `storage_message_ids`, `approval_message_ids`, `control_message_id`, `channel_message_ids`, timestamps (UTC), retries, `next_attempt_at`, `last_error`, `history[]` |
| `meta` | `{_id: "state"}`: `next_publish_at`, `last_published_at`, `paused`, `min_interval`, `max_interval` |
| `counters` | `{_id: "posts", seq}` – atomic `$inc` for sequential ids |
| `lease` | `{_id: "main", holder, host, pid, run, heartbeat, expires_at}` |

Indexes (created on startup): `posts(status, decided_at)`, `posts(content_hash)`, `posts(created_at)`.

Without `MONGO_URL` the same API is served by `bot/jsonstore.py` (one JSON file in `data/`, dev only).

## Storage channel = file store

```
DM msg ──copy──► storage msg 501 ──copy──► approval msg 77
                      │
                      └──copy (publish time)──► channel msg 9001
MongoDB: items[].file_id / caption / entities  ← fallback if msg 501 was deleted
```

* Single message: `copyMessage`; album: `copyMessages` (keeps the album grouping, ids in increasing order).
* If `copyMessages` returns fewer messages than requested, the copied part is **deleted (rollback)** and the
  post is rebuilt from file_ids (`sendMediaGroup` / `sendPhoto` / `sendVideo` / …). If the rebuild fails
  with a permanent error the post becomes `failed` and the owner is alerted.

## Scheduler

```
every 20 s:
  paused?                        → skip
  now < meta.next_publish_at?    → skip                 (survives restarts: stored in MongoDB)
  first approved post by (decided_at, _id) whose next_attempt_at is empty or past
  claim it: approved → publishing (atomic)
  send; on success: published, next_publish_at = now + uniform(MIN, MAX) minutes
```

* First post ever: `next_publish_at` is empty → publishes immediately.
* `/setinterval` changes `meta.min/max_interval`; the slot already computed stays, so it applies after
  the next publish.
* Backoff only delays that one post (`next_attempt_at`); later posts can go first.
* 429: the post and the global slot wait `retry_after`; the retry counter does not change.
* A send that **times out** may have been delivered → `needs_review` + owner alert, no automatic retry.
* ETA shown to users is a window: earliest uses MIN gaps, latest uses MAX gaps.

## Instance lease (one poller)

```
 run N (holder)                           run N+1
 ─ heartbeat every 30 s ─►  lease.expires_at = now + 120 s
 ─ MAX_RUNTIME reached ─► stop polling, finish publish, DELETE lease
                                          ─ try: lease free or expired? ─► take it, start polling
                                          ─ else wait 10 s and try again (never steals a live lease)
```

GitHub `concurrency` already serialises runs; the lease additionally protects against a local copy, a VPS
copy, or a run that is killed without releasing (it simply expires after 2 minutes). If the holder cannot
renew its lease in time it shuts itself down instead of risking two pollers (Telegram 409 Conflict).

## Startup / shutdown

```
start:  config → logging → MongoDB → wait for lease → deleteWebhook → setMyCommands
        → recovery → polling + scheduler + heartbeat
stop:   (SIGTERM / SIGINT / MAX_RUNTIME_MINUTES / lease lost)
        stop polling → submit buffered albums → wait ≤20 s for in-flight publish
        (still running → cancel → needs_review) → release lease → close DB
```

Recovery on startup (only after the lease is held):
* `publishing` → `needs_review` (+ one owner alert listing them)
* `pending` without a control message (submission interrupted) → `failed` (+ submitter told to resend)
* `approved` without `decided_at` → `decided_at = created_at`

## GitHub Actions chain

```
bot.yml run ── python -m bot (MAX_RUNTIME_MINUTES = 340 − time used) ── exit 0
           └─ if: always() → gh workflow run bot.yml  (skipped when cancelled or bad config)
cron "0 */6 * * *" → backup start if the chain broke (queued behind a running run)
keepalive.yml (weekly) → keeps bot.yml enabled despite the 60-day inactivity rule
```

Runtime profiles (`RUNTIME_PROFILE`, auto-detected when empty): `github_actions` (stdout logs, 340 min
default runtime), `persistent` (Docker/systemd/VPS, rotating file log), `dev` (interactive terminal, DEBUG).
