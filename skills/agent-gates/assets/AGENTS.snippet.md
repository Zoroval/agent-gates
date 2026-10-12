## Intelligence change boundary

Changes to model selection, prompts, context builders, reasoning settings, model-output schemas, retrieval, judges or model-written reports are protected by the Intelligence Change Gate. The protected paths are listed in `.github/intelligence-policy.json`.

- Before you push, run the gate locally:
  `python tools/validate_intelligence_change.py --base origin/<default-branch> --head HEAD`
- If it fails and you did not mean to change model behavior, revert the protected part of your diff.
- If you did mean to, run an eval of baseline and candidate on every affected stage, using the golden dataset. Commit one eval report (JSON manifest) under `evals/experiments/`. Start it with `--scaffold-experiment`. Use real numbers. The gate recomputes pass and fail.
- Run the experiment on the final version of the change. Any later edit to a protected file changes the fingerprint and makes the evidence stale.
- Never commit raw customer data, traces or keys in a manifest. Reference private artifacts by SHA-256.
- Never edit the policy, the gate, the workflow or an override to make a PR pass.
- If you cannot produce evidence, stop and report the fingerprint, stages and risk to the user. Only a listed approver can authorize a break-glass override, for one PR number. Do not create an override file unless the user explicitly authorizes it.
- An override is a recorded decision, not a pass. Never describe it as a pass.
