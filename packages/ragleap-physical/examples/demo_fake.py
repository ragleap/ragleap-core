"""Walk through the whole path with the built-in simulator. No hardware, no network."""
from ragleap_physical import Bridge, Capability, Envelope, ExpectedOutcome
from ragleap_physical.adapters import FakeJointAdapter

ENVELOPE = {
    "envelope_version": 0, "allowed_commands": ["set_position_target"],
    "targets": {"joint_1": {"pos_min": -1.0, "pos_max": 1.0, "vel_max": 2.0, "force_max": 5.0}},
    "rate": {"max_commands_per_s": 5}, "episode": {"max_sim_time_s": 60, "max_steps": 100000},
    "on_violation": "reject",
}

bridge = Bridge(FakeJointAdapter(), Envelope(ENVELOPE), org_id="org_demo", max_capability=Capability.CONTROL)
print("observe:", bridge.observe().entities[0])
proposal = bridge.propose(requested_by="employee:ops", target="joint_1", command="set_position_target",
                          value=0.5, unit="rad", expected_outcome=ExpectedOutcome("joint_1", "pos", 0.45, 0.55, "rad", 5.0))
print("proposal digest:", proposal.digest())
bridge.approve(proposal, "person:alice")   # a named person approves the exact proposal
result = bridge.apply(proposal)
print("result:", result.state, "outcome_ok =", result.outcome_ok)
try:
    bridge.propose(requested_by="employee:ops", target="joint_1", command="set_position_target", value=5.0,
                   unit="rad", expected_outcome=ExpectedOutcome("joint_1", "pos", 4.0, 6.0, "rad", 5.0))
except Exception as exc:
    print("out-of-range value refused:", exc)
