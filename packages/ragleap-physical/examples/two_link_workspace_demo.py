
"""Demonstrate workspace acceptance and rejection with a two-link arm.

Uses the built-in simulator only. No hardware or external dependencies.
"""

from ragleap_physical import (
    Bridge,
    Capability,
    Envelope,
    EnvelopeViolation,
    ExpectedOutcome,
)
from ragleap_physical.adapters import FakeArmAdapter


JOINT_LIMITS = {
    "pos_min": -1.2,
    "pos_max": 1.2,
    "vel_max": 10.0,
    "force_max": 10.0,
}

WORKSPACE = {
    "x": [0.3, 1.0],
    "y": [-0.1, 0.1],
    "z": [0.4, 1.5],
}

CONFIG = {
    "envelope_version": 0,
    "allowed_commands": ["set_position_target"],
    "targets": {
        "joint_1": JOINT_LIMITS,
        "joint_2": JOINT_LIMITS,
    },
    "workspace": WORKSPACE,
    "rate": {"max_commands_per_s": 5},
    "episode": {"max_sim_time_s": 60, "max_steps": 100000},
    "on_violation": "reject",
}


def create_bridge():
    """Create a bridge connected to a fresh simulated two-link arm."""
    adapter = FakeArmAdapter()

    bridge = Bridge(
        adapter,
        Envelope(CONFIG),
        org_id="org_demo",
        max_capability=Capability.CONTROL,
    )

    return bridge, adapter


def demonstrate_inside_workspace():
    print("\n--- Scenario 1: Target inside workspace ---")

    bridge, adapter = create_bridge()

    proposal = bridge.propose(
        requested_by="employee:ops",
        target="joint_1",
        command="set_position_target",
        value=0.5,
        unit="rad",
        expected_outcome=ExpectedOutcome(
            "joint_1", "pos", 0.45, 0.55, "rad", 6.0
        ),
    )

    bridge.approve(proposal, "person:alice")
    result = bridge.apply(proposal)

    print("Movement accepted.")
    print("Result:", result.state)
    print("Outcome verified:", result.outcome_ok)
    print("Final joint positions:", adapter.pos)


def demonstrate_outside_workspace():
    print("\n--- Scenario 2: Target outside workspace ---")

    bridge, adapter = create_bridge()

    proposal = bridge.propose(
        requested_by="employee:ops",
        target="joint_1",
        command="set_position_target",
        value=1.0,
        unit="rad",
        expected_outcome=ExpectedOutcome(
            "joint_1", "pos", 0.95, 1.05, "rad", 6.0
        ),
    )

    bridge.approve(proposal, "person:alice")

    try:
        bridge.apply(proposal)
    except EnvelopeViolation as exc:
        print("Movement rejected.")
        print("Reason:", exc)
        print("Violation code:", exc.code)
        print("Any movement applied:", bool(adapter.applied))


if __name__ == "__main__":
    print("Two-link arm workspace demonstration")
    demonstrate_inside_workspace()
    demonstrate_outside_workspace()
