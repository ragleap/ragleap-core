# Receiving Telegram messages without a public address

By default the bot can send messages (approval pings) but receives nothing, because Telegram delivers messages to a public HTTPS webhook and a small server often has none. With long polling the app asks Telegram for new messages instead, so replies such as `YES <id>` reach it without opening a port.

## Switch it on

1. The bot token must be set (`TELEGRAM_BOT_TOKEN`).
2. Set `TELEGRAM_POLLING=true` and restart the app.
3. No webhook may be registered for the bot. Telegram refuses `getUpdates` while one exists. The app checks first; if a webhook is registered it does not poll, says so in the log and in the setup status, and checks again every minute. It never deletes your webhook. To switch from a webhook to polling, delete the webhook yourself.

Optional: `TELEGRAM_POLL_TIMEOUT` (seconds Telegram holds a request open, 1 to 50, default 30) and `TELEGRAM_POLL_WORKERS` (messages handled at the same time, 1 to 8, default 4).

## Who is answered

The Telegram bot is a document question-and-answer bot: with a webhook it answers any sender from the documents you ingested. That is why polling, which makes a bot that used to receive nothing start receiving, answers **only the owner's private chat by default**: the chat that is set as the approval target on Telegram. A message from any other chat, a group, or a sender who is not the chat itself is ignored without a reply, so a stranger who finds the bot learns nothing and cannot use your documents or your model budget. Set the approval target first (Telegram, a private chat with a positive number); without one, every message is ignored.

If you really want a public bot that answers any sender, set `TELEGRAM_POLLING_PUBLIC=true`. Then anyone who finds the bot can ask questions about your documents and use your model budget, as with a webhook. The setup status shows which mode is active.

## How it behaves

- **One poller.** Only the scheduler leader polls (the same process that runs the background jobs), so several app processes do not fight over one bot. With `SCHEDULER_ENABLED=false` a process never polls.
- **Same rules as the webhook.** The same code handles the webhook and the poller. Approvals still count only from a private chat (a one-to-one conversation) with the owner (see approval-chats.md).
- **Handling is at-least-once.** An update is acknowledged to Telegram only after its handler finished. If the app stops in the middle, Telegram delivers the update again. A repeated update is ignored while the app still remembers its id. Approval replies are safe to repeat because an approval can be claimed only once.
- **Order.** Messages from one chat are handled in order; different chats are handled at the same time.
- **A failing message** is logged by type, acknowledged and not retried forever.
- **Waiting.** Errors back off from 2 to 60 seconds. A refused token waits 5 minutes. A rate limit waits as Telegram asks.
- **The bot token** is never written to a log or to the status. Only the type of an error is kept.

## What it does not do

- It does not make the bot run agent actions. Starting agent runs from Telegram is a separate setting that is not part of this change.
- It does not replace a webhook for an installation that already uses one; it is for installations that have none.
- A message that arrives while the app is stopped waits at Telegram (for up to 24 hours) and is delivered when the app polls again.
