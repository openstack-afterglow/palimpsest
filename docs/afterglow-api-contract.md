# Afterglow consumer API contract

This is the integration contract for an external dashboard or SDK using the
Palimpsest Hub `/v1` API. The Hub console at `/app` uses the same routes.
Afterglow's own UI and monitoring implementation belong to the Afterglow project;
this document does not claim that they have shipped. No new instance projection
fields, list pagination or lifecycle service changes are introduced here.

## Discovery and authentication

- Resolve the deployment's canonical HTTPS origin. Its machine-readable contract
  is `GET /openapi.json`; interactive documentation is at `/docs`.
  The lab public document is
  <https://palimpsest.dmslab.re.kr/openapi.json>.
- Send `X-Auth-Token: <original project-scoped Keystone token>` and
  `X-Project-Id: <that token's immutable project ID>` on consumer requests.
  The header asserts the token's scope; it does not switch projects. Obtain a
  new project-scoped token to change projects. Never put tokens in URLs, browser
  storage, build arguments, Dockerfiles, descriptive metadata or evidence logs.
- Read `GET /v1/me` before enabling controls. Check returned `project_id`,
  `user_id`, roles and capabilities. Current authority is checked again at the
  server, not inferred from a visible public image or a cached identity.
- Missing/expired/revoked credentials return 401. A mismatched project assertion
  returns 403. Insufficient authority returns 403; invisible or privately owned
  resources may instead return 404 to avoid revealing their existence.
- Project members with write authority can admit builds and instances. Control
  requires the exact creator's `packages:manage_own` authority or project
  `packages:manage` authority. A different ordinary member cannot control the
  owner's instance. Starting an accepted instance is creator-only. Renewing a
  dev instance's delegation is creator-only. Reader inventory does not grant
  control. Private-image instance rows also retain exact-creator visibility.
- Native package keys, image keys and delegated worker/guest credentials do not
  replace the original human token for instance lifecycle and image-job routes.
  Published-image reads separately support image API keys and public anonymous
  reads; do not treat this as lifecycle authority.

## Consumer endpoints

Paths below are relative to the origin. JSON admission responses are durable
receipts, not proof that the guest is ready. Keep returned IDs outside list
windows. Request schemas, bounds and operation IDs are in `/openapi.json`.

| Method | Endpoint | Meaning |
| --- | --- | --- |
| GET | `/v1/me` | Original identity and freshly evaluated capabilities |
| GET | `/v1/instance-options` | Authorized boot images, flavors, networks, SSH key names and limits; no allocation |
| POST | `/v1/git-builds` | Admit a pinned Git Dockerfile build; 202 with job receipt |
| POST | `/v1/registry-imports` | Admit an OCI registry import; 202 with job receipt |
| POST | `/v1/build-contexts` | Upload a bounded tar context; 201 |
| POST | `/v1/dockerfile-builds` | Admit a Dockerfile with optional existing context digest; 202 |
| GET | `/v1/image-jobs` | Own jobs in the asserted project, newest first; `limit` 1–100 |
| GET | `/v1/image-jobs/{job_id}` | Own durable admission, progress and publication receipt |
| GET | `/v1/published-images` | Authorized image catalog; `limit` 1–200, `offset` ≥0 |
| GET | `/v1/published-images/{image_id}` | Image identity, public manifest projection and ordered layers |
| GET | `/v1/published-images/{image_id}/blobs/{digest}` | Authorized immutable payload download |
| PUT | `/v1/published-images/blobs/{digest}` | Stage local publication bytes; not needed for worker builds |
| POST | `/v1/published-images` | Publish staged local content; 201; separate from instance creation |
| POST | `/v1/published-images/extensions` | Publish a derived image from an authorized base; 201 |
| GET | `/v1/published-images/quota` | Caller publication usage and limits |
| POST | `/v1/instances` | Accept immutable intent and prepare an owned boot volume; 202 |
| GET | `/v1/instances` | Committed project rows, subject to private visibility; newest first, `limit` 1–100 |
| GET | `/v1/instances/{instance_id}` | Observe/reconcile provider state; busy reads return last committed row |
| POST | `/v1/instances/{instance_id}/advance` | Explicit `create-server` or `delete-volume`; 202 |
| POST | `/v1/instances/{instance_id}/stop` | Preserve writable data and request power-off; service or dev; 202 |
| POST | `/v1/instances/{instance_id}/resume` | Start/unshelve a dev instance; 202; not a service restart API |
| POST | `/v1/instances/{instance_id}/lifecycle/renew` | Creator replaces dev auto-shelve delegation; 200 |
| GET | `/v1/instances/{instance_id}/connection` | Fixed/floating addresses and SSH key name; 200 |
| DELETE | `/v1/instances/{instance_id}` | Explicit server and exclusive boot-volume removal; 202 |

