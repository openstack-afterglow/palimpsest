## ADDED Requirements

### Requirement: Dev Container source selection is explicit and bounded

The Hub SHALL import `devcontainer.json` only when an authorized caller explicitly selects that source mode on an admitted, immutable Git revision. It SHALL use the discovery order `.devcontainer/devcontainer.json`, `.devcontainer.json`, then an unambiguous `.devcontainer/<one-level-name>/devcontainer.json`, unless the caller selects one exact supported config path. It SHALL neither merge configs nor fall back on invalid input. It SHALL parse one UTF-8 JSONC object, allowing comments but not duplicates or trailing commas, using limits enforced during parsing: 64 KiB input, depth 16 with the root at depth 1, 1,024 values plus object keys counted together, 128 members per object/array, 128 UTF-8 bytes per decoded key and 4,096 UTF-8 bytes per decoded string. It SHALL resolve Dockerfile and context relative to the selected config and confine them to the pinned snapshot. It SHALL not traverse symlinks in config, Dockerfile, context directory or selected ignore paths.

The converter SHALL refuse `devcontainer_unsupported` for any decoded `${` sequence in an otherwise accepted key or string value. The only exception is one complete leading `${localWorkspaceFolder}` token in `build.dockerfile`, `build.context`, `dockerFile` or `context`, followed by end of string or `/` and a valid relative suffix. This token denotes the pinned checkout root. Bare or incomplete sequences and additional tokens SHALL fail; `$` without `{` is literal. Diagnostics SHALL omit the value and use the known field pointer or, for untrusted names, the nearest known parent and source position.

#### Scenario: Nested config selects the repository context

- **GIVEN** a pinned checkout with `.devcontainer/devcontainer.json`, a Dockerfile alongside it, and `build.context` equal to `..`
- **WHEN** the caller explicitly requests import without a path
- **THEN** the Hub selects that config and resolves the Dockerfile inside `.devcontainer` and the build context at the checkout root
- **AND** the same paths are used for validation, staging and provenance.

#### Scenario: Discovery cannot choose one config

- **GIVEN** neither higher-precedence path exists and two one-level candidates exist
- **WHEN** the caller omits the path
- **THEN** the Hub returns `devcontainer_ambiguous` and requires an explicit selection before any job is queued.

#### Scenario: Input exceeds its authority or budget

- **WHEN** a config uses duplicate keys, exceeds parser bounds, selects a symlinked file/path, or resolves a path outside the checkout
- **THEN** the Hub refuses without registering a context, queueing a build, creating an image or allocating a VM.

#### Scenario: A host variable cannot pass through as a literal

- **GIVEN** an otherwise valid configuration with `"remoteEnv": {"X": "${localEnv:PATH}"}`
- **WHEN** the converter checks the decoded input
- **THEN** it returns `devcontainer_unsupported` at `/remoteEnv` with the offending source position, without echoing the value or reading the environment
- **AND** it does not emit a plan or queue a build.

### Requirement: The converter admits a closed and explicit subset

A versioned pure converter SHALL map only `name`, the modern `build.dockerfile` and `build.context` pair or legacy `dockerFile` and `context` pair, literal `build.args` and `build.target`, `workspaceFolder`, `containerUser`, `remoteUser`, `containerEnv`, `remoteEnv` and policy-enabled `forwardPorts` under the design's field, precedence and size rules. It SHALL return a deterministic typed plan or bounded structured refusals, not a partial executable plan. A valid `$schema` annotation and empty `customizations` are the only ignored fields and SHALL produce notices. All other fields, unsupported values and competing explicit native build inputs SHALL cause clear refusals consistently across CLI, SDK, API and dashboard. No import SHALL execute or accept lifecycle hooks, Features, Docker CLI/API or Compose behavior, image-label metadata, arbitrary mounts, privilege or network overrides, secret inputs, or browser IDE settings.

#### Scenario: Literal values project separately onto build and dev settings

- **WHEN** a valid configuration selects a Dockerfile/target and supplies distinct runtime and remote user/env settings
- **THEN** the build descriptor gets only typed build inputs
- **AND** the dev profile applies workload settings to workload/readiness and remote overlays only to managed developer sessions; these settings override permitted OCI defaults under runtime policy, not the native `command` or `readiness_argv` selections
- **AND** the instance request exposes no cwd, user, env or port-mapping overrides.

#### Scenario: A recognized but unsupported setting cannot be masked

- **WHEN** an input supplies `postCreateCommand`, `features`, `mounts`, `runArgs`, a nonempty tool customization, non-permitted port request, or an unknown field
- **THEN** the API reports a stable refusal with the known field pointer or, for an unknown/untrusted key, the nearest known parent and source position, without echoing that key or the value
- **AND** no override, generic confirmation or alternate client turns it into a successful conversion.

#### Scenario: An annotation has no runtime effect

