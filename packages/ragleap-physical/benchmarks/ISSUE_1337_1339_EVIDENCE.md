# Issues #1337 to #1339: approval and stop model, evidence

Labels: **REAL** = a real run on the real source tree with versions written down. **SANDBOX** = a run in a scratch environment; treat it as reported, not checked.

## What this change adds
- `benchmarks/approval_model.py`: three new properties next to the original P1 to P5 model. P6: a stop invalidates outstanding approvals (a **pending design decision**, issue #1338, not a settled rule). P7: no adapter command after a stop, with the two race slots as explicit events. P8: identity comparison is insensitive to letter case and Unicode compatibility form. Also a replay of named multi-proposal sequences against the real Bridge.
- `benchmarks/run_approval_model_extensions.py`: prints every disagreement between the model and the Bridge, by sequence and step.
- `tests/test_approval_model_extensions.py`: ten tests (8 pass, 2 are strict expected failures for the open gaps #1338 and #1339).
- In the original part of the model file only the module docstring (one sentence replaced), two import lines and the position of the `__main__` guard changed. P1 to P5 are untouched.

## REAL run
Base: `main` at 6052248d, worktree. Python 3.10.12, pytest 9.2.0. Installed extra: `test` only, so tests that need MuJoCo or the MCP SDK are skipped.

| Run | Result |
|---|---|
| Full suite before this change | 488 passed, 146 skipped, 1 xfailed |
| New tests alone | 8 passed, 2 xfailed |
| Full suite after this change | 496 passed, 146 skipped, 3 xfailed |
| Replay runner (post-fix Bridge, `bridge.py` SHA-256 starts `d170d564b7614f1d`) | 2 disagreements, both P6; none for P8 |

The two disagreements: `P6-stop-invalidates-A` (step 5, apply after stop and clear: model rejects, Bridge accepts) and `P6-stop-invalidates-multiple` (step 7, same with two approvals). Both wait for the maintainer's decision on #1338.

## Deliberate breaks (REAL, each restored afterwards)
| Break | Tests that failed |
|---|---|
| Model: `identity_key` no longer folds case | the identity property test, the replay P8 test |
| Model: a stop no longer invalidates approvals | the P6 model test |
| Model: an adapter command after a stop is not recorded | the P7 model test |
| Bridge: the stop re-check before the first adapter call removed | `test_p7_bridge_rechecks_stop_after_dry_run_before_adapter_command` |
| Bridge: exact-text identity comparison restored | `test_p8_bridge_rejects_case_variant_self_approval`, the replay P8 test |

## Earlier finding (SANDBOX, before the Bridge fixes of #1337 and #1339)
On the pre-fix package the replay reported three disagreements: `P8-case-self-approval` (step 2, approve as `EMPLOYEE:OPS`: model rejects, Bridge accepted), and the two P6 sequences above. Two stop races also reproduced: a stop landing during the dry run, and a stop landing at the adapter call. The first is fixed since then; the second is the open part of #1339 (an adapter has to refuse a command while it is stopped; the simulated stand-ins do not yet).

## Not run
Python 3.10 beyond CI, MuJoCo, the MCP SDK extra, Windows. The model abstracts time and general thread scheduling: the races are deterministic hook injections at two named slots, not an exploration of all schedules.
