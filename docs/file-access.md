# File access for agents

Agents can work with folders you grant. Nothing is granted by default, and the agent never sees or chooses a host path: it names a grant and a path inside it. This page describes what exists on main. It is not a security certification, and the outside security review (issue #800) has not happened yet. Keep writing off until you are comfortable with it.

## Grants (reading)

Grants are made on the machine with the `ragleap files` command, never from the chat:

    ragleap files add /path/to/folder --name notes --mode read
    ragleap files list
    ragleap files off    # pause all file access now
    ragleap files on

Needs: `COMPUTER_CONTROL=true` on the sandbox runner, `FILE_TOOLS_ENABLED=true` and `SANDBOX_TOKEN` in the app. Tools: `list_files`, `read_file` (text only, the start of the file). Anything an agent reads counts as outside content, so outbound actions after it need your approval, as for web pages.

## Writing (off by default)

All of these must be true before an agent can even propose a write:

1. `COMPUTER_CONTROL=true` and `FILE_TOOLS_ENABLED=true` (as above).
2. A grant made with `--mode write`. The `ragleap files add` command still refuses write grants on purpose; that is switched on separately, when you decide to roll writing out.
3. `ragleap files writing on` on the machine (the runner checks it on every call).
4. `FILE_WRITE_ENABLED=true` in the app (this is the app-side kill switch).

How a write goes:

- The agent proposes `write_file`: the path on the first line, the whole new text after it, at most 2000 characters. Nothing is cut silently.
- The app first asks the runner for a read-only preview and stores it with the action. If the preview is refused, or the new text is identical to the file, no action is created.
- **Every write needs your approval, also in `full` autonomy mode.** The approval message and the dashboard show the path, whether it replaces or creates a file, lines added and removed, short hashes and the start of the diff.
- After approval exactly that text is written, and only if the file still has the content that was previewed. If it changed in between, nothing is written.
- The old version is kept first in `.ragleap-trash` inside the granted folder, the new file is written atomically, and the result is read back. The trash holds at most 1000 old versions; when it is full, writes are refused until you empty it by hand. The agent cannot list, read or write the trash or any name starting with `.ragleap-`.
- Logs and the learning store keep the path and hashes, never the file text. A `file_write` audit line carries grant, path, old and new hashes and the trash name.

## The trash (host only)

Old versions land in `.ragleap-trash` inside the granted folder. You manage them on the machine, never through the agent:

    ragleap files trash list notes
    ragleap files trash restore notes <id>            # <id> is the first column of the list
    ragleap files trash restore notes <id> --replace  # also when a file is there now; the current one goes into the trash
    ragleap files trash empty notes --older-than 7d   # shows what would go; add --yes to remove for good

The runner can also move one file into the trash (operation `delete`, only if the file still matches what was previewed, and only while writing is switched on). Nothing gives the agent a delete tool yet: that is the next step.

Not included yet: the agent tool for deleting files, and broad folders.

## Folder permissions

The runner container is hardened: no network, no capabilities, a read-only root and user 65534. For a write grant, the folder must be writable by that user. Making it so is your decision and happens on the machine, for example with a dedicated folder you create for this.

## Limits to know

- Approving is allowed for the `operator` role and above, like other approvals. Say so in your team rules if you give that role to people who should not approve file changes.
- Text files only (UTF-8), small edits; this is not a general file manager.
- Whoever approves should read the diff, not only the title.
