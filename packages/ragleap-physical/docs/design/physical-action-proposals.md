DRAFT: Physical-action proposal conventions for RagLeap MCP-style tools

# DRAFT — Physical-Action Proposal Conventions for RagLeap MCP-Style Tools

- Repository: `ragleap/ragleap-core`
- Package: `ragleap-physical`
- Working branch: `docs/physical-action-proposals-v2`
- Baseline commit: `e1b118ef60159331fbfa96f50dd6fbde22d1bdbe`
- Status: design and test-only contribution; simulation-only; no real-device validation.
- AI assistance disclosure: this document and its test additions were prepared with AI assistance; a human maintainer is responsible for review and merge decisions.

## 1. Purpose and scope

This design records the current proposal lifecycle and supplies a small test contract that can be run against the existing RagLeap Bridge and an independent test fake. Safety requirements below link to the package's primary invariant reference, [`docs/safety-invariants.md`](../safety-invariants.md); that invariant document is not duplicated here.

The Bridge currently accepts only simulated adapters and refuses a non-simulated adapter (`src/ragleap_physical/bridge.py::Bridge.__init__`). No statement in this document is a certification or guarantee for real hardware.

Status labels mean:

- **Implemented and tested**: current source and a named test provide evidence for the stated behavior.
- **Partially tested**: a related test exists, but does not cover the whole claim.
- **Untested**: no evidence was run or inspected for the exact claim.
- **Unresolved**: a maintainer decision or remaining gap is recorded explicitly.

## 2. Architecture

- `src/ragleap_physical/proposal.py` defines `Proposal`, its JSON-ready representation, canonical bytes, and SHA-256 digest.
- `src/ragleap_physical/bridge.py` owns proposal records, approvals, stop state, fresh-state checks, dry-run checks, adapter calls, outcome verification, and audit records.
- `src/ragleap_physical/adapters/base.py` defines the simulator-facing adapter operations and their refusal expectations; these adapter tests do not alone prove Bridge behavior.
- `tests/test_adapter_conformance.py` tests the adapter interface across the simulated and observe-only adapter fixtures. `tests/test_physical_action_proposals_conformance.py` separately tests the Bridge-level action-target contract.
- `benchmarks/approval_model.py` and `benchmarks/run_approval_model_extensions.py` are finite abstract/model replay evidence only; their own comments exclude general concurrency and real hardware dynamics.

## 3. Proposal lifecycle

1. **Create and validate.** `Bridge.propose()` observes the target, validates the requested actor and envelope conditions, constructs a `Proposal`, and stores its digest (`src/ragleap_physical/bridge.py::Bridge.propose`; `src/ragleap_physical/proposal.py::Proposal`).
2. **Approve.** `Bridge.approve()` checks that the proposal is pending, the approver is a named identity, the proposal digest matches the validated record, the requester is not the approver, and the proposal has not expired (`src/ragleap_physical/bridge.py::Bridge.approve`; `tests/test_bridge.py::test_approver_must_be_a_named_id`; `tests/test_known_gaps.py::test_self_approval_with_other_letter_case_is_refused`).
3. **Re-check at apply.** `Bridge.apply()` checks stop state, approval record, digest and expiry, then observes fresh state and re-checks state/static envelope conditions before dry-run or apply (`src/ragleap_physical/bridge.py::Bridge.apply`; `tests/test_safety_invariants.py::test_apply_checks_the_state_again_and_refuses_when_the_episode_limit_was_reached`, the direct S5 test).
4. **Dry-run, stop check, apply.** The Bridge validates the adapter dry-run result and checks stop again before calling adapter `apply()` (`src/ragleap_physical/bridge.py::Bridge.apply`; `tests/test_bridge.py::test_dry_run_speed_limit_blocks_before_any_motion`; `tests/test_known_gaps.py::test_no_adapter_command_when_stop_lands_during_the_dry_run`).
5. **Observe and report.** The Bridge advances in bounded simulation slices, obtains the final observation, checks the expected outcome, and reports/logs a verified or failed result (`src/ragleap_physical/bridge.py::Bridge._run_to`, `Bridge._outcome_ok`, and `Bridge.apply`; `tests/test_bridge.py::test_full_path_verifies_real_outcome`; `tests/test_bridge.py::test_missed_outcome_is_reported_failed_not_assumed_ok`).

