# Phase 2 live smoke: PASS after provisioning fix

## Successful run

Run `b97f217b2b8e` passed, based on the user-supplied console log and the saved
`spikes/01_instabox_openswe/artifacts/phase2_run/verification_report.json` and
`patch.diff` inspected in the workspace.

- Model: `9router:cx/gpt-6.1-sol` (Dev), through local 9Router.
- Baseline: 4/4 tests passed.
- Patch: 2 files, fixing the discount bounds and half-up rounding, plus one new
  regression-test file; existing tests/configuration were unchanged.
- Red on a separate clean snapshot VM: pytest exit 1, 19 cases.
- Green on another clean snapshot VM: pytest exit 0, 19/19 passed.
- 15 new cases, including 14 that failed on baseline and passed with the patch.
- Verdict: PASS; no regressions or static gate findings.
- Cleanup: console log confirms deletion of agent VM, Red VM, Green VM and snapshot.

This establishes the Phase 2 fixture bugfix/verification path. It does not yet
establish MetaGPT/ChatDev integration or a full AI SDLC workflow.

## Earlier provisioning failure

Evidence: user-supplied console log, 2026-11-13.

- Run ID: `481939c72cf2`
- Machine ID: `m_agq5ckcam4greq7xqutcs5asea`
- API preflight and machine creation succeeded.
- Provisioning failed before the coding agent ran: Python could not create the
  pytest virtualenv at `/opt/phase2_pytest_481939c72cf2` (permission denied).
- Cleanup succeeded; the log confirms that the created VM was deleted.

The runner now creates the pytest virtualenv at `/data/phase2_pytest_<run_id>`,
on the writable persistent disk and outside the repository checkout. This keeps
it in the pre-agent snapshot and prevents workspace cleanup from removing it.
No elevated guest permissions are required.

The offline regression checks the missing-system-pytest bootstrap sequence,
including installation and pytest discovery through the new Python path.
The successful run above supersedes this earlier provisioning failure.
