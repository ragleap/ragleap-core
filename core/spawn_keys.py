"""Which signing keys do the start records of sub-agents depend on? Read-only; it never prints a key.

    docker exec ragleap-core-app-1 python -m core.spawn_keys

To rotate the key: set SPAWN_SIGNING_KEY_PREVIOUS to the old key, SPAWN_SIGNING_KEY to the new one and restart. Old records keep
verifying. Run this report; when the old key id shows `safe_to_retire: true` (no unfinished run depends on it), remove the
previous key from the settings. A record whose key is no longer configured is refused, so a run that still needs it stops.
"""
import json
import sys
from typing import Dict


def report() -> Dict:
    from core import agent_loop, autonomy
    current, keys = autonomy._signing_keys()
    counts = {c["key_id"]: c for c in agent_loop.start_key_counts()}
    out: Dict = {"current_key_id": current, "keys": []}
    for kid in sorted(set(counts) | set(keys)):
        c = counts.get(kid, {"total": 0, "unfinished": 0})
        out["keys"].append({"key_id": kid, "current": kid == current, "configured": kid in keys, "records": c["total"],
                            "unfinished_runs": c["unfinished"], "safe_to_retire": kid != current and c["unfinished"] == 0})
    out["unfinished_runs_whose_key_is_not_configured"] = sum(c["unfinished"] for k, c in counts.items() if k not in keys)
    return out


def main() -> int:
    print(json.dumps(report(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
