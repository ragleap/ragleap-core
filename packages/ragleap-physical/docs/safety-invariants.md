# Safety invariants of ragleap-physical

Pre-alpha, simulation only. Nothing here controls real hardware. This page is a list of what the bridge is meant to guarantee, where the code enforces it, and which tests check it today. It is **not** a safety certification, and a passing test is an example, not a proof. The approval model in `benchmarks/approval_model.py` is a hand-written abstract model, not a proof about the code either.

Every test named below is checked by `tests/test_safety_invariants.py`: if a test is renamed or removed, that test fails until this page is updated. Open gaps are listed with their issue number; a gap that is marked as an expected failure in the tests says so.

### S1 The AI never moves a device
A model can observe, propose and read results. Nothing it can call sends a command to a device. Enforced in `tools.py` and `bridge.py` (capability levels).
- `tests/test_bridge.py::test_observe_level_cannot_propose_or_apply`
- `tests/test_bridge.py::test_recommend_level_cannot_apply`
- `tests/test_bridge.py::test_apply_without_approval_refused`
- `tests/test_tools.py::test_propose_returns_proposal_and_moves_nothing`

### S2 An approval binds to the exact change, once, with a named person
An approval is tied to the digest of the exact proposal, can be used once, expires, and names a person. The requester cannot approve its own proposal; ids are compared after Unicode normalisation and case folding. The console takes the approver from the token, never from the request. Enforced in `bridge.py` and `console.py`.
- `tests/test_bridge.py::test_changed_after_approval_refused`
- `tests/test_bridge.py::test_approval_is_single_use_replay_refused`
- `tests/test_bridge.py::test_expired_approval_refused`
- `tests/test_bridge.py::test_approver_must_be_a_named_id`
- `tests/test_approval_model.py::test_requester_self_approval_transition_matches_real_bridge`
- `tests/test_known_gaps.py::test_self_approval_with_other_letter_case_is_refused`
- `tests/test_console.py::test_the_approver_comes_from_the_token_never_from_the_request`
- `tests/test_console.py::test_a_wrong_digest_is_refused_and_nothing_is_applied`