Instance lists have no offset, status/lifecycle filter or updated-since query.
They are not a complete fleet event feed. Retain known IDs and fetch each one;
never infer deletion from absence in a bounded list. There is no public session
count, grace deadline, observation-error or trust-expiry summary in the instance
projection. The renewal response supplies `trust_expires_at`; keep that receipt.

### Build → playbook → VM

All three image-job admissions require a UUID `request_id`, `name`, `os_family`
and `os_version`. Choose immutable audience `private`, `internal` or `public`.
OS fields are declarations, not detected/attested distribution facts. Current
worker builds target `linux/amd64`.

Git also requires `repo_url` (operator-allowed HTTPS host, no embedded
credentials) and a full 40-hex `revision`. `context_subdir` defaults to `.` and
`dockerfile_path` to `Dockerfile`; paths stay inside the checkout. Optional
`credential_ref` names an operator-managed credential, never its value. The Git
route is rate-limited to six requests per hour; respect 429, do not loop.

Registry requires `reference`. Dockerfile requires `dockerfile`, with optional
`context_digest`, `target` and bounded `build_args`. For local files, first POST
raw tar bytes to `/v1/build-contexts` with `Content-Type: application/x-tar`,
`X-Content-SHA256: sha256:<64hex>` and an actual `Content-Length`; use the returned
context digest in the same user's/project's Dockerfile admission. Browser Fetch
sets Content-Length for a file body; do not try to set that forbidden header in
JavaScript. The console accepts an existing context digest; it does not upload
contexts. The SDK/API supports context upload.

Job statuses are `queued` → `building` → `complete` or `error`; an expired or
failed build can become `error`. Only `complete` plus ordered `output_digests`
permits output selection. `error_code` describes terminal failure; there is no
automatic requeue. Successful current worker jobs publish their image as part
of completion: read `published_image_id`, then GET that image. Historical jobs
can be complete with outputs but no published image; do not invent a catalog
image ID. Catalog runtime values are redacted; do not rebuild an exact manifest
from its public projection.

A console playbook is this image's ordered layer stack, source and audience,
not an Ansible playbook. Select the completed output, inspect provenance, then
use its own `image_job_id` and exact ordered `output_digests` for creation. The
current Hub requires an eligible own completed job in the asserted project;
merely seeing somebody else's published image does not make it launchable.
Creation also requires name, boot `image_id`, flavor/network IDs, `boot_gib` and
`writable_gib` (strictly smaller than boot size). Optional command/readiness
arrays use argv, not shell text. The console gets provider choices through
`/instance-options`; its truncated choices are not a full provider inventory.

## Instance state and allowed progression

`status` is the Hub state, not Nova `provider_status`. `provider_phase`,
`server_create_attempted`, IDs and `error_code` further constrain actions.
A state name alone never grants permission or proves that a provider task ended.
The server remains authoritative for admission and ownership.