- **WHEN** an otherwise valid config includes an absolute `$schema` URI and `customizations: {}`
- **THEN** the caller receives explicit ignored notices and neither value is fetched, installed or passed to a worker or runtime.

### Requirement: Converted builds retain authority, context policy and provenance

Converted builds SHALL use the current authorized Dockerfile job and private publication path. The source adapter SHALL apply bounded Dockerfile/context ignore selection and additional context-root `.plmignore` exclusions before packaging, without weakening mandatory exclusions or changing guest capture filtering. The Hub SHALL bind the exact commit, selected config digest, converter and policy versions, Dockerfile, context and effective ignore identities, normalized plan and ordered output to the private job and publication receipt. It SHALL not publish the raw config, private profile, runtime environment overrides or secrets. Neither caller-authored metadata nor unbound layer bytes SHALL be authoritative.

The worker SHALL reject the presence of `devcontainer.metadata` in the final verified OCI image config, after build and before output staging or publication. It SHALL report `devcontainer_image_metadata` through leased terminal job failure handling, not as an admission-time HTTP error.

#### Scenario: Excluded source stays excluded

- **WHEN** context `.plmignore` excludes a file and the selected config is itself excluded by the packing rules
- **THEN** the config is still validated from the pinned checkout, but neither excluded file is available to Dockerfile `COPY`
- **AND** the exact staged context digest and ignore policy identity are recorded.

#### Scenario: An exact request replays without a new build

- **WHEN** the same authorized creator repeats a previously admitted request UUID with the same pinned source and native descriptor
- **THEN** the Hub returns the existing receipt without fetching, reinterpreting its profile or queueing a second job
- **AND** a changed descriptor or different principal cannot take over that receipt.

#### Scenario: Accepted build later produces labeled image metadata

- **GIVEN** a config passed admission with HTTP 202
- **WHEN** the worker finds `devcontainer.metadata` in the exact verified built image config, including an empty or inherited label
- **THEN** the job settles with `status: "error"` and `error_code: "devcontainer_image_metadata"` before output staging or publication, with no launchable output
- **AND** authorized image-job reads expose the terminal receipt with HTTP 200, SDK preserves the status and code, and CLI and dashboard show the failure without the label value.

### Requirement: A managed profile creates only an explicit development VM

The Hub SHALL require explicit `lifecycle=dev`, the exact managed job/publication/profile binding and the normal scoped CPU VM, key, network, image-integrity and quota checks before create. It SHALL refuse service/production and alternate selectors that bypass the binding. It SHALL keep supported settings out of the carrier and trusted bootstrap environment. Managed SSH and owner-authorized console sessions SHALL authenticate and enter the verified workspace overlay at the accepted cwd with the non-root session identity and scoped environment. If enabled, forwards SHALL require explicit user activation through verified SSH and bind only the approved client-loopback TCP ports. Stop/resume SHALL retain and reuse the same instance-private upper and sealed settings without rerunning conversion; no external volume or older VM is imported.

#### Scenario: Developer access reaches the new workspace

- **GIVEN** the selected completed private build and a qualified ready development instance
- **WHEN** its owner opens managed SSH or an authorized console session
- **THEN** the session runs inside the exact workload root with the selected cwd, effective non-root user and session-only overrides
- **AND** a provider running state, carrier login or console log alone is not successful developer access.

#### Scenario: Forwarding cannot expose a service implicitly

- **WHEN** a config requests a supported port and the port policy permits it
- **THEN** the request changes no provider rule or public listener
- **AND** only explicit authorized session activation can establish that exact client-loopback forward; a conflict fails instead of widening or remapping it.

#### Scenario: Stop and resume preserve private work

- **GIVEN** edits in the writable root of a ready managed VM
- **WHEN** its owner stops or shelves it and then resumes the same VM
- **THEN** existing lifecycle checks restore the exact bound lower stack, writable data and profile
- **AND** the Hub does not fetch a fresh config, reformat the upper, execute hooks or attach another volume.

### Requirement: Qualification distinguishes source results from live evidence

The change SHALL document the supported subset, VM-vs-container differences, precedence, trust and retention boundaries, limits and actionable examples of actual refusals. Source/unit checks SHALL NOT count as live build-to-VM, login or preservation evidence. Live qualification SHALL require separate scoped owner approval and the roadmap GATE.

M52-02 and its GATE SHALL remain open until the selectable-volume requirement is qualified under an approved exact-owner contract or an owner-approved roadmap amendment under a new ID records its disposition and applicable evidence. An instance-private upper is not selectable-volume evidence.

Live qualification SHALL include the dashboard journey on the owner-designated surface and SHALL NOT change the separate M4-15 dashboard requirement.

#### Scenario: Source checks pass but no live approval exists

- **WHEN** the design validates or the later converter tests pass without the approved live journey
- **THEN** the work reports only the evidence obtained and makes no M5.2 completion claim.
