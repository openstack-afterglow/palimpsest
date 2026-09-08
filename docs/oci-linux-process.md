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
