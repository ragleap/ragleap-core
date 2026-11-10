# AUDIT log lines

Every agent action writes one JSON line on the `ragleap.audit` logger, so a log shipper can keep it. The line
starts with `AUDIT ` and then the JSON. This page lists the events and fields. It is a guide for the **operator**;
it is not legal advice and it does not make an installation compliant with any law or standard.

```text
INFO:ragleap.audit:AUDIT {"action_id": "3f9c1a2b", "action_type": "send_email", "audit": "action_proposed", "channel": "email", "content_sha256": "...", "request_id": "9b1e...", "role": "sales", "target": "a@b.c", "ts": "2026-11-09T12:00:00.123456Z"}
```

## Events

| `audit` | When |
|---|---|
| `action_proposed` | an action is waiting for approval (`action_id` links it to its decision) |
| `action_executed` | an action ran, after approval or on its own (`autonomous` says which) |
| `action_error` | an action ran and failed |
| `action_rejected` | an approval was refused |
| `action_skipped` | an action was not allowed (`reason`: autonomy off, action not allowed, channel not allowed) |
| `action_forced_approval` | an action that would have run on its own needs approval (untrusted input) |
| `retention_purge` | the retention job deleted rows (`table`, `retention_days`, `cutoff`, `deleted`, `trigger`), see `docs/audit-retention.md` |

## Fields

| Field | Meaning |
|---|---|
| `ts` | UTC time of the event |
| `audit` | the event name above |
| `request_id` | the `X-Request-ID` of the call that caused it, when it came from a call (see below) |
| `action_id` | links a proposal to its approval or rejection |
| `action_type`, `channel`, `role` | what, where, which role |
| `target` | the recipient or address, cut to 200 characters, in clear |
| `approver`, `approver_role`, `approver_kind` | who approved or rejected it |
| `autonomous` | true when no person approved it |
| `content_sha256`, `result_sha256` | sha256 of the full content and of the result; the text itself is never written |

## The request id

Every response carries `X-Request-ID`. You can send your own (8 to 64 letters, digits or `-._`); otherwise one is made.
An `AUDIT` line written while handling a call carries the same id, so a line can be matched to the call in your proxy or
client log. Lines written later in the background (for example when an agent run continues after an approval) have no
request id. Set `LOG_REQUEST_ID=true` to put the id in every app log line as well; the format is unchanged otherwise.

## Find the lines

```bash
docker logs --since 1h ragleap-core-app-1 2>&1 | grep 'AUDIT ' | tail -5
```

In Loki (LogQL), for example: show the lines of one request.

```text
{container="ragleap-core-app-1"} |= "AUDIT " |= "9b1e..."
```

## Switch them off

`AUDIT_LOG_LINES=false` turns them off. How long the lines are kept is up to your own log store; see
`docs/audit-retention.md` for the database tables.
