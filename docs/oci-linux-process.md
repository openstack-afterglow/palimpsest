# Linux OCI process metadata

Palimpsest's OCI intake supports exactly Linux amd64. The legacy image-config
field `ArgsEscaped` may be absent, null, `false` or `true`; a present non-null
value must be a JSON boolean. Numbers (including 0 and 1), strings, arrays
and objects are rejected.

On this Linux path, either boolean leaves the process unchanged:
`Entrypoint` followed by `Cmd` forms the literal argument vector. Palimpsest
does not join, split, unquote, unescape or insert a shell. Spaces, quotes,
backslashes and shell metacharacters remain ordinary argument bytes. An image
that explicitly names a shell still executes that image-supplied shell; this
rule does not prevent the image program from interpreting its own arguments.

## Basis and boundaries

The [OCI image specification v1.1.1](https://github.com/opencontainers/image-spec/blob/v1.1.1/config.md)
describes `ArgsEscaped` as a deprecated Windows compatibility field and permits
null for optional properties. Moby v28 marks the field
[Windows-specific](https://github.com/moby/moby/blob/v28.0.0/api/types/container/config.go)
and its [Linux process construction](https://github.com/moby/moby/blob/v28.0.0/daemon/oci_linux.go)
copies the executable and argument vector. The Linux interpretation above is
based on these contracts, not a Windows command-line implementation or a
promise of complete Docker compatibility.

All existing process type/size/bootability checks and the Linux amd64 image
platform gate remain. Windows and other architectures are not enabled.
The original config bytes, descriptor digest and source snapshot binding
remain authoritative and are not rewritten. Configs differing only in the
boolean can produce the same canonical process but different source identities.
There is no process schema, boot-plan version, materialization recipe or guest
binary change. Existing defaults and explicit `--user` provenance are retained.
PID 1 protection, capability removal, no-new-privileges, securebits, seccomp,
filesystem validation and VM-exclusive root ownership are unchanged.

## Focused verification

Use the process and source regressions first, then the affected image, request,
store and adapter consumers described in [testing](testing.md). The original
NGINX archive has an independently opted-in native proof. A parser pass is not
service qualification: require real default startup, readiness, public exec,
actual root comparison, PID 1 access refusal and normal stop/removal. The proof
uses network none and does not test external HTTP reachability. Preserve the
original failed qualification and unchanged archive even after a later pass.

## Verified parser / failed NGINX checkpoint — 2026-09-08

At [866d5e6](https://github.com/openstack-afterglow/palimpsest/commit/866d5e675b3c31f81f790468aa6d228251a13c88),
the focused nine-module selection passed 661 tests locally (22.77 s), with one
Linux-only openat2 skip. The same selection passed all 662 tests on the exact
pushed SHA on `pieroot-server` (191.59 s). Ruff, architecture and lane inventory
checks passed; code and native plan received independent Astra approval after
Sol implementation. This is not a full-suite result.

The unchanged original NGINX archive native test **failed** (20.71 s). It passed
the former ArgsEscaped intake rejection and console observations show actual
root transition, isolated workload startup and original entrypoint execution.
NGINX then reported permission denied opening `/var/log/nginx/error.log` and
exited with status 1 before detached startup returned. Readiness, subsequent
public exec/root-report comparison/PID 1 refusal and stop/rm did not pass.
Console root markers alone are not the missing authenticated public comparison.

Read-only listing of the preserved lower image shows `error.log -> /dev/stderr`
and `access.log -> /dev/stdout`. Source inspection of
`guest/stage1/init.c:prepare_workload_mount_boundary` and
`safe_workload_dev_entries` shows a private mode-0755 `/dev` with exactly six
devices (`null`, `zero`, `full`, `random`, `urandom`, `tty`) and no standard-stream
symlinks. The missing paths are a source-based explanation consistent with
the observed error, not a traced-syscall proof. Link creation alone has not
been qualified; inherited-FD reopening permissions may also matter. No image,
user, capability or guest device-policy change was made to hide the failure.

The new NGINX definition `hub-nginx-a6ccaf95` (UUID
`475bf9eb-fe11-45fb-8f27-d689d71b0586`) is shut off with autostart disabled.
Its runtime at `/tmp/p-hub-nginx-193990e3`, root and logs remain preserved,
alongside the prior inactive Redis definition and all earlier retained data.
The original NGINX archive SHA256 remains
`64e3f9852293174971bbd81e54bf0093896fc0ee525c9b0494763d5208ffa029`;
other original Hub and existing built-image hashes also remain unchanged.

Independent plan review first caught postflight checks conditional on test
success. The revised wrapper runs every preservation observation even after
failure, preserves the primary test exit code and passed 225 mocked-shell
scenarios. In the actual NGINX failure it correctly reported the extra inactive
definition while still verifying the old Redis UUID/state/autostart, all source
hashes, zero active VMs and exact clean product SHA. No failed VM was deleted
to satisfy inventory checks.

The separate existing built-image cold exec regression also **failed** at this
same SHA (5.51 s), during launch with
`OCI runtime ancestor changed during verification`. An independently approved
cold-only plan preserved both earlier inactive definitions. The new `exec-cli`
definition (UUID `415ba534-2eaf-4135-92dc-f033311e7b47`) is now also shut off
with autostart disabled, with failure evidence at `/tmp/p-execcli-9d99c203`.
Postflight retained the primary failure and checked all 17 observations: the
extra-definition inventory check failed as expected; all four archive hashes,
both previously preserved UUID/state/autostart checks, zero active domains,
exact SHA and clean tree passed. No retry or manual cleanup followed.

`oci_host.py:verify_runtime_parent` rechecks ancestor identity, ownership, mode
and ctime around ACL verification and again through held/visible paths. Which
ancestor or field changed in this attempt is not known; normal directory
activity is a candidate, not a confirmed cause. The parser patch did not change
this verifier. The failed cold test is not a new default-run pass, and earlier
successful cold results cannot substitute for it. A narrow diagnostic and a
reviewed retry plan preserving the existing `exec-cli` name/definition are
required before another native attempt.

The next guest task must define and independently review a narrow standard-I/O
path contract with positive/negative C and native tests before changing the
device allowlist or packaged ELF. Direct registry-reference intake remains a
separate unimplemented task. Neither the parser change nor this failed service
test constitutes a new application build or full Gate 2 qualification.
