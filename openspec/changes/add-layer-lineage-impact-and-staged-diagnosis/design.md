## Context

Facts read from source at dev `7557727` (2026-11-15):

- Parent and base are digest bindings. A `Palimpsestfile` names `FROM` and each `LAYER` by SHA-256, the build record keeps the ordered parents and a recipe digest, and a tag cannot move to different bytes (`build.py`). `run base --layer` checks that each layer names the previous one and that the root layer binds the base (`cli.py`).
- Consumers are pinned. A run ledger records its artifact digests; a Hub instance stores its ordered digests, base and manifest identity at creation; a published image is an immutable manifest with ordered memberships.
- Two caches exist and are keyed differently. The BuildKit build cache is keyed by build inputs and its solver decides which steps are reused. The OCI→SquashFS conversion cache is keyed per layer and includes a digest of the ordered preceding descriptors and DiffIDs, so a changed parent re-converts only the layers after it. A `Palimpsestfile` build has no step cache: the recipe runs as a whole.
- Compatibility evaluation today is `artifact_compatibility.check_artifact`: declared architecture, artifact kinds, chain and base binding, client backend. Its scope is `platform-metadata`.
- `base_migration.check` (M51-01, same branch) compares an old and a new base against the paths the kept layers touch and returns `clean` / `conflict` / `incomplete`. It has no listing adapter, so nothing in the product can produce its input yet.
- Nothing lists descendants, nothing rebuilds them, and no smoke-test result is stored.

## Goals / Non-Goals

**Goals**
- Show the affected child graph of a parent or base replacement before anything is done.
- Keep "existing lineage kept" and "new lineage to be built" visibly different, and keep rebuild, reconversion, transfer and deployment as four separate states.
- Make every compatibility statement say which stage it belongs to and mark what was not evaluated.

**Non-Goals**
- No automatic descendant rebuild, no orchestration, no change to running VMs or writable data.
- No admission decision: the plan informs; the existing runtime checks and the M5.1 acknowledgement gate (not implemented) decide.
- No ABI solver. No rule that forbids overlapping paths between ordered layers: an ordered OCI changeset may overwrite and delete lower paths, and that stays legal.
- No Hub API or schema change in this change.

## Decisions

1. **The plan is a pure function over metadata records.** `lineage.impact_plan(artifacts, changed=, replacement=, consumers=)` takes records the caller read; `inventory.lineage_impact` is the only reader. This keeps the rule that local store reads go through `inventory`, and lets a later Hub endpoint reuse the same function over SQL rows.
2. **Descendants come from recorded parent pointers.** A layer is a child of its `parent_digest`; a root layer is a child of its `base_image_digest`. The walk is breadth-first, deterministic (digest order within a depth), cycle-safe and bounded (5000 nodes, then `truncated: true`). The scope is stated in the plan (`recorded-local-metadata`): a descendant that exists only on a Hub or on another host is not listed and the plan does not claim completeness beyond its store. A stored layer whose own record names the changed artifact (its recorded ancestor list, or a chain that records that base) but which the walk cannot reach because a layer between them is not stored is listed under `unreached`; it is reported, not planned and not silently dropped.
3. **Each affected layer carries two blocks.**
   - `existing_lineage`: always `kept`, with the local runs, projects and retained instances that reference the layer. They stay bound to it. A retained instance is named by its validated identity only.
   - `new_lineage`: four separate states.
     - `rebuild` depends on how the layer was produced. A `Palimpsestfile` layer: `required`, `recipe: digest-recorded`, `recipe_edit: required`, `cache: not-reused` — only the recipe's digest is stored, the recipe names `FROM` and `LAYER` by digest so it must be edited to name the new parent, and the recipe runs as a whole. A committed or imported layer: `required`, `recipe: not-recorded`, `cache: unknown`. A BuildKit runtime pack: `inputs-unchanged`, `cache: decided-by-buildkit` — the base image is not an input of the Dockerfile build; the build command runs again and BuildKit's own cache decides which steps execute.
     - `reconversion`: `required` with `cache: keyed-by-base-image` for a BuildKit runtime pack (the pack key binds the base image, so another base is another pack); `not-applicable` for layers that are not a conversion of another artifact.
     - `transfer` and `deployment`: `not-inspected` (the local plan does not query a Hub or change a consumer).
   A state is never inferred from another: a recorded recipe digest does not mean the rebuild will succeed or reproduce the same bytes, a BuildKit cache hit says nothing about the pack cache and the reverse, and a rebuilt layer is neither transferred nor deployed.
