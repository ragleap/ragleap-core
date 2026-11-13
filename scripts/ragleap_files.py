#!/usr/bin/env python3
"""
ragleap files: the owner's command for folder grants (stdlib only).

Only this command writes grants/grants.json. The sandbox runner reads it (the folder is mounted
read-only into the runner and nowhere else), so the app, the API and the AI employees cannot create,
change or widen a grant. The same rules the runner applies are used here, imported from the runner.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import time
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("RAGLEAP_HOME") or HERE.parent).resolve()
GRANTS_DIR = HOME / "grants"
GRANTS_FILE = GRANTS_DIR / "grants.json"
DISABLED_FLAG = GRANTS_DIR / "DISABLED"
WRITE_FLAG = GRANTS_DIR / "WRITE_ENABLED"
SCAN_LIMIT = 2000
TRASH_NAME = ".ragleap-trash"
STAMP_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")


def _load_runner():
    spec = importlib.util.spec_from_file_location("ragleap_sandbox_runner", HERE.parent / "sandbox" / "runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _load_runner()


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def read_raw():
    if not GRANTS_FILE.exists():
        return []
    try:
        data = json.loads(GRANTS_FILE.read_text(encoding="utf-8"))
    except ValueError:
        fail("grants/grants.json is not valid JSON. Fix or remove it by hand; nothing was changed.")
    items = data.get("grants") if isinstance(data, dict) else None
    if not isinstance(items, list):
        fail("grants/grants.json has no 'grants' list. Fix or remove it by hand; nothing was changed.")
    return items


def write_raw(items):
    GRANTS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(GRANTS_DIR), prefix=".grants-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "grants": items}, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, GRANTS_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def parse_expires(text):
    if text is None:
        return None
    m = re.fullmatch(r"(\d+)([mhd])", text.strip().lower())
    now = datetime.now(timezone.utc)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        when = now + timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})
    else:
        try:
            when = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            fail("--expires must look like 30m, 12h, 7d or a date such as 2026-12-31")
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    if when <= now:
        fail("--expires is already in the past")
    return when.replace(microsecond=0).isoformat()


def env_flag():
    env = HOME / ".env"
    if not env.exists():
        return False
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("COMPUTER_CONTROL="):
            return line.split("=", 1)[1].strip().strip('"').lower() == "true"
    return False


def scan(folder):
    """Counts files and folders other users cannot read (the sandbox runs as an unprivileged user)
    and protected names that will stay hidden. Looks at the first SCAN_LIMIT entries only."""
    unreadable, example, protected, seen = 0, None, 0, 0
    for root, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            seen += 1
            if seen > SCAN_LIMIT:
                return unreadable, example, protected, True
            full = os.path.join(root, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if runner.name_denied(name):
                protected += 1
            need = (stat.S_IROTH | stat.S_IXOTH) if stat.S_ISDIR(st.st_mode) else stat.S_IROTH
            if st.st_mode & need != need:
                unreadable += 1
                example = example or os.path.relpath(full, folder)
    return unreadable, example, protected, False


def cmd_add(args):
    if not runner.GRANT_NAME_RE.fullmatch(args.name):
        fail("The name must be 1 to 32 characters: lower-case letters, digits, - or _, starting with a letter or digit.")
    if args.mode != "read":
        fail("Only read-only grants are available for now.")
    given = os.path.expanduser(args.path)
    if not os.path.exists(given):
        fail("That folder does not exist: " + args.path)
    resolved = os.path.realpath(given)
    if not os.path.isdir(resolved):
        fail("That is not a folder. Folders only for now: " + args.path)
    problem = runner.grant_path_problem(resolved)
    if problem:
        fail("Not granted: " + problem + " (" + resolved + ")")
    home = str(HOME)
    if home == resolved or home.startswith(resolved + os.sep) or resolved.startswith(home + os.sep):
        fail("Not granted: that folder contains, or is inside, the RagLeap install folder.")
    items = read_raw()
    if any(isinstance(g, dict) and g.get("name") == args.name for g in items):
        fail("The name '" + args.name + "' is already used. Remove it first: ragleap files remove " + args.name)
    entry = {"name": args.name, "path": resolved, "mode": "read", "purpose": args.purpose,
             "expires": parse_expires(args.expires), "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    unreadable, example, protected, truncated = scan(resolved)
    items.append(entry)
    write_raw(items)
    print("Granted '%s' (read-only): %s" % (args.name, resolved))
    if resolved != os.path.abspath(given):
        print("  (the path you gave was a link; the grant uses where it really points)")
    print("  expires: " + (entry["expires"] or "never"))
    if protected:
        print("  note: %d protected item(s) (such as .env files or keys) exist inside and stay hidden from the AI." % protected)
    if unreadable:
        print("  warning: %d item(s) are not readable by everyone (for example %s). The sandbox cannot read those; "
              "give them permissions like 644 for files and 755 for folders if they should be visible." % (unreadable, example))
    if truncated:
        print("  (only the first %d entries were checked)" % SCAN_LIMIT)
    if not env_flag():
        print("Computer control is still off. To turn it on, add COMPUTER_CONTROL=true to .env and run:")
        print("  docker compose --profile sandbox up -d")
    elif DISABLED_FLAG.exists():
        print("File access is paused ('ragleap files off'). Run 'ragleap files on' to resume.")
    return 0


def cmd_list(args):
    items = read_raw()
    if not items:
        print("No folder grants yet. Add one: ragleap files add <folder> --name <name>")
        return 0
    now = datetime.now(timezone.utc)
    for raw in items:
        g, why = runner.parse_grant(raw)
        if g is None:
            print("%-14s invalid (%s)" % (str(raw.get("name") if isinstance(raw, dict) else raw)[:14], why))
            continue
        state = "expired" if g["_when"] is not None and g["_when"] <= now else "active"
        print("%-14s %-5s %-9s expires %-25s %s  [%s]" % (g["name"], g["mode"], g["purpose"], g["expires"] or "never", g["path"], state))
    return 0


def cmd_remove(args):
    items = read_raw()
    keep = [g for g in items if not (isinstance(g, dict) and g.get("name") == args.name)]
    if len(keep) == len(items):
        fail("No grant named '" + args.name + "'. See: ragleap files list")
    write_raw(keep)
    print("Removed '%s'. The AI can no longer see that folder." % args.name)
    return 0


def cmd_off(args):
    GRANTS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    DISABLED_FLAG.write_text("File access is paused. Run: ragleap files on\n", encoding="utf-8")
    print("File access is off now. It takes effect immediately; your grants are kept.")
    return 0


def cmd_on(args):
    if DISABLED_FLAG.exists():
        DISABLED_FLAG.unlink()
    print("File access resumed." if env_flag() else
          "The pause is removed, but computer control is still off. Add COMPUTER_CONTROL=true to .env and run: docker compose --profile sandbox up -d")
    return 0


def cmd_writing(args):
    GRANTS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    if args.state == "on":
        WRITE_FLAG.write_text("Writing is allowed for grants made with --mode write. Run: ragleap files writing off\n", encoding="utf-8")
        print("Writing is switched on. It still needs COMPUTER_CONTROL=true and a grant made with --mode write; "
              "every change also needs your approval. It takes effect immediately.")
    else:
        if WRITE_FLAG.exists():
            WRITE_FLAG.unlink()
        print("Writing is switched off. It takes effect immediately; your grants are kept.")
    return 0


def _trash_folder(name):
    for g in read_raw():
        if isinstance(g, dict) and g.get("name") == name and isinstance(g.get("path"), str):
            folder = g["path"]
            if os.path.islink(folder) or not os.path.isdir(folder):
                fail("The folder of '%s' is not there: %s" % (name, folder))
            return os.path.realpath(folder)
    fail("No grant named '" + name + "'. See: ragleap files list")


def _trash_entries(folder):
    """[(stamp, relative path, size)] oldest first. Real files only; links and other things are ignored."""
    root = os.path.join(folder, TRASH_NAME)
    if os.path.islink(root) or not os.path.isdir(root):
        return []
    found = []
    for stamp in sorted(os.listdir(root)):
        sdir = os.path.join(root, stamp)
        if not STAMP_RE.fullmatch(stamp) or os.path.islink(sdir) or not os.path.isdir(sdir):
            continue
        for dp, _dirs, files in os.walk(sdir, followlinks=False):
            for f in files:
                full = os.path.join(dp, f)
                st = os.lstat(full)
                if stat.S_ISREG(st.st_mode):
                    found.append((stamp, os.path.relpath(full, sdir).replace(os.sep, "/"), st.st_size))
    return found


def _stamp_time(stamp):
    return datetime.strptime(stamp[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def cmd_trash_list(args):
    entries = _trash_entries(_trash_folder(args.name))
    if not entries:
        print("The trash of '%s' is empty." % args.name)
        return 0
    for stamp, rel, size in entries[-200:]:
        print("%s  %9d bytes  %s" % (stamp, size, rel))
    if len(entries) > 200:
        print("(showing the newest 200 of %d)" % len(entries))
    print("Put one back: ragleap files trash restore %s <id>   (the id is the first column)" % args.name)
    return 0


def _clean_empty_dirs(path, stop):
    d = path
    while d.startswith(stop) and d != os.path.dirname(stop):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def cmd_trash_restore(args):
    folder = _trash_folder(args.name)
    found = [e for e in _trash_entries(folder) if e[0] == args.id]
    if len(found) != 1:
        fail("No single trashed file with the id '%s'. See: ragleap files trash list %s" % (args.id, args.name))
    stamp, rel, _size = found[0]
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or any(runner.name_denied(p) for p in parts):
        fail("That trashed file has a protected or unsafe name and is not restored by this command.")
    src = os.path.join(folder, TRASH_NAME, stamp, *parts)
    dest = os.path.join(folder, *parts)
    cur = folder
    for p in parts[:-1]:
        cur = os.path.join(cur, p)
        if os.path.islink(cur):
            fail("A link is in the way at " + os.path.relpath(cur, folder) + ". Nothing was changed.")
    if os.path.lexists(dest):
        if not args.replace:
            fail("A file is there now: " + rel + ". Add --replace to put the trashed version back; the current file "
                 "then goes into the trash, so nothing is lost. Nothing was changed.")
        if os.path.islink(dest) or not os.path.isfile(dest):
            fail("What is there now is not a normal file. Nothing was changed.")
    cur = folder
    for p in parts[:-1]:
        cur = os.path.join(cur, p)
        if not os.path.isdir(cur):
            os.mkdir(cur, 0o755)
    if os.path.lexists(dest):
        stamp2 = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + os.urandom(3).hex()
        keep = os.path.join(folder, TRASH_NAME, stamp2, *parts)
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        os.replace(dest, keep)
        print("The file that was there is now in the trash as " + stamp2)
    os.replace(src, dest)
    _clean_empty_dirs(os.path.dirname(src), os.path.join(folder, TRASH_NAME, stamp))
    print("Restored %s from %s." % (rel, stamp))
    return 0


def cmd_trash_empty(args):
    folder = _trash_folder(args.name)
    entries = _trash_entries(folder)
    if args.older_than:
        m = re.fullmatch(r"(\d+)([mhd])", args.older_than.strip().lower())
        if not m:
            fail("--older-than must look like 30m, 12h or 7d")
        cutoff = datetime.now(timezone.utc) - timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]: int(m.group(1))})
        entries = [e for e in entries if _stamp_time(e[0]) < cutoff]
    stamps = sorted({e[0] for e in entries})
    total = sum(e[2] for e in entries)
    if not stamps:
        print("Nothing to remove.")
        return 0
    if not args.yes:
        print("This would remove %d trashed version(s), %d bytes, for good. Nothing was removed. "
              "Run again with --yes to do it." % (len(stamps), total))
        return 0
    for stamp in stamps:
        sdir = os.path.join(folder, TRASH_NAME, stamp)
        if not os.path.islink(sdir):
            shutil.rmtree(sdir)
    print("Removed %d trashed version(s), %d bytes." % (len(stamps), total))
    return 0


def cmd_status(args):
    items = read_raw()
    print("COMPUTER_CONTROL in .env: " + ("true" if env_flag() else "off"))
    print("Paused with 'ragleap files off': " + ("yes" if DISABLED_FLAG.exists() else "no"))
    print("Writing switched on with 'ragleap files writing on': " + ("yes" if WRITE_FLAG.exists() else "no"))
    print("Grants: %d" % len(items))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="ragleap files", description="Choose which folders the AI employees may read.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="grant a folder")
    a.add_argument("path")
    a.add_argument("--name", required=True)
    a.add_argument("--mode", default="read", choices=["read", "write"])
    a.add_argument("--purpose", default="use", choices=["use", "knowledge"])
    a.add_argument("--expires", default=None, help="30m, 12h, 7d or a date")
    a.set_defaults(fn=cmd_add)
    sub.add_parser("list", help="show grants").set_defaults(fn=cmd_list)
    r = sub.add_parser("remove", help="remove a grant")
    r.add_argument("name")
    r.set_defaults(fn=cmd_remove)
    sub.add_parser("off", help="pause all file access now").set_defaults(fn=cmd_off)
    sub.add_parser("on", help="resume file access").set_defaults(fn=cmd_on)
    w = sub.add_parser("writing", help="allow or stop writing to folders granted with --mode write (off by default)")
    w.add_argument("state", choices=["on", "off"])
    w.set_defaults(fn=cmd_writing)
    t = sub.add_parser("trash", help="old versions kept when a file was changed or deleted (host only)")
    ts = t.add_subparsers(dest="tcmd", required=True)
    tl = ts.add_parser("list", help="show what is in the trash of a grant")
    tl.add_argument("name")
    tl.set_defaults(fn=cmd_trash_list)
    tr = ts.add_parser("restore", help="put one trashed version back")
    tr.add_argument("name")
    tr.add_argument("id")
    tr.add_argument("--replace", action="store_true", help="also when a file is there now (it goes into the trash)")
    tr.set_defaults(fn=cmd_trash_restore)
    te = ts.add_parser("empty", help="remove trashed versions for good (asks for --yes)")
    te.add_argument("name")
    te.add_argument("--older-than", default=None, help="only versions older than 30m, 12h or 7d")
    te.add_argument("--yes", action="store_true")
    te.set_defaults(fn=cmd_trash_empty)
    sub.add_parser("status", help="show the switches").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