**A stop invalidates earlier approvals (#1338).** Ending a stop revokes every approval given before the clear; after `clear_stop()` the action needs a fresh approval by a named person, and a clear when nothing was stopped changes nothing.
- `tests/test_known_gaps.py::test_stop_invalidates_an_approval_given_before_it`
- `tests/test_stop_revokes_approvals.py::test_a_fresh_approval_after_the_clear_applies`
- `tests/test_stop_revokes_approvals.py::test_clear_without_a_stop_does_not_revoke`

### S3 Limits live only in the bridge and the envelope
A caller cannot change a limit through a tool, a view or an input dictionary. Enforced in `envelope.py` and `bridge.py`.
- `tests/test_envelope.py::test_caller_cannot_change_envelope_through_input_dict`
- `tests/test_bridge.py::test_envelope_view_cannot_change_limits`
- `tests/test_tools.py::test_envelope_view_tool_cannot_change_limits`

### S4 Default deny: reject, never clamp
A value outside a limit, an unknown target or an unknown command is refused. The bridge never quietly changes a value to fit. Enforced in `envelope.py` and in each adapter.
- `tests/test_bridge.py::test_out_of_range_refused_no_clamping`
- `tests/test_bridge.py::test_unknown_target_refused`
- `tests/test_envelope.py::test_clamping_not_allowed`
- `tests/test_arm3_mujoco_review.py::test_arm3_mujoco_review_m1_a_target_beyond_the_actuator_range_is_refused_not_silently_clamped`

### S5 Checks run at propose and again at apply, on fresh state
The limits are checked when a change is proposed and again, on a fresh observation, when it is applied. Enforced in `bridge.py` (`apply`). Until this page was written there was no test that the second check fires; `tests/test_safety_invariants.py` now has one.
- `tests/test_safety_invariants.py::test_apply_checks_the_state_again_and_refuses_when_the_episode_limit_was_reached`

### S6 The dry run runs on a copy and the whole path is checked
The dry run reports every joint and the path extremes without changing the device. The workspace box is checked on the plan and again while the motion runs. Enforced in `bridge.py`, `envelope.py` and the adapters.
- `tests/test_workspace.py::test_end_pose_outside_the_box_is_refused_before_any_motion`
- `tests/test_workspace.py::test_a_mid_path_excursion_is_refused_even_if_the_end_pose_is_fine`
- `tests/test_workspace.py::test_runtime_monitor_stops_a_real_motion_that_leaves_the_box_even_if_the_dry_run_lied`
- `tests/test_bridge.py::test_dry_run_speed_limit_blocks_before_any_motion`
- `tests/test_adapter_conformance.py::test_dry_run_does_not_change_observation`

### S7 Fail closed
If something needed for a check is missing or unexpected, the answer is no, and an unexpected error goes to the safe state. Enforced in `bridge.py` and `envelope.py`.
- `tests/test_workspace.py::test_unknown_workspace_fails_closed`
- `tests/test_workspace.py::test_dry_run_that_reports_no_positions_fails_closed`
- `tests/test_bridge.py::test_adapter_error_goes_to_safe_state`

### S8 Stop needs no approval and never waits for a running command
`stop()` sets its flag at once, needs no capability and no approval, and a running apply notices it between slices. An adapter refuses a command while it is stopped. Enforced in `bridge.py` and in the simulated adapters.
- `tests/test_bridge.py::test_stop_needs_no_approval_and_blocks_apply`
- `tests/test_bridge_limits.py::test_stop_during_apply_returns_at_once_and_halts_after_the_current_slice`
- `tests/test_bridge_limits.py::test_stop_does_not_depend_on_capability`
- `tests/test_known_gaps.py::test_no_adapter_command_when_stop_lands_during_the_dry_run`
- `tests/test_adapter_stop_refusal.py::test_apply_refuses_while_stopped`

**Open gaps:** the operator console answers one request at a time, so a stop request can wait up to 10 s behind a client that never finishes its request (#1367). Expected failure: `tests/test_lingering_close_review.py::test_f2_the_console_stop_is_not_held_up_by_a_client_that_never_sends_its_body`. A stop from another thread can still land between the bridge's last check and the adapter call; the adapter refusal narrows this for the simulated adapters (#1339). Real devices are out of scope.

### S9 Device data is untrusted
Text from a device is never trusted: unknown units are dropped, sizes are capped, duplicate ids are dropped, control and bidirectional characters are refused, and results are marked untrusted. Enforced in `observation.py` and `tools.py`.
- `tests/test_tools.py::test_results_are_json_and_marked_untrusted`
- `tests/test_bridge.py::test_injection_text_in_device_data_never_reaches_output`
- `tests/test_observation.py::test_unknown_unit_dropped`
- `tests/test_hardening.py::test_d_duplicate_ids_are_all_dropped`
- `tests/test_hardening.py::test_e_control_and_bidi_characters_are_refused_in_reason`

### S10 Exactly four tools, none can approve, apply, stop or change a limit
Enforced in `tools.py`.
- `tests/test_tools.py::test_exactly_four_tools_at_recommend_and_three_at_observe`
- `tests/test_tools.py::test_no_tool_can_apply_approve_stop_or_change_limits`

### S11 Memory is capped
Pending proposals, kept records, the in-memory audit list and the number of raw items examined are all limited. Enforced in `bridge.py` and `observation.py`.
- `tests/test_bridge_limits.py::test_old_records_are_evicted_but_a_replay_is_still_refused`
- `tests/test_bridge_limits.py::test_live_proposals_are_never_evicted`
- `tests/test_bridge_limits.py::test_audit_list_is_capped_but_the_sink_gets_everything`
- `tests/test_observation.py::test_size_cap_truncates`
- `tests/test_hardening.py::test_c_the_size_cap_counts_invalid_items_too`

### S12 Changing a tool is a human action, never a command
Replacing a worn tool in the simulated machine is a method of the adapter. It is not one of the four tools and cannot be called by a model. **There is no test written for this invariant alone;** it follows from S10:
- `tests/test_tools.py::test_no_tool_can_apply_approve_stop_or_change_limits`

### S13 Only simulated adapters are accepted
The bridge refuses an adapter that does not say it is simulated. Enforced in `bridge.py`.
- `tests/test_bridge.py::test_real_hardware_refused`
- `tests/test_adapter_conformance.py::test_simulated_is_true`