4. **Staged diagnosis, no overall verdict.** `compatibility_stages.diagnose` returns five stages in dependence order with `passed` / `failed` / `unverified`, reasons, and evidence where a report exists:
   | Stage | Evidence today | Without evidence |
   | --- | --- | --- |
   | `structure` | kind, declared architecture, parent chain and base binding of the replacement and of each child, from stored records | `unverified` |
   | `filesystem` | a `base_migration.check` report (`clean` with complete inputs and no conflict → passed; any found conflict → failed with "needs an explicit risk acknowledgement", also when the check was otherwise incomplete; anything else → unverified), with the report digest | `unverified` |
   | `distribution_abi` | none; a declared release change is reported as a reason | always `unverified` |
   | `kernel_device` | none | always `unverified` |
   | `smoke_test` | none (no result store) | always `unverified` |
   The summary counts statuses; it does not collapse them. `failed` in the filesystem stage means "conflicts found", which the M5.1 contract turns into a required acknowledgement, not into "cannot run". Input of an unexpected shape leaves its stage `unverified`; it never raises and never passes.
5. **The structure stage passes only what the records show.** It is `failed` for a different kind or declared architecture and for a replacement that descends from the layer it replaces. It is `unverified` — not `failed`, not `passed` — when the replacement is bound to another base (a legitimate new lineage that metadata cannot vouch for), when either chain records no base, when the replacement's parent chain is not completely stored, and when a kind or architecture is not recorded.
6. **Diagnostics repeat no free text from artifacts.** Reasons are fixed sentences plus architectures and artifact kinds. Declared release names are repeated only when they are short plain names. No paths, recipe text, tokens or record contents appear in a plan or a warning.
7. **Exit code 0 for any computed plan.** A plan whose structure stage failed is still information the user asked for; only unusable input is an error.

## Reviewed, not implemented — needs approval

### Declared `provides` / `requires` / `conflicts`

Strata resolves layer definitions with Debian-style `Depends`, `Pre-Depends`, `Conflicts` and `Provides`, and forbids two software layers that contain the same path. What carries over and what does not:

- **Useful:** a place for the author to state requirements the structure cannot show (for example "needs glibc ≥ 2.38", "needs CUDA 12 userspace", "conflicts with layer family X"), so that a missing or contradicted declaration becomes a named reason in the `distribution_abi` stage instead of silence.
- **Does not carry over:** the path-disjointness rule. Palimpsest layers are ordered OCI changesets, where overwriting and deleting lower paths is the mechanism, not an error. Path overlap belongs to the filesystem stage as a finding to acknowledge.
- **Limits that must be stated with it:** a resolver checks declarations against declarations. It cannot show that an ABI is compatible, and an undeclared requirement stays invisible. A satisfied declaration set would move the stage from "nothing known" to "declarations consistent", never to "passed".
- **Cost and compatibility:** it needs a metadata schema (layer config or image annotations), a vocabulary and versioning rule, Hub registration validation, bundle/OCI export mapping, and a decision for every existing layer, none of which declares anything (they must read as `undeclared`, not as conflict-free). That is a schema and interoperability decision → owner approval before any implementation. Recommended first step if approved: optional, additive annotations with a closed vocabulary, evaluated only in diagnosis, never in admission.

### Composition lock

A lock would record how a requirement became an exact composition: requested references → resolved ordered digests (base and layers), per-layer conversion recipe identity (the existing `DerivedSquashFSKey` fields), builder and packer versions, target platform, and the diagnosis and structure-check report digests that were seen. Review:

- **Most inputs already exist** as separate records (build record, materialization receipt, published manifest, layer-cache consumer binding). The lock would be a single canonical document referencing them, so a composition can be re-resolved and compared later.
- **What it must not claim:** bit-identical rebuilds. Build records do not capture tool versions or the environment, and recipes download from the network. A lock pins inputs and tool identities; it does not make a recipe deterministic. A solver that produces a lock orders and selects; it does not prove ABI.
- **Open decisions:** where the lock lives (client file next to the project, Hub row, or OCI artifact with a `subject`), who may write it, whether running requires one, and how it relates to the published manifest, which already fixes order and digests. Until these are decided, the lock stays a proposal.

### Hub reverse lookup

The same pure plan can run over Hub rows, but it needs a new read endpoint (for example `GET /v1/layers/{digest}/descendants` and "published images and instances that use this layer"). It must reuse the existing visibility rules exactly (a caller must not learn that a hidden project's layer descends from theirs), bound its result, and be added to the policy action list. That is new API surface with an authorization decision → separate proposal.

## Risks / Trade-offs

- **Completeness can be over-read.** The plan lists what one store records. Mitigation: `scope` and the fixed statement say so; the CLI prints the statement.
- **`not-applicable` / `not-inspected` constants look like placeholders.** They are the honest value for a local SquashFS graph, and they keep the four actions separate in the schema so that a Hub-side plan can fill them without a format change.
- **The filesystem stage has no producer yet.** Until a listing adapter exists (M51-02 onward) it is `unverified` in every real plan. That is accurate, and it is the reason the stage exists.
- **Two status vocabularies.** `check_artifact` keeps `compatible` / `incompatible` / `unknown` for existing consumers and is not an input of the staged diagnosis: its result also reflects the client host and backend, which are not structure. Changing the existing result would be a breaking change and is not proposed.