| Hub status | Meaning and supported next steps |
| --- | --- |
| `uncertain` | Admission/provider effect cannot yet be proved. Observe exact ID; do not issue another create or blindly repeat an effect. A later observation may settle `booting`, `error` or deletion. |
| `booting` | Boot-volume preparation or guest startup. Wait while `provider_phase=volume_preparing`. Only `volume_ready` + `server_create_attempted=false` + boot volume ID permits explicit `advance(create-server)`. An authenticated first guest result becomes `ready` or `error`; timeout can become `error`. Stop requires an observed owned server; delete remains explicit. |
| `ready` | First execution/probe succeeded, or latest dev restore succeeded. Stop → `stopping`; delete → `deleting`. Dev idle policy may produce `shelved`; service is not automatically shelved. |
| `error` | Execution, preparation, provider or restore failed. Inspect `error_code` and IDs. Stop only with an observed owned server; delete is explicit. Do not silently rebuild. A dev bare VM without retained restore state is not proof of restored workload. |
| `stopping` | Durable stop intent. Observe until Nova SHUTOFF yields `stopped`. Do not auto-repeat stop. Dev resume is server-gated; an in-flight stop can be refused with 409. |
| `stopped` | Explicit power-off; upper is preserved on the exclusive boot disk. Dev with retained state can resume → `resumed`; service is delete-only after stop. |
| `shelved` | Dev idle offload preserves the upper. Resume → `resumed` (or `error/resume_state_missing`); delete is explicit. Stop refuses a shelved instance: resume first if power-off is wanted. |
| `resumed` | Dev provider returned ACTIVE; restored guest report is pending. A new report produces `ready` or `error`. Observe; do not repeat resume. Stop/delete remain explicit. |
| `deleting` | Durable delete intent. Observe exact server/volume settlement. Only `delete_volume_ready` and original request/boot volume IDs permit `advance(delete-volume)`. A response lost after volume deletion is not permission to repeat it. |
| `deleted` | Terminal Hub receipt. Do not launch/resume this row. Create a new intent for a new VM; published lower images remain separate. |

`advance` body is `{ "operation": "create-server" | "delete-volume",
"request_id": "<original create UUID>", "boot_volume_id": "<accepted volume UUID>" }`.
It never substitutes another volume or another create intent. Stop, resume and
renew take no JSON body. Delete removes this instance's server and exclusive
writable boot disk, not its published image/lower layers or another VM's upper.
Connection output does not supply a guest username or trusted SSH host keys
(`ssh.user` and `ssh.host_keys` are null); establish those separately.

### First result versus resumed execution

`result` and `result_recorded_at` are the sealed first guest execution receipt.
`resume_result` is the latest retained dev restore report. Never overwrite or
label the first result as the resumed command's output. Show both with distinct
labels. `ready` is point-in-time execution/probe evidence, not ongoing health;
service restart observations live in the existing `restart` projection.

A modern CPU dev guest retains restore state after a successful command even
without a readiness probe. Resume runs the command again: commands requiring
once-only behavior must guard themselves in preserved storage. Older no-probe,
failed-first-result or other non-restorable guests can return as a bare VM with
`status=error` and `error_code=resume_state_missing`, rather than stay `resumed`
forever. This means provider access was restored but workload restore cannot be
proved. Do not report success or retry resume to manufacture a guest result.
A stopped instance without retained state is refused instead; delete it when
finished. Existing older guests are not upgraded in place.

## Replay and uncertain outcomes

- Assign a UUID before image-job admission or instance creation. Persist it with
  the exact immutable descriptor and asserted project. A same-ID/same-descriptor
  replay returns the original receipt after current authorization; it does not
  allocate a second job/instance. A changed descriptor with that ID returns 409.
  Image-job receipts are exact-user scoped; instance create IDs are project
  scoped with additional private-visibility checks. Do not share IDs between
  users. `advance` binds the original create UUID and boot volume as above.
- Stop/resume/delete/renew have no request-id replay guarantee. Admission replay
  safety does not extend to arbitrary provider effects. Never assign a new UUID
  to hide an uncertain create, nor replay a destructive request after a timeout.
