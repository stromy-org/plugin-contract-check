# plugin-contract-check (source)

This directory is the source of the **public** repository `stromy-org/plugin-contract-check`
(ORG-PLAN-413 B3P). It holds only the plugin entitlement verifier and the public key list. It has no
content, contacts or campaign material. It must be public because a public plugin repo cannot call a
private reusable workflow or read a private secret.

| File | Role |
|---|---|
| `verify_entitlements.py` | the verifier (standard library plus `ssh-keygen`) |
| `allowed_signers` | the pinned public keys; empty until activation |
| `.github/workflows/entitlement-contract.yml` | the reusable workflow a plugin calls, pinned by full commit SHA |
| `caller/entitlement-contract.yml` | the thin caller the stromy-org writer adds to each public plugin |
| tests | `scripts/tests/test_public_plugin_entitlements.py` in stromy-org: a throwaway keypair, every paired negative control |

## How a PR is judged

1. The private parent signs `entitlements.json` (SSH signature, namespace `stromy-entitlements`) and
   opens a PR in each public plugin with it and `entitlements.json.sig`.
2. A plugin PR runs the caller on `pull_request_target`, so GitHub executes the **base** branch's caller. The PR head is
   checked out as untrusted data.
3. The verifier checks: both files exist and the repo matches; the signature verifies under a pinned key;
   `seq` did not go down and `generated_at` is at most 45 days old; every server in `.agents/mcp.json` is entitled;
   the PR does not edit the caller, and the entitlements and signature change only together with a higher `seq`.
4. Anything missing, unreadable or unavailable is `failure`. Zero checks executed is `failure`.

The required status check is the context `entitlement-contract / verify`.

## Key rotation (add the new key, then remove the old)

1. Generate the new keypair. Add its public key to `allowed_signers` as an extra line and merge. Bump each
   plugin caller's pinned SHA (an operator change).
2. Switch `ENTITLEMENTS_SIGNING_KEY` in stromy-org to the new key and re-run the writer so every plugin is re-signed.
3. Only after every plugin verifies under the new key, remove the old line. A compromise runs the same
   sequence with step 3 done at once.

Activation (create this repo, mint the key, require the check on the public-plugin rulesets) is an operator
gate: ORG-PLAN-413 B3PA.