The model-facing tools are read-only or proposal-only; the test `tests/test_tools.py::test_no_tool_can_apply_approve_stop_or_change_limits` is the package's direct evidence for that boundary, and `src/ragleap_physical/tools.py` defines those tool operations.

## 4. Safety requirements and evidence

For the complete S1–S13 invariant catalog and its test references, see [`docs/safety-invariants.md`](../safety-invariants.md). The following focused mapping avoids restating that catalog.

Proof status describes the evidence for the specific requirement and scope in that row. `Proved by deliberate break` means the evidence includes a deliberate, scoped break and a test that detects it; for S5, this is a post-approval state change, not a Bridge-source mutation. It does not imply a proof for other layers or unmutated conditions. `Tested, no direct deliberate break yet` means a relevant test exists without a recorded deliberate break for that requirement. `Untested` means no test evidence exists for that scope.

| Requirement | Proof status | Implementation and test evidence | Deliberate-break evidence |
| --- | --- | --- | --- |
| Cross-implementation proposal creation | Proved by deliberate break | Shared contract test `tests/test_physical_action_proposals_conformance.py::test_target_proposes_and_applies_with_approval`; the required proposal object and ID are asserted for both targets. | The `IndependentFakeTarget.propose()` return mutation failed the fake parameter; see the six independent-fake mutations in Section 8. |
| Cross-implementation approval acceptance | Proved by deliberate break | Shared contract test `tests/test_physical_action_proposals_conformance.py::test_target_proposes_and_applies_with_approval` checks the returned approval digest for both targets. | The fake approval-storage mutation caused apply to raise `ApprovalError`; see the six independent-fake mutations in Section 8. |
| Cross-implementation successful result reporting | Proved by deliberate break | Shared contract test `tests/test_physical_action_proposals_conformance.py::test_target_proposes_and_applies_with_approval` checks the result proposal ID, success flag, and accepted state for both targets. | The fake false-result mutation failed the success assertion; see the six independent-fake mutations in Section 8. |
| Canonical proposal representation | Tested, no direct deliberate break yet | `Proposal.canonical()` and `Proposal.digest()` in `src/ragleap_physical/proposal.py`; `tests/test_bridge.py::test_changed_after_approval_refused` (S2 in `docs/safety-invariants.md`). | No deliberate break of canonical serialization itself is recorded. |
| Proposal digest binding and tampering rejection | Proved by deliberate break | Bridge regression `tests/test_bridge.py::test_changed_after_approval_refused` (S2); shared contract test `tests/test_physical_action_proposals_conformance.py::test_target_rejects_tampering_after_approval`. | The Bridge digest-gate mutation and the independent-fake digest-check mutation failed their respective tests; see the separate evidence tables in Section 8. |
| Named-approver validation | Tested, no direct deliberate break yet | `Bridge.approve()` in `src/ragleap_physical/bridge.py`; `tests/test_bridge.py::test_approver_must_be_a_named_id` (S2). | No direct deliberate break for Bridge named-ID validation is recorded. |
| Self-approval refusal | Proved by deliberate break | `tests/test_known_gaps.py::test_self_approval_with_other_letter_case_is_refused` (S2); shared contract test `tests/test_physical_action_proposals_conformance.py::test_target_refuses_self_approval`. | The independent-fake requester/approver-check mutation failed the shared test; see the six independent-fake mutations in Section 8. This does not claim a Bridge-source self-approval mutation. |
| Expiry and single-use approval | Tested, no direct deliberate break yet | `tests/test_bridge.py::test_expired_approval_refused` and `tests/test_bridge.py::test_approval_is_single_use_replay_refused` (S2). | No Bridge mutation proof for expiry or single-use is recorded; the independent fake does not model expiry, and its contract test does not assert single-use. |
| Application requires a valid approval | Proved by deliberate break | `tests/test_bridge.py::test_apply_without_approval_refused` (S1); shared contract test `tests/test_physical_action_proposals_conformance.py::test_target_proposes_and_applies_with_approval` exercises approval before success. | The isolated Bridge approved-state mutation failed with the wrong exception; the fake approval-storage mutation also failed. See the separate evidence tables in Section 8. |
| Re-check safety conditions against fresh state at apply | Proved by deliberate break | `tests/test_safety_invariants.py::test_apply_checks_the_state_again_and_refuses_when_the_episode_limit_was_reached` is the S5 test named in `docs/safety-invariants.md`; it covers the episode-limit condition. | After proposal and approval, the test changes simulated state with `adapter.advance(61.0, 100000)`. `bridge.apply()` raises `EnvelopeViolation` with code `episode`, and `adapter.applied` remains empty. This is a state-change break, not a Bridge-source mutation. |
| Stop refusal in the cross-implementation contract | Proved by deliberate break | Shared contract test `tests/test_physical_action_proposals_conformance.py::test_target_refuses_apply_after_stop`; Bridge stop coverage includes `tests/test_bridge.py::test_stop_needs_no_approval_and_blocks_apply` (S8). | The independent-fake stopped-state mutation failed the shared test; see the six independent-fake mutations in Section 8. |
| Post-dry-run stop re-check | Proved by deliberate break | `tests/test_known_gaps.py::test_no_adapter_command_when_stop_lands_during_the_dry_run` (S8). | The Bridge stop-flag check immediately before adapter `apply()` was bypassed; the test failed when the stopped adapter was called. This is the third Bridge-source mutation in Section 8. |
| Initial and in-flight stop enforcement | Tested, no direct deliberate break yet | `tests/test_bridge.py::test_stop_needs_no_approval_and_blocks_apply`, `tests/test_bridge_limits.py::test_stop_during_apply_returns_at_once_and_halts_after_the_current_slice`, and `tests/test_adapter_stop_refusal.py::test_apply_refuses_while_stopped` (S8). | These tests cover the listed initial/in-flight cases; no direct deliberate break for these cases is recorded. |
| Dry-run validation before motion | Tested, no direct deliberate break yet | `tests/test_bridge.py::test_dry_run_speed_limit_blocks_before_any_motion` and `tests/test_adapter_conformance.py::test_dry_run_does_not_change_observation` (S6); workspace-path checks are listed under S6 in `docs/safety-invariants.md`. | No direct deliberate break for dry-run result validation is recorded. |
| Adapter interface expectations | Tested, no direct deliberate break yet | `tests/test_adapter_conformance.py::test_simulated_is_true` (S13) and `tests/test_adapter_conformance.py::test_dry_run_does_not_change_observation` (S6). The suite covers listed fixtures, not arbitrary future adapters. | No adapter-source mutation proof is recorded. |
| Final outcome verification | Tested, no direct deliberate break yet | `tests/test_bridge.py::test_full_path_verifies_real_outcome` and `tests/test_bridge.py::test_missed_outcome_is_reported_failed_not_assumed_ok`. “Real outcome” means the final observed simulator state, not a physical device. | No direct deliberate break for final outcome verification is recorded. |
| Auditability and failure handling | Tested, no direct deliberate break yet | `tests/test_bridge_limits.py::test_audit_list_is_capped_but_the_sink_gets_everything` (S11); `tests/test_bridge.py::test_adapter_error_goes_to_safe_state` and `tests/test_bridge.py::test_refused_applies_are_in_the_audit_log`. | No direct deliberate break for audit or safe-state handling is recorded. |
| Real-device safety or stopping behavior | Untested | `Bridge.__init__()` rejects non-simulated adapters; `tests/test_bridge.py::test_real_hardware_refused` (S13) tests only that software refusal. There is no real-device validation evidence in this contribution. | No real-device proof exists; simulated tests cannot establish real-hardware behavior. |

