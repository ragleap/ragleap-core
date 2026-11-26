# Where approvals are accepted

Approvals ("YES <id>" or "NO <id>") count only when they come from a private, one-to-one conversation with the owner.

- Telegram: the approval target must be a private chat with the owner (a positive number). A group, supergroup or channel id (a negative number) is not accepted when you save the setting, and an approval typed in a group is ignored.
- Discord: use a direct message conversation. An approval typed in a server channel is ignored.
- Why: in a group the id that arrives belongs to the group, so everyone in it would look like the owner. Approvals from the dashboard or the API are not affected.

## Pending approvals expire

A pending action that waits longer than `APPROVAL_EXPIRY_HOURS` (default 168, which is 7 days) is not run when someone approves it late: the person gets a message that it expired, and it is logged as expired. Rejecting an old one still works. Set `APPROVAL_EXPIRY_HOURS=0` to turn the limit off.

This helps an operator keep approvals with one person. It is not a certification of any kind.

## An approval is bound to the exact action

When an action waits for approval, RagLeap stores a SHA-256 hash of the action as it was proposed: its id, type, channel, target, content, subject and role. If a signing key is configured (`SPAWN_SIGNING_KEY`, else `ADDON_ENCRYPTION_KEY`), it also stores an HMAC of that hash. The approval message shows a short `Ref` (the first 12 characters of the hash).

Right before an approved action runs, RagLeap recomputes the hash from the exact fields it is about to execute and compares it with the stored one. If anything differs, even one character, nothing runs, the pending row is already used up, an `approval_mismatch` audit event is written and the owner is told to ask again.

- Typed replies stay `YES <id>` and `NO <id>`.
- The approve API route accepts an optional `action_hash` query parameter (the `Ref`, 12 to 64 hex characters). A different value runs nothing and leaves the action pending.
- Without a signing key only the hash is stored. That catches a changed field but not a database writer who also rewrites the hash. With a key, a rewritten hash or a removed signature is refused.
- A pending action created before this change has no hash. It still runs (and is audited as `approval_legacy_unbound`) until it expires after `APPROVAL_EXPIRY_HOURS`. A database writer could use this to skip the check for such rows.
- If the action cannot be stored, no approval message is sent and the agent step ends with an error.
