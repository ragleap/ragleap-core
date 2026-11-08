# Security incident runbook (for the operator)

This page helps the person who runs a RagLeap installation handle a suspected
security incident. It uses the controls RagLeap really has today. It is **not
legal advice** and it does **not** make an installation compliant with any law
or standard. Whether you must report an incident, to whom and by when is your
own decision, with your own lawyer or compliance team.

All commands below read the owner key from a private file and never print it.
Replace `/root/ragleap-home/owner_key.txt` with your own path, and `BASE` with
your RagLeap address.

```bash
BASE=http://127.0.0.1:8000
rl() { printf 'header = "x-api-key: %s"\n' "$(tr -d '\r\n' < /root/ragleap-home/owner_key.txt)" | curl -s -K - "$@"; }
```

## 1. Write down the detection time first

Before you change anything, copy the record template (section 6) and fill in
the **time you first noticed** the problem, in UTC (`date -u +%FT%TZ`). If a
reporting deadline applies to you, it is usually counted from this moment.

## 2. Contain

Do the steps that fit. Each one is reversible.

1. **Stop agents acting.** Read the current settings first and save them, so you
   can restore them: `rl $BASE/autonomy`. Then set the mode to `off` and send the
   same `channels`, `actions`, `approval_channel` and `approval_target` you saved
   (the call replaces all of them):
   `rl -X POST $BASE/autonomy -H 'content-type: application/json' -d '{"mode":"off","channels":[...],"actions":[...],"approval_channel":"...","approval_target":"..."}'`
2. **Reject waiting actions.** List them with `rl $BASE/autonomy/pending`, then
   `rl -X POST $BASE/autonomy/pending/ACTION_ID/reject` for each one you do not trust.
3. **Cut a person or a key.** Find the person with `rl $BASE/users`. Then:
   - revoke one personal key: `rl -X DELETE $BASE/users/USER_ID/keys/KEY_ID`
   - disable the person: `rl -X PATCH $BASE/users/USER_ID -H 'content-type: application/json' -d '{"status":"disabled"}'`
   - end their single sign-on sessions: `rl -X DELETE $BASE/users/USER_ID/sessions`
4. **Refuse the shared key.** Set `SHARED_KEY_DISABLED=true` in the app settings
   and recreate the app. Only a personal owner key will then work.
5. **Single sign-on.** To switch it off, remove the `OIDC_*` settings and
   recreate the app. Ending sessions (step 3) is faster for one person.
6. **Document taint.** `TAINT_RETRIEVED_DOCUMENTS` is on by default. Do not set
   it to `false` during an incident.

## 3. Keep evidence

Save these now, with a UTC timestamp in the file name. Do not paste keys into them.

- `rl "$BASE/audit?limit=500"` (admin or owner): who made which state-changing
  request, with role, method, path and status.
- `rl "$BASE/autonomy/log?limit=500"` (operator or above): what agents did,
  who approved or rejected (`decided_by`), content and result.
- `rl "$BASE/agent-runs"` and `rl $BASE/agent-runs/RUN_ID` (operator or above):
  the steps of agent runs, including whether a run was tainted.
- Container logs: `docker logs --since 24h CONTAINER > file`. Docker keeps logs
  only as long as its logging settings allow; copy them before they rotate.
- A database dump (`pg_dump`) if you need to keep the full state.

What this evidence does **not** show: read-only requests (GET) are not in the
audit log; stored content is cut to 2000 characters; there is no content hash
and no automatic purge or retention setting yet; audit rows and agent logs are
not sent to a central log store by RagLeap.

## 4. Classify

Pick a class and write it in the record. Use your own scheme, or the one your
regulator or customers require. Typical labels: unauthorised access, leaked or
misused key, malicious or poisoned document, unexpected agent action, service
outage, data exposure, other. Note what is known and what is still unknown;
update the record as you learn more.

## 5. Report and recover

- **Report:** deciding who must be told (regulator, customers, partners) and
  by when is the operator's duty. RagLeap does not send reports for you.
- **Recover:** rotate every key that may have been exposed (create a new
  personal key, then revoke the old one), restore the autonomy settings you
  saved only when you understand the cause, and re-check `rl $BASE/me`.
- **Learn:** write what happened, what worked and what to change. If the cause
  is a bug in RagLeap, report it privately, see `SECURITY.md`.

## 6. Incident record template

Copy this into your own ticket or document. RagLeap does not store it.

```text
Incident ID:
Detected at (UTC):                 <first noticed, before any change>
Detected by / how:
Reported to operator at (UTC):
Classification:
Systems and people affected:
Containment steps and times (UTC):
Evidence saved (file names):
Reporting decision:                <who decided, when, with whose advice>
Reported to (name, time UTC):      <only if a duty applies to you>
Deadline you are working to:       <detected at + the period your lawyer gives you>
Root cause:
Recovery done at (UTC):
Follow-up actions and owners:
```

## What RagLeap does not provide yet

No security-specific alert rules, no incident table or timer, no central log
store in the Docker Compose install, no audit retention setting. These are
tracked as engineering work. Until then, use this page and your own tools.
