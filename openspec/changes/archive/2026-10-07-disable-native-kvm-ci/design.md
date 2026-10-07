## Context

Initial 2026-10-07 inspection: the protected environment requires reviewer `jung-geun`; repository flag was `true`, but both pinned HTTPS kernel/config inputs were absent at repository and environment scope. Merely setting `false` also failed the former Test verdict and skipped release publication. The owner explicitly permits disablement. Work in an isolated dev clone to preserve unrelated shared changes.

## Goals / Non-Goals

**Goals:** stop automatic native attempts and their approval waits; keep ordinary validation and publication verification; distinguish policy acceptance from native qualification; make re-enabled proof fail closed.

**Non-Goals:** repair/host kernels, remove environment reviewers, weaken pins or receipts, change cloud resources, publish a tag/release, or merge main without its owner.

## Decisions

1. Choose explicit opt-out rather than automatic approval because approval cannot fix missing inputs. Configure canonical lowercase `false`. The actual Test Bash verdict uses literal strings; Release retains GitHub's standard case-insensitive expression comparisons. Missing/non-boolean flags deny; do not add unrelated flag parsing machinery or claim uppercase aliases are rejected by GitHub equality.
2. Test verdict accepts exactly `true:success` and `false:skipped`. Keep trusted-ref/event/repository gating and rename its displayed check so skipped proof is not labeled successful proof.
3. Release publication explicitly checks `!cancelled()`, trusted repository, push/tag, successful verify, and those same two states. A status function is necessary to override GitHub's implicit `success()` after the intentionally skipped dependency. Keep both dependencies, least permissions and PyPI environment.
4. Preserve native protection and implementation for opt-in. Native execution still requires `true`; failed, cancelled, missing or unexpectedly skipped enabled proof cannot authorize publication. Emit an explicit disabled/no-proof notice.
5. Validate real shell verdicts plus actual workflow-condition boundary combinations. Observe a genuine trusted dev push and its pending deployments after strict PR checks; no native/release execution for test convenience.
6. The first genuine dev run accepted native opt-out but exposed a separate macOS descendant-test setup race: its oversized archive was visible before the descendant PID existed. Correct only the existing fixture's causal order (spawn, record PID, then exceed size); retain production code, timeouts, limits and the real descendant-kill assertion. Preserve the observed failing run alongside corrected local and strict integration evidence.

## Risks / Trade-offs

- Releases made with this policy disabled have no current native KVM qualification; docs and job notices must say so. Portable proof is not a substitute.
- `false` affects the repository globally, but an older main/tag workflow still has its old gate. Prepare owner-managed promotion rather than claim a variable write updates source on every ref.
- Re-enabling requires exact pinned HTTPS inputs and existing protected approval; no automatic credential or environment changes are bundled.
- Before/after workflow shapes and event populations can differ. Record samples honestly; do not claim performance improvement from a single run or manufacture twenty runs.
