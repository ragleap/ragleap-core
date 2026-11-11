"""Run the issue #1337-#1339 SANDBOX replay against this checkout's Bridge."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from helpers import make, propose
from approval_model import replay_multi_proposal


def main():
    print("Evidence label: SANDBOX (FakeJointAdapter; no hardware)")
    print("Bridge SHA-256 source:", __import__("hashlib").sha256(
        (ROOT / "src/ragleap_physical/bridge.py").read_bytes()).hexdigest())
    findings = replay_multi_proposal(make, propose)
    for item in findings:
        print("DISAGREEMENT", item)
    print("Total disagreements:", len(findings))
    print("P6: pending design decision; expected disagreement until decision/implementation.")
    print("P7: stop-during-dry_run and stop-at-adapter-call hooks are covered by pytest tests.")
    print("P8: report only actor forms accepted by Bridge's actor grammar.")


if __name__ == "__main__":
    main()
