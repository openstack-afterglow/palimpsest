## Why

M52-01 through M52-04 call for importing the part of `devcontainer.json` that maps to a Palimpsest development VM. A successful import must say what was converted and must not silently discard runtime requirements or grant access to the host.

## What Changes

- Add explicit, revision-pinned Dev Container config selection to the Hub Git Dockerfile workflow. Convert a closed subset into a bounded build descriptor and a separate development profile; refuse unsupported or conflicting settings before executing the build.
- Bind the profile to the exact private job, immutable publication and new development VM. Use the same admission and diagnostics on CLI, SDK, Hub API and dashboard. Managed SSH and console access must enter the built workspace, not the carrier shell.
- Preserve Palimpsest source policy, `.plmignore`, authorization, existing VM mode and data ownership. Do not import mounts, lifecycle commands, Features, privilege settings, or implicit network exposure.
- This subset does not deliver the M52-02 selectable-volume clause. Keep M52-02 and GATE open until the owner approves and qualifies a volume contract or records an approved roadmap amendment under a new ID.
- Specify portable and separately authorized live qualification, including stop/resume data preservation and examples of actual refusals.

## Capabilities

### New Capabilities

- `devcontainer-config`: bounded configuration intake, deterministic conversion, immutable provenance, and development VM admission.

### Modified Capabilities

None.

## Impact

Future implementation touches Git staging, image job/publish admission, the instance guest runner and access path, and the CLI/SDK/API/dashboard callers. This change is design only under `spec.md:19`; M4/M5.1 remain prerequisites to implementation and qualification. It grants no build, resource, release, deployment or live test authorization. No Docker CLI/API compatibility, Docker-equivalent behavior, or browser IDE is implied.
