# Observe-only message feed replay

`ragleap_physical.adapters.message_feed` replays a recorded file of messages as observations. It is a model of a message feed,
not a client of one: there is no network code, no URL, no subscription, no dependency and no command. Standard library only.
Status: pre-alpha, simulation only. It has not been run on a recording from a real system.

## What it does

- The caller gives the bytes of a recorded file (or reads a local file with `read_feed_file`, which refuses links, directories,
  pipes and anything above the byte cap) and a `FeedSpec`.
- Every message is one line: a flat JSON object with exactly three keys, `topic` (text), `time_s` (seconds) and `payload` (a
  number, or one of the words declared for that topic).
- The `FeedSpec` declares each accepted topic with its sensor id, unit, scale, value range and, if wanted, words with the
  number each one stands for. A message for any other topic is refused.
- An observation shows, for each declared topic, the last value received up to the replay time (`<sensor id>`) and its age
  (`<sensor id>_age_s`). A topic with no message yet is **missing**: it has no value and no age, it is never shown as zero, and
  `topics_missing` counts it. `FakeMessageFeedAdapter.missing()` lists the topics.
- The adapter has no target. `apply()` and `dry_run()` refuse. `stop()` freezes the replay.

## What it refuses

The whole file is refused, before the first message is replayed, if any line fails. An error names the line number and the
declared sensor id, never text from the file.

- not printable ASCII, a lone carriage return, a blank line, an empty file;
- anything but a JSON object with exactly the three keys; a repeated key; `NaN`, `Infinity`; nesting; booleans; null;
- a topic that was not declared; a word that was not declared for that topic; a number outside the declared range after scaling;
- a time that does not strictly increase, from one message to the next, across all topics.

## Hard caps

The caller may ask for less, never for more.

| Cap | Limit |
|---|---|
| bytes | 5,000,000 |
| messages | 100,000 |
| topics | 64 |
| characters per line | 4,096 |
| characters per topic | 64 |
| characters per payload number | 32 |
| declared words per topic | 8 |
| characters per declared word | 16 |
| time span in seconds | 10,000,000 |

## Failure modes it can show

A recorded file can hold old, repeated or missing messages on purpose, so a consumer can be tested against a stale value (look at
the age), a topic that never arrives (look at `topics_missing`) and a value that repeats. It does not model a device that reports
a state it is not in.

## Limits

- The recorded file is the only input. Nothing is subscribed to, and nothing here talks to a broker.
- Topic and word rules are this adapter's own: they were not checked against any messaging standard. [unverified]
- A replay passes through the Bridge (observe-only) without a sensor being dropped, tested in `tests/test_message_feed_bridge.py`.
- Not yet tested through the console.
- The strict text and number checks and the caps for bytes, line length and time span come from the CSV replay module.
