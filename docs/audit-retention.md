# Audit retention

By default RagLeap keeps its audit trail forever. This page explains how to set a limit, what the purge deletes,
and what to do before you turn it on. It is a guide for the **operator**. It is not legal advice and it does not
make an installation compliant with any law or standard; how long you must keep or may keep records is a
question for your own counsel.

## What can be limited

| Setting | Table | What it holds |
|---|---|---|
| `AUDIT_RETENTION_DAYS` | `audit_events` | who changed what (method, path, status, the person) |
| `ACTION_LOG_RETENTION_DAYS` | `autonomy_log` | what agents did, who approved or rejected it, the content (cut to 2000 characters) |
| `AGENT_RUN_RETENTION_DAYS` | `agent_runs` | finished agent runs with their steps and the text they worked on |

Each setting is a number of days. Empty or `0` keeps everything (the default). A value between 1 and 6 is
refused and treated as "keep everything", so a typo cannot wipe the history. Set them in your `.env` and restart
the app.

Agent runs that are `running` or `waiting_approval` are never deleted. Pending approvals, documents and memory are
not touched by this feature.

## Before you turn it on

Deleting rows removes evidence. First make sure the `AUDIT` log lines (one JSON line per agent action, see
`ragleap.audit`) reach a log store you keep for as long as you need. Then pick the number of days with your own
counsel. Every purge writes one `retention_purge` log line per table with the number of rows deleted, so the
deletion itself leaves a trace in that store.

## How it works

When at least one limit is set, the scheduler leader runs the purge every 6 hours. It deletes in batches of 1000
(at most 50 batches per table and run; the rest is taken in the next run). Nothing runs when no limit is set.

## Check and run it

These calls need an admin (status) or the owner (purge). They read the key from a private file.

```bash
rl() { printf 'header = "x-api-key: %s"\n' "$(tr -d '\r\n' < /root/ragleap-home/owner_key.txt)" | curl -s -K - "$@"; }
rl http://127.0.0.1:8000/retention                          # the limits and how many rows are already older
rl -X POST "http://127.0.0.1:8000/retention/purge"          # dry run (the default): only counts
rl -X POST "http://127.0.0.1:8000/retention/purge?dry_run=false"   # delete now
```

## Not covered yet

Request bodies are not stored, but the text of agent runs and the activity log is kept in full until its limit
passes. Redaction of stored prompts is not built yet.