## 5. MCP specification mapping

The server advertises the protocol versions `2025-06-18` and `2025-03-26` in `src/ragleap_physical/mcp_server.py::SUPPORTED_PROTOCOLS`; `tests/test_mcp_server.py::test_initialize_and_version_choice` tests version choice. This implementation evidence is separate from the specification claims below.

Verified official specification references:

| Version | Exact section | Official source | Relevance |
|---|---|---|---|
| `2025-06-18` | Base Protocol → Lifecycle → Initialization → Version Negotiation | [Official MCP Lifecycle specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle#version-negotiation) | Client sends a supported version; server responds with the requested version if supported, otherwise another supported version. |
| `2025-03-26` | Base Protocol → Lifecycle → Initialization → Version Negotiation | [Official MCP Lifecycle specification](https://modelcontextprotocol.io/specification/2025-03-26/basic/lifecycle#version-negotiation) | Same version-negotiation behavior for this dated specification. |
| `2025-06-18` | Server Features → Tools → User Interaction Model | [Official MCP Tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#user-interaction-model) | The spec says applications should provide visibility and confirmation prompts; it also says protocol implementations are free to expose tools through different UI patterns. |
| `2025-03-26` | Server Features → Tools → User Interaction Model | [Official MCP Tools specification](https://modelcontextprotocol.io/specification/2025-03-26/server/tools#user-interaction-model) | The spec recommends a human in the loop able to deny tool invocations; it does not define RagLeap's proposal/approval ledger. |
| `2025-06-18` | Specification → Security and Trust & Safety → Tool Safety | [Official MCP specification overview](https://modelcontextprotocol.io/specification/2025-06-18) | Hosts must obtain explicit user consent before invoking any tool. This is general tool invocation consent, not an exact-proposal digest approval protocol. |

MCP defines tool discovery/invocation and general user-consent guidance, not a mandatory physical-action workflow or this package's identity, digest, expiry, single-use, revalidation, or stop rules (official [2025-06-18 Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) and [2025-03-26 Tools](https://modelcontextprotocol.io/specification/2025-03-26/server/tools)). Those controls are RagLeap-specific. **Untested:** this contribution did not exhaustively compare every MCP requirement or every transport behavior against the complete specification; the cited sections are the only MCP claims made here.

## 6. Reusable test-side conformance contract

`tests/test_physical_action_proposals_conformance.py` declares a test-only `PhysicalActionTarget` Protocol with only `propose`, `approve`, `apply`, and `stop`. It is not a production API. `IMPLEMENTATIONS` feeds the same parametrized test functions two targets:

1. **RagLeap Bridge**: `_bridge_target()` returns the actual `Bridge` created through `tests/helpers.py::make`, with the existing `FakeJointAdapter`.
2. **IndependentFakeTarget**: an in-memory implementation in the conformance test module. It creates a `Proposal`, tracks proposal and approval digests, rejects altered proposals, enforces stop-before-apply, and returns a deterministic verified result without calling `Bridge` or an adapter.

The shared tests exercise proposal creation/approval/success (`test_target_proposes_and_applies_with_approval`), self-approval refusal (`test_target_refuses_self_approval`), altered-after-approval rejection (`test_target_rejects_tampering_after_approval`), and stop-before-apply refusal (`test_target_refuses_apply_after_stop`). Their applicable implementation-specific refusal types use the same public errors (`ApprovalError` and `SafetyStop`). The test fake deliberately does not model expiry, fresh observations, envelope checks, dry-run physics, single-use policy beyond its simple local approval record, or audit behavior; those behaviors are **not applicable** to this small contract and remain covered only by RagLeap-specific tests when cited above.

Adapter-only assertions in `tests/test_adapter_conformance.py` remain separate. They do not stand in for these Bridge-level tests.

## 7. Test-to-requirement map

| Contract requirement | Shared test | Behaviors the test observes |
|---|---|---|
| Create a proposal and accept a named approver before successful application | `test_target_proposes_and_applies_with_approval` | Returned `Proposal`, nonempty ID, approval digest equals proposal digest, successful result is tied to that proposal. |
| Refuse requester self-approval | `test_target_refuses_self_approval` | Upper-case requester identity is rejected with `ApprovalError`. |
| Reject an altered proposal after approval | `test_target_rejects_tampering_after_approval` | Changing the reason after approval is rejected by `apply`. |
| Refuse application after stop | `test_target_refuses_apply_after_stop` | Applying after `stop()` raises `SafetyStop`. |
| Apply successfully after valid approval | `test_target_proposes_and_applies_with_approval` | Result identifies the same proposal, reports a successful outcome, and has an accepted final state. |

The apply-time fresh-state check is not part of this cross-implementation Protocol because the independent fake has no observation or envelope interface; the direct RagLeap test remains `tests/test_safety_invariants.py::test_apply_checks_the_state_again_and_refuses_when_the_episode_limit_was_reached` (S5).

## 8. Deliberate-break proof evidence

**Method:** for each row, copy the conformance test module into a unique temporary directory, alter one line only in that temporary copy, run `python -m pytest <temporary-copy>::<test-name> -q` with `PYTHONPATH=src;tests`, capture its nonzero exit and assertion/exception failure, discard the temporary copy, then run the unmodified test against both implementations. Production source and repository test files are not mutated. The temporary test copy retains the same target implementations and parametrized assertions as the checked-in suite.

The following mutations were applied only to temporary test copies; each corresponding test was run, and the original conformance module then passed in the full focused suite. **REAL** means the failure was actually observed from pytest; the tested implementations remain simulations or in-memory fakes, not hardware.

| Requirement | Corresponding test | One-line temporary mutation | Actual result |
|---|---|---|---|
| Proposal creation | `test_target_proposes_and_applies_with_approval` | Make `IndependentFakeTarget.propose()` return `None`. | **REAL** — exit 1; `1 failed, 1 passed`; `Proposal` type assertion failed. |
| Approval acceptance | `test_target_proposes_and_applies_with_approval` | Do not store the digest in `_approvals` in fake `approve()`. | **REAL** — exit 1; `1 failed, 1 passed`; fake `apply()` raised `ApprovalError: proposal has no matching approval`. |
| Self-approval refusal | `test_target_refuses_self_approval` | Remove the requester/approver equality condition in fake `approve()`. | **REAL** — exit 1; `1 failed, 1 passed`; pytest reported `DID NOT RAISE ApprovalError`. |
| Tampering rejection | `test_target_rejects_tampering_after_approval` | Disable the digest equality condition in fake `apply()`. | **REAL** — exit 1; `1 failed, 1 passed`; pytest reported `DID NOT RAISE ApprovalError`. |
| Stop refusal | `test_target_refuses_apply_after_stop` | Disable the stopped-state check in fake `apply()`. | **REAL** — exit 1; `1 failed, 1 passed`; pytest reported `DID NOT RAISE SafetyStop`. |
| Successful result | `test_target_proposes_and_applies_with_approval` | Return `outcome_ok=False` from fake `apply()`. | **REAL** — exit 1; `1 failed, 1 passed`; `outcome_ok is True` assertion failed. |

In every mutation run, the one passing parameter was the unmodified RagLeap Bridge; the failure was the deliberately mutated fake. This proves these shared tests detect the listed regressions in the test-side implementation. It is not mutation testing of production Bridge source. The temporary copies were removed after each run; the unmodified focused suite subsequently passed. No proof is claimed for any other cross-implementation fake requirement.

### Bridge-source mutation proofs

These are separate from the six `IndependentFakeTarget` mutations above. Each Bridge mutation was applied to an isolated temporary package copy (`src` and `tests`); an import-path check confirmed pytest loaded that copy. The mutation was restored in the copy before the regression tests were rerun, then the temporary copy was discarded. None of these mutations was made in the working branch.

| Requirement | Existing regression test | Temporary Bridge mutation | Actual result |
|---|---|---|---|
| Digest validation at apply | `tests/test_bridge.py::test_changed_after_approval_refused` | Bypass `if digest != rec["approval"]["digest"] or digest != rec["digest"]:`. | **REAL failure:** pytest reported `DID NOT RAISE ApprovalError`. |
| Approval required before apply | `tests/test_bridge.py::test_apply_without_approval_refused` | Change `if rec is None or rec["state"] != "approved":` to `if rec is None:`. | **REAL failure:** the test received `TypeError: 'NoneType' object is not subscriptable` instead of `ApprovalError`. |
| Stop check after dry-run | `tests/test_known_gaps.py::test_no_adapter_command_when_stop_lands_during_the_dry_run` | Bypass the Bridge stop-flag check immediately before adapter `apply()`. | **REAL failure:** the stopped adapter was called and raised `ValueError: the adapter is stopped: clear the stop first` instead of the expected `SafetyStop`. |

After restoring the temporary Bridge, those three isolated tests passed. The original working-tree regression run `python -m pytest tests/test_bridge.py tests/test_known_gaps.py -q` then reported **33 passed**. The original working-tree `bridge.py` SHA-256 remained `e69678a5327a583f4fbe2bafced78034df87e87429fc667b3fd38a7460ebb0d3`; the temporary package copy was discarded. These proofs establish detection for the three listed Bridge behaviors only. The Section 4 S5 state-change break is separate from these Bridge-source mutations; the other requirements marked as lacking direct deliberate breaks do not have Bridge-source mutation evidence.

## 9. Execution evidence and limitations

### Focused pytest suite

| Field | Evidence |
|---|---|
| Command | `$env:PYTHONPATH = 'src;tests'; python -m pytest tests/test_physical_action_proposals_doc.py tests/test_physical_action_proposals_conformance.py tests/test_safety_invariants.py tests/test_known_gaps.py tests/test_adapter_conformance.py tests/test_approval_model.py -q` |
| Operating system | Windows PowerShell. |
| Python / pytest | Python 3.11.4; pytest 8.3.5. |
| Baseline | `e1b118ef60159331fbfa96f50dd6fbde22d1bdbe`. |
| Previously recorded latest verified result | **REAL** — 192 passed, 108 skipped, 0 failed, 0 xfailed; 14.52 s. This is the corrected runtime from the preceding focused run. |
| Follow-up validation after this evidence audit | **REAL** — 193 passed, 108 skipped, 0 failed, 0 xfailed; 38.19 s. |

The `pytest -rs` rerun reported 96 skips because optional MuJoCo was not installed, and 12 skips because unknown target/command refusal cases apply only to control adapters, not observe-only adapters; it again reported 192 passed and 108 skipped. No xfail was in the focused selection: the #1338 `test_known_gaps.py` test passed on this baseline. Skips are not passes and do not validate real devices. The conformance fake and Bridge use simulated/in-memory behavior only; physical-device validation is **untested**.

Additional executed checks:

| Command | Result |
|---|---|
| `python -m pytest tests/test_bridge.py -q` | **REAL** — 30 passed, 0 failed; Python 3.11.4 / pytest 8.3.5. |
| `python -m pytest tests/test_lingering_close_review.py::test_f2_the_console_stop_is_not_held_up_by_a_client_that_never_sends_its_body -q -rxX` | **REAL** — 1 xfailed as expected, strict xfail reason `gap: issue 1367` (13.47 s). |
| `python benchmarks/approval_model.py` | **SANDBOX** — 30 reachable states, P1–P5 pass, no counterexample; abstract model only. |
| `python benchmarks/run_approval_model_extensions.py` | **SANDBOX** — 0 disagreements against this Bridge for selected replay; FakeJointAdapter, no hardware. |

### Approval-model replay

`benchmarks/approval_model.py` is a finite explicit-state model and `benchmarks/run_approval_model_extensions.py` replays selected extension scenarios against this checkout's Bridge. The replay is `SANDBOX` evidence only: the source documents finite scenarios, discrete expiry abstraction, incomplete general concurrency coverage, and no real hardware dynamics. The actual outcomes are recorded in the table above.

## 10. Known gaps and issue status

- **#1337 — self-approval identity comparison:** addressed in PR #1340; IDs are compared after Unicode normalization and case folding (`src/ragleap_physical/bridge.py::_same_identity`; `tests/test_known_gaps.py::test_self_approval_with_other_letter_case_is_refused`).
- **#1339 — stop during dry-run:** narrowed by PRs #1340 and #1366; the Bridge rechecks before its adapter apply call and the simulated adapter refuses while stopped (`tests/test_known_gaps.py::test_no_adapter_command_when_stop_lands_during_the_dry_run`). This is not a guarantee for arbitrary real hardware.
- **#1338 — approval across stop/clear:** the inspected baseline's `Bridge.clear_stop()` resets approved records to validated and clears their approval; `tests/test_known_gaps.py::test_stop_invalidates_an_approval_given_before_it` asserts refusal. In this checkout that test is not marked `xfail`, contrary to the test-file header and older notes. Whether the policy itself is the intended maintainer decision remains **unresolved**; verify the issue's current status before claiming it is open or resolved.
- **#1367 — console stop responsiveness:** remains an HTTP serving-layer gap, distinct from Bridge stop enforcement. `tests/test_lingering_close_review.py::test_f2_the_console_stop_is_not_held_up_by_a_client_that_never_sends_its_body` is marked `xfail(strict=True, reason="gap: issue 1367")`; a Bridge-level stop check does not solve a client blocking the console request handler.

## 11. Status summary

- Implemented and tested in the baseline: canonical proposal digests; named approver and self-approval checks; expiry and single-use behavior; apply-time S5 re-check; dry-run checks; simulated adapter conformance; outcome verification (`src/ragleap_physical/proposal.py`, `src/ragleap_physical/bridge.py`, and named tests in sections 4 and 7).
- Newly tested across two independent test targets: proposal, approval, self-approval refusal, altered proposal refusal, stop refusal, and success (`tests/test_physical_action_proposals_conformance.py`).
- Untested: real hardware; universal adapter behavior; the full MCP specification beyond the exact sections cited; assertions not included in the executed test command.
- Unresolved: maintainer policy for issue #1338; console stop responsiveness issue #1367 (`tests/test_lingering_close_review.py::test_f2_the_console_stop_is_not_held_up_by_a_client_that_never_sends_its_body`).