- **409 + `Retry-After: 10`** on instance admission means **not applied**: another
  holder or an in-flight lifecycle action prevented this request. Wait at least
  ten seconds, reconcile, then let the user explicitly decide whether to retry.
  Other 409s can mean permanent state/descriptor conflict and may carry no retry
  header. In particular, a generic 409 is not proof of no prior effect.
- **503 + `Retry-After: 1`** is a lock timeout outside that definite admission
  refusal and means **outcome unknown** for a mutation. Wait at least one second
  before reconciliation; do not infer not-applied or blindly resend. Other 5xx,
  timeouts, disconnection and transport errors likewise require reconciliation.
- A busy `GET /instances/{id}` returns 200 with **last committed state** after
  bounded admission, without provider reconciliation. Repeated 200s therefore do
  not prove freshness or an idle provider. When free, this GET can reconcile and
  commit SQL observations, but it dispatches no provider lifecycle mutation.
  The list route reads committed SQL without provider reconciliation.
- The console sends one mutation per explicit submission/click, locks concurrent
  controls for that instance, surfaces retry guidance and holds mutations for a
  numeric Retry-After interval. Refresh/Check provider reads do not repeat an
  action. After an unconfirmed reply, an unchanged explicit image-job resubmission
  in the same connection reuses its UUID. A confirmed admission releases that
  UUID so a later explicit submission can build again, including after a failed
  job. Disconnect clears tab memory, including those UUIDs; reconcile
  retained receipts before submitting the same work from another connection.

## Polling and monitoring guidance

Keep an ID-based registry in Afterglow. For a known queued/building job or
booting/stopping/resumed/deleting instance, start with reads every 10 seconds.
Do not use synchronized tight pollers; use bounded backoff/jitter for errors and
honor Retry-After. Give the user a manual refresh and stop after a declared idle
deadline; a stalled poll is not a reason to resend a mutation. The console stops
after ten minutes without observed progress and resumes reads on Refresh.

Observe ready dev instances at a suitably slower interval if the monitor must
notice automatic shelve. There is no push stream or automatic stable-state poll
in the console: Refresh or Check provider updates a ready/shelved row. Monitor
all known IDs separately from the bounded newest-first lists. Compare
`updated_at`, state, phase and result receipts, not only the HTTP status. On
uncertain deletion, retain the resource in monitoring and escalate unresolved
provider ownership/state instead of allocating a replacement or refunding quota.

## OpenAPI verification and limits

The application generates OpenAPI from the running routes. Consumer request
bodies are machine-readable, including Git/Dockerfile/registry descriptors and
bounded binary-context input. Some instance/job responses are untyped JSON;
OpenAPI does not encode every status, transition, error header or replay rule.
Use this narrative and consumer contract tests for those semantics.

Local verification must GET `/openapi.json` from this application and compare
required paths, methods, request schemas and Keystone security declarations to
`app.openapi()`. Before deployment acceptance, fetch the public `/openapi.json`
and compare the same consumer paths and referenced component schemas against
the reviewed release, without credentials or mutating requests. Capture actual
UTC time, release SHA, schema digest, mismatches and scope of the check. Do not
infer the deployed version from `info.version` (the API version is not the
package release). A reachable matching schema proves discovery, not provider
behavior, authorization denial or build/lifecycle acceptance.

The initial public-document read in this implementation session was retained
and compared at **2026-11-17T12:03:40Z** against this working tree's generated
contract: 17 consumer path objects, all 42 component schemas and security
schemes matched. Canonically reserialized capture SHA-256:
`c346c1613c3dbc00965e5045dbcce889a5494d0b5766d5c0b5489792000b0670`.
The deployed package behind that earlier capture was not identified; the fixed
OpenAPI version is not release evidence. Later public GETs returned 503 during
the separately controlled 0.3.31 rollout. Current post-rollout schema equality is
**not live-verified** here; a post-rollout receipt must establish it. Local tests
and a synthetic browser exercise do not replace the separately approved 0.3.32
console/provider journey.
