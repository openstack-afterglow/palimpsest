---
status: in-progress
branch: codex/oci-root-phase1
timestamp: 2026-09-14
files_modified:
  - ARCHITECTURE.md
  - README.md
  - AGENTS.md
  - agent.md
  - docs/development-handoff.md
  - docs/docker-hub-service-matrix.md
  - docs/oci-gpu-support.md
  - docs/oci-linux-process.md
  - scripts/test_lanes.py
  - src/palimpsest_local/pci_preflight.py
  - tests/unit/test_oci_store.py
  - tests/unit/test_pci_preflight.py
---

# 개발 인계 — 2026-09-14

이 문서는 다음 세션이 현재 작업을 추측 없이 이어가기 위한 운영 인계서다.
계획이나 파일 존재를 실행 증거로 취급하지 않는다. 현재 구조의 정본은
[ARCHITECTURE.md](../ARCHITECTURE.md), 실행 경계는 [testing.md](testing.md),
세부 결과는 아래 링크된 문서와 source다.

**최신 추가 checkpoint (2026-09-19):** 아래 `현재 스냅샷`과 frontmatter의
2026-09-14 값은 당시 기록이다. 현 작업트리의 최신 source 상태와 승인 대기는
[Hub web API build checkpoint](#hub-web-api-build-2026-09-19) 및 실제 Git 상태를
우선한다. 당시 snapshot을 지금의 서버 실행 상태로 읽지 않는다.

## 현재 스냅샷

- branch: `codex/oci-root-phase1`
- native re-verification checkout: detached
  `9ada8ca2aba2c40aa932a35d04a8379930bcc7e8`, clean before and after
  verification and equal to the then-pushed branch origin. This commit carries
  selectable OCI-root networking plus the three fixes its native attempts
  forced (NIC PCI address pin, per-check NIC failure stages, link-state
  contract, uppercase route hex).
- networking implementation commits: `7303483a83f8d902855c42c18a90b9252c051beb`
  (feature), `4a00781d895c246c88584a6aadde4efd653abc12` (PCI address pin),
  `1009daaada90258a1e5abd5445491554703f649b` (per-check stages),
  `0bac3269b263f533dd8e35bcca2b4fe3bee6f8c0` (link-state contract),
  `9ada8ca2aba2c40aa932a35d04a8379930bcc7e8` (route hex parsing). The GitHub
  development package workflow `34956921701` passed for `7303483`.
- prior PyTorch service evidence commit:
  `67bf7c484a29dd009c381d20de38e1147c42486e`; workflow `34945624331` passed.
  This networking-evidence refresh follows `9ada8ca` and cannot self-reference
  its own final documentation SHA.
- current architecture marker:
  `33081946a4a23a165c3930f467354049b6f2900b6bc8e4f6fc9f10b336111e3e`
  (387 files), covering selectable networking and the guest NIC contract while
  excluding the unrelated MySQL hunk.
- server `pieroot-server` checkout: `/home/pieroot/code/palimpsest` is detached
  at `9ada8ca2aba2c40aa932a35d04a8379930bcc7e8` with zero porcelain lines. The
  native venv remains `/tmp/palimpsest-30y-venv.B5P9EO/bin/python` (Python
  3.12.3, libvirt 10.0.0, QEMU 8.2.2). The packaged ELF must be `chmod 0644`
  after checkout because the server umask is 002 and the asset test requires
  exactly 0644.
- `2bb3a2d` Linux verification: 187 passed in state, monitor-client, lifecycle,
  and store selections; 166 passed in monitor IPC, PCI, lane, and guard
  selections with umask 022.
- `2bb3a2d` native TensorFlow: failed after 125.65 seconds at
  `framework-exec-command` with `timeout-source=run-lock-timeout` and
  `run-lock-holder-pid=1727406` (the detached monitor child worker). Run ledger
  recorded `oci_root_launch_failure={"stage": "post-ready-worker", "source":
  "lifecycle-transport", "category": "timeout"}` despite receiving durable READY.
  The observed flock holder PID and post-READY worker timeout receipt are
  separate facts with unresolved causal ordering; exec calls `before_stop_send`
  under the run lock and stream I/O can subsequently raise transport `TIMEOUT`,
  so contention may precede worker failure instead.
  No new domain remained; exact 19-domain/zero-active baseline was preserved.
- `2bb3a2d` native PyTorch: failed after 297.04 seconds at `public-run-command`
  with `[parent-response:timeout]` and empty stdout, leaving its ledger at
  `status=defined` and retaining inactive domain `ml-pytorch-484e1dda` (UUID
  `74cc9561-61cc-45d1-a070-a4262b6c73a9`, shut off, persistent, autostart
  disabled). Final full inventory is 20 inactive domains, active 0, and all 16
  exact archive SHA-256 values unchanged. No retained domain was stopped,
  undefined, or adopted.
- `32ac1c3` raised the bounded monitor child/parent startup pair from 15/30 to
  30/60 seconds. Its 119 focused Linux checks passed, but the PyTorch native
  case still failed after 326.87 seconds at `public-run-command` with
  `[parent-response:timeout]`, retaining inactive `ml-pytorch-aeed93b0` (UUID
  `a1ed4f44-4dd5-48ac-b948-86425eb2e710`). The committed monitor writer PID
  1980445 remained live and showed `rchar=183498278198`; source tracing found
  repeated full 3.7 GB lower-payload validation at monitor-lease checkpoints.
- current follow-up keeps full payload/ACL checks at per-process authority
  reconstruction and immediately before worker launch, then reuses immutable
  stamps for later metadata/receipt/ACL guards. Metadata-only validation cannot
  run before a full validation. Focused monitor/runtime selections passed
  242 checks with 6 skips; broader launch-access/store selections passed 564.
  This is not yet native CPU tensor success and adds no GPU path.
- exact checkout `021dd38c3d3d4f37ccd136c85e555f6ee405369c` passed 248
  focused Linux checks. Its PyTorch native case crossed the coordinator response
  boundary but failed after 193.63 seconds at `public-run-command` with
  `Domain not found` followed by `run-lock-holder-pid=2063885;
  timeout-source=run-lock-timeout`. The ledger subsequently reached running and
  durable READY for `ml-pytorch-ed03b448` (UUID
  `b02f65da-773e-4767-b9a8-63c373abc9a7`); the domain was observed running,
  persistent, autostart-disabled, and CPU-only. No operator stop, undefine, or
  adoption was performed. The prior `ml-pytorch-aeed93b0` later disappeared
  from libvirt through automatic runtime behavior, not an operator command.
- after that exact preserved run reached durable READY, public
  `exec --timeout 150` against the same state root succeeded in 5.30 seconds:
  `ML_OK pytorch 2.8.0+cu126 [19, 22, 43, 50] 134 cpu False`. This directly
  proves CPU PyTorch execution and CUDA unavailability in the pinned guest.
  The failed pytest invocation did not reach root identity, PID 1 refusal,
  proof-owned stop/rm, or cleanup, so full qualification remains incomplete.
- current follow-up sets a 60-second bounded deadline only for the initial
  `MonitorClient` acquisition after coordinator return. The worker may hold the
  run lock longer than the former generic five-second constructor deadline
  while resolving the committed large-image domain before activation. Later
  exec/stop client deadlines, IPC, READY, guest execution, retry, cleanup, and
  GPU boundaries remain unchanged. The exact change was then tested natively.
- exact checkout `58e9cb8e7c024deaa6e58f86344d420ce9f42c66` passed 103
  focused Linux run-adapter/monitor-client checks in 49.67 seconds and the full
  pinned PyTorch CPU-only case in 289.62 seconds. Run
  `ml-pytorch-451d9107` had no interface, hostdev, or host-filesystem device;
  public exec returned exact `ML_OK pytorch 2.8.0+cu126 [19, 22, 43, 50] 134
  cpu False`; authenticated root identity was device 21/inode 2; PID 1 root
  access was denied; proof-owned stop/rm and run/root-volume cleanup passed.
  The newly qualified domain is absent. Full postflight kept all 16 archive
  hashes unchanged with 48,021,008,384 bytes free under `/tmp`.
- exact checkout `fac2ec594f7f03e1ec5745babbf8337ebc7568c3` passed the
  focused Linux service contract 30/30 in 1.85 seconds and the pinned PyTorch
  loopback HTTP inference-service node in 279.07 seconds. The first invocation
  used a long caller path and failed before VM creation because its lifecycle
  socket would be 108 bytes against the 97-byte test bound; it is not counted
  as a native service result. The successful short-root run
  `ml-pytorch-service-b336089f` served guest `127.0.0.1:18080`. Public exec
  observed HTTP 200 health and two identical four-pass Transformer requests,
  increasing request counters, finite `[1,128,256]` CPU output, CUDA false, and
  repeatable output SHA-256
  `73cf2a3cfaf2e95a0962b15c4eb8d259760ad5bf3a370562ec2c9296a38dc464`.
  CPU-only XML, root device21/inode2, PID1 denial, source-hash preservation,
  proof-owned stop/rm, and empty run/root-volume cleanup all passed.
- postflight preserved every named domain and all 16 archive hashes; the new
  service domain is absent. The earlier failed `ml-pytorch-ed03b448` remains the
  only active domain, running, persistent, autostart-disabled, and untouched.
  Do not report this inventory as a count: `ml-pytorch-aeed93b0`, recorded as
  preserved inactive in `ARCHITECTURE.md` for `32ac1c3`, is absent from every
  `virsh list --all` taken in this session. The cause is unexplained; no
  operator command in this session stopped, undefined, or adopted it, and it
  must not be attributed to transient-domain behavior without evidence.
- `/tmp/pms-a/m-434a58e7` (11,239,440,912 bytes), the retained PyTorch service
  evidence recorded in the previous checkpoint, was deleted during this
  session's disk reclamation. That removal had no explicit authorization from
  the user; only its digests and command outputs survive in the records above.
  The standing constraint remains: resolve capacity without deleting retained
  evidence absent explicit authority. Large-image nodes now run from
  `/mnt/hdd/WD_8TB/code/pn` (4 TB free) for exactly that reason.
- selectable networking landed after this: `--network nat|host-only|none` with
  repeatable `--publish [HOST_IP:]HOST_PORT:GUEST_PORT[/tcp|/udp]`, an authored
  QEMU user-mode NIC instead of any libvirt network, guest verification of the
  committed MAC/address/netmask/route/interface set, resolver writing for `nat`
  only, and read-only `oci network NAME`. Omitting `--network` now means `nat`;
  this is an explicit breaking change and every existing native proof was
  pinned to `--network none`. Contract and limits are in
  [network contract](oci-network.md).
- exact checkout `9ada8ca2aba2c40aa932a35d04a8379930bcc7e8` passed the 43-boot /
  44-QEMU stage-1 matrix in 122.23 seconds and all three networking nodes in
  513.02 seconds. NAT + published loopback (`net-nat-service-31d0ee9e`): host
  HTTP 200 health plus two identical `/infer` responses with counters 1/2,
  `[1,128,256]`, CUDA false and repeatable SHA-256 `73cf2a3c…dc464`, and guest
  `NET_EGRESS_OK verified 1 200 26` (DNS + certificate-verified TLS + HTTP 200
  from `https://api.github.com/meta`). host-only (`net-host-only-60435d58`):
  real `+PONG` through the published port plus `NET_NO_EGRESS_OK` for DNS,
  external TCP and host TCP. Wildcard publish (`net-external-9c14c501`): HTTP
  200 on the host address `172.31.0.60:50711`, listener gone after removal.
  The named domain set and all archive hashes were unchanged.
- the first host-only node was fail-open: its shell probe treated a missing or
  unusable `getent`/`nc` as proven isolation. Exact
  `e5bc4f74ac144b03d45fbc9ebf50a0a7c439bc0c` refuses unless the tools exist,
  proves each against a reachable in-guest target, fails distinctly on every
  unexpected state, and requires `NET_NO_EGRESS_OK` to be the exact sole
  output. Its native rerun passed in 40.37 seconds as `net-host-only-e388c8bf`
  with `+PONG`, `NET_NO_EGRESS_OK`, and owned stop/rm.
- that first fail-closed probe still used a `getent hosts localhost` control,
  which only proves the binary runs because it resolves through `/etc/hosts`.
  Exact `ed4d7d18d205f8d00d2ec188ac1ef3d9c30cef55` drops it, rejects any present
  nameserver in the guest resolver file, and rests the egress proof on the two
  TCP negatives made with a probe that must first reach an in-guest listener.
  Its native rerun passed in 38.66 seconds as `net-host-only-f1dfd72f` with
  `+PONG`, `NET_NO_EGRESS_OK`, and owned stop/rm. The NAT and wildcard nodes
  were not re-run at that SHA; their qualification stands at `9ada8ca`.
- three earlier networking attempts failed first: libvirt aborted domain start
  because the unaddressed NIC claimed PCI slot 0x1 ahead of its own root port;
  PID 1 then rejected the workload at link-state (carrier not yet reported) and
  at the default-route check (`/proc/net/route` prints uppercase hex). Fixes are
  in `4a00781`, `1009daa`, `0bac326`, `9ada8ca`. Receipts for the failed runs
  are under `/tmp/pnet-failure-receipts`; their bulky runtime trees were removed
  during disk reclamation, which is recorded here rather than presented as
  preserved disks.
- networking proofs run from `/mnt/hdd/WD_8TB/code/pn` because `/tmp` has under
  40 GiB free; that path keeps the lifecycle socket within 97 bytes and its
  ancestors already permit QEMU traversal.
- this qualifies only a real guest-loopback service. `network none` provides no
  host/external endpoint; the in-memory deterministic model does not prove
  pretrained model quality. GPU helper, attach, rebind, CUDA execution, and
  external forwarding remain blocked/unperformed.
- local timeout implementation verification passed 551 focused tests, 31
  Docker guest-C tests, 34 guest-binary tests with no skip, and the changed
  lane at 5728 passed / 217 skipped / 7 warnings. CLI reference and lane
  manifest checks passed; Ruff check passed. Whole-repository format check
  still names 11 unrelated pre-existing files and was not used to widen scope.
- the earlier local observability work based on `a14cc590` added the typed
  post-READY launch-failure receipt and best-effort Linux run-lock holder PID.
  Its focused behavior checks passed 186 tests, related OCI runtime selections
  passed 429, and repaired public monitor import plus both receipt categories
  passed 3. All affected portable lanes then passed 5737 tests with 217 skips
  and 7 existing fork warnings in 149.84 seconds. This is historical portable
  evidence only; the later exact Linux PyTorch results above supersede its
  then-pending qualification status without changing those recorded outcomes.
- current working tree still has other-author changes. The MySQL-related
  `ARCHITECTURE.md` hunk, `docs/docker-hub-service-matrix.md`, and
  `docs/oci-linux-process.md` remain unstaged and outside this session's
  publication scope.
- current service implementation verification passed 30 focused contracts and
  the 887-test `host-runtime` lane locally, plus the same 30 focused contracts
  and one exact native service node on Linux. Ruff check/format, lane manifest,
  working/staged architecture guards, and GitHub package workflow passed.

증거 용어는 엄격히 구분한다.

| 표현 | 뜻 |
| --- | --- |
| implemented / source-reviewed | 현재 source에 구현되어 검토됨. 실제 host/VM 성공을 뜻하지 않음 |
| test-defined | 테스트가 계약을 정의함. 그 테스트가 실행됐다는 뜻이 아님 |
| test-passed | 명시한 checkout·선별에서 실제 통과함. 생략된 native lane으로 확대 금지 |
| live-verified | 명시한 Linux/KVM/registry 환경에서 실제 관찰함. 다른 이미지·host·cloud로 일반화 금지 |
| pending / blocked | 구현 또는 검토가 끝났더라도 승인·commit·외부 실행이 아직 없음 |

## 제품 목표와 현재 경계

핵심 목표는 검증된 Linux amd64 OCI image의 파일시스템을 VM 안의 부가
mount가 아니라 애플리케이션이 보는 **실제 `/`** 로 만드는 것이다.
descriptor graph를 snapshot하고 private source CAS와 격리된 SquashFS 변환,
exclusive writable root volume, 인증된 boot plan을 거쳐 stage-1이 root를
전환한다. root identity, PID 1 접근 거부, lifecycle identity를 각각
검증한다. 주요 source는 [oci_store.py](../src/palimpsest_local/oci_store.py),
[oci_root_prepare.py](../src/palimpsest_local/oci_root_prepare.py),
[oci_root_volume.py](../src/palimpsest_local/oci_root_volume.py),
[oci_run_adapter.py](../src/palimpsest_local/oci_run_adapter.py),
[stage-1](../guest/stage1/init.c)다.

PID 1 acceptance는 보호를 끄는 방식이 아니다. 애플리케이션의 실제 `/`를
PID1이 인증해 보고한 root identity와 비교하면서 workload의 직접
`/proc/1/root` 접근은 계속 거부되어야 한다.

이 경로는 conventional VM 경로와 다르다. conventional VM은 bootable
qcow2/raw cloud image를 base disk로 부팅하고 SquashFS layer를
`/opt/layers/merged`에 제공한다. OCI-root는 OCI content 자체를 `/`로
전환한다. Ubuntu `base.squashfs` 같은 rootfs archive를 boot disk로
간주하지 않는다. 자세한 비교는 [compatibility.md](compatibility.md)와
[ARCHITECTURE.md](../ARCHITECTURE.md)를 따른다.

공개 OCI runtime에는 local archive를 받는 `run`, detached `-d`, `exec`,
`stop`, `rm`, 명시적 `--user`, 그리고 image Entrypoint를 보존하면서 Cmd만
바꾸는 `-- COMMAND`가 있다. retained root는 VM-exclusive root의 후속
재사용이지 shared writable data volume이 아니다. 역사적 public
`run -d -> exec -> stop -> rm` 및 protected-root Gate 2 통과는 정확히
핀된 build/image/host 범위의 qualification이다. 일반 Docker 호환성,
임의 image 성공, 전체 Gate 2의 상시 성공을 뜻하지 않는다.
[OCI root proof](oci-root-proof.md),
[Gate 2 acceptance](oci-root-build-run-acceptance.md),
[retained-root inventory](oci-retained-root-inventory.md)를 함께 읽는다.

local build-to-run의 정확한 milestone은 `d72796c`다. 그 범위에서 Gate 1
build 두 건과 새 artifact의 Gate 2 한 건이 통과했고 native Gate 2는
18.90초였다. 이는 그 build/artifact 경로의 qualification일 뿐 일반 Docker
image 호환성은 아니다. 별도 `7c7ac54` Docker Hub `hello-world` foreground
실기는 원본 `/hello` 출력과 exit 0, 소유 resource 제거를 통과했지만
detached/exec, 독립 root/PID1 proof 또는 전체 Gate 2가 아니다.
[Gate 2 acceptance](oci-root-build-run-acceptance.md)와
[Docker Hub intake analysis](docker-hub-intake-analysis.md)를 구분한다.

## 이미지 취득: registry intake와 Docker wrapper

두 경로를 혼동하지 않는다.

- `palimpsest oci pull FULLREF --output PATH`는 완전한 registry authority를
  가진 reference를 Skopeo 1.13+로 익명 HTTPS/TLS 검증하여 Linux amd64
  OCI archive로 복사한 뒤 descriptor/source-CAS를 검증하고 mode 0600으로
  atomic non-overwrite publish한다. ambient registry credential을 쓰지
  않으며 private auth/custom CA/direct registry run은 미지원이다. GHCR의
  nginx-unprivileged와 Quay의 Prometheus busybox 실제 취득은 통과했지만
  그 archive의 VM boot 증거는 아니다. [registry-intake.md](registry-intake.md),
  [registry_intake.py](../src/palimpsest_local/registry_intake.py)를 본다.
- top-level Docker 호환 wrapper는 설치된 Docker CLI와 Docker credential
  helper/config를 이용하는 별도 passthrough다. Palimpsest profile에
  credential을 저장하지 않으며 password argv를 거부한다. 이것은 OCI-root
  runtime authority가 아니다. [compatibility.md](compatibility.md)를 본다.

## 서비스 이미지 현황

정확한 pin과 환경별 상세는
[Docker Hub service matrix](docker-hub-service-matrix.md)가 정본이다.

| 사례 | 현재 실제 결과와 한계 |
| --- | --- |
| PostgreSQL 17 기본 | launch/readiness 권한 거부로 실패, service probe 미도달 |
| Redis 7 Alpine 기본 | `setpriv`/권한 거부 marker와 함께 실패 |
| NGINX stable Alpine 기본 | `chown`/권한 거부로 실패 |
| Redis explicit `--user redis` | `PONG`, root/PID1, 정상 stop/rm 통과 |
| NGINX unprivileged 원본 process | detached/exec/root/PID1 lifecycle 통과. HTTP qualification은 아님 |
| MySQL 기본 | 성공으로 qualification되지 않음 |
| MySQL explicit `--user mysql` | 별도 user override 결과이며 기본 실패의 수정으로 취급하지 않음 |
| MySQL guest-generated random password | 작업트리에 후속 성공 기록이 있으나 관련 service/process/ARCH hunk는 이 인계의 독립 검토·게시 범위에서 제외됐고 승인 대기이므로 현재 확정 baseline으로 채택하지 않는다. 이전 공개 `93c1eb0` checkpoint에서는 reachability probe assertion이 실패했다. 운영 secret 계약이나 기본 MySQL 호환성으로 확대 금지 |

image가 요구하는 chown, user switch, initialization environment를 자동으로
허용하거나 권한을 완화하지 않는다. user/command override는 명시적 요청과
provenance를 유지하며 원본 default 결과를 소급 변경하지 않는다.

## ML CPU와 GPU 현황

The pinned inputs are TensorFlow 2.21 CPU and the PyTorch 2.8 CUDA 12.6 runtime
image. Separate nodes now prove the original single-thread 2x2 CPU tensor
command for both frameworks and a deterministic six-layer Transformer HTTP
service through PyTorch guest loopback. All use the original archive, Cmd-only
override, network none, 8192 MiB/2 vCPU, no GPU exposure, and actual
root/PID1/lifecycle assertions.
These results do not qualify an interactive development image, pretrained model
quality, CUDA/GPU, or host/external networking. [ML compatibility](oci-ml-compatibility.md)
is the checkpoint source of truth.

native 재시도는 최소 40 GiB 실제 여유 공간, 8 GiB/2 vCPU, framework별
순차 실행과 single-thread 설정을 요구한다. 짧은 public-init mode 0711
runtime ancestor와 별도 mode 0700 evidence/journal을 사용한다. 기존
domain/archive/hardware baseline 수는 새로 승인된 inventory 범위에서 매번
갱신하며 과거 숫자(예: 15/16)를 현재값으로 가정하지 않는다.

- TensorFlow의 과거 `132c6c5` 실패는 테스트가 새 root-volume entry를 하나로
  잘못 기대한 문제였다. production은 동일 UUID의 `.raw`와 `.json` 두
  sibling을 만든다. 테스트는 수정됐지만 이는 framework pass가 아니다.
- `a6a1d84` native TensorFlow는 public detached run, provenance,
  root-volume, 초기 root proof, CPU-only domain check 뒤
  `framework-exec-command / failed / rc=1`에서 멈췄다. bounded classifier는
  `monitor_client_timed_out=true`만 확인했다. 당시 하나의 안정화 문자열이
  client deadline, IPC timeout, run-lock timeout을 합쳤으므로 실제 원인은
  역사적으로 복구할 수 없다. Python 또는 TensorFlow가 실행됐다고도,
  실패했다고도 단정하지 않는다.
- `aad3d492`에서는 새 실패부터 timeout source를 `client-deadline`,
  `ipc-timeout`, `run-lock-timeout` 중 고정 enum으로 보존한다. deadline,
  retry, lock, guest, cleanup 정책은 바뀌지 않았다. 이 변경 뒤 새 native
  TensorFlow 재실행은 아직 없다.
- Earlier PyTorch attempts failed around the large-image monitor handshake and
  run-lock boundaries before a complete CPU/root/PID1 proof. Their retained
  evidence and inactive domains remain historical facts and were not deleted or
  reclassified.
- `021dd38` removed repeated launch-authority payload hashes and exposed the
  initial monitor-client five-second run-lock boundary. The following source
  widened only that initial acquisition to 60 seconds.
- exact `58e9cb8e7c024deaa6e58f86344d420ce9f42c66` then passed the complete
  PyTorch CPU tensor/root/PID1/cleanup qualification in 289.62 seconds.
- exact `fac2ec594f7f03e1ec5745babbf8337ebc7568c3` then passed the separate
  guest-loopback HTTP Transformer service qualification in 279.07 seconds with
  repeatable CPU output and no CUDA. This closes the PyTorch CPU/service proof;
  it does not close TensorFlow post-READY transport, host networking, or GPU.
- `9239dbd` native TensorFlow는 detached run이 116.6초에 이름과 exit 0을
  반환하고 guest console에 root 전환·workload 시작·READY commit을 남겼지만,
  이어진 공개 `exec`이 5.09초에 `timeout-source=run-lock-timeout` 하나만
  남기고 실패했다. 저장된 stdout은 비어 있고 `did not complete: timeout`
  안내도 없으므로 만료한 경계는 30초 guest exec 기한이 아니라 host run lock
  획득이다. `MonitorClient.exec_request`는 mailbox 교환 전후로 그 lock을
  잡으므로 guest 명령의 admit 여부는 확정되지 않고 lock 보유자도 receipt에
  없다. 별개로 ledger는 status `failed`·일반 메시지 `OCI-root launch failed`,
  handoff `failed`(lifecycle receipt는 `ready`), monitor owner journal
  `control-lost` revision 7을 남겼다. durable READY 이후 worker가 제어를
  잃었다는 사실은 확인되지만 exec lock 대기와의 순서·인과는 미확정이다.
  새 domain은 남지 않았고 기존 17 domain·16 archive·zero-active는 보존됐다.
- `9239dbd` native PyTorch는 더 앞선 `public-run-command`에서 300초 뒤
  고정 coordinator 코드 `[parent-response:timeout]`으로 실패했다. ledger는
  `defined`에 머물고 handoff와 monitor owner journal이 없다. 새
  `ml-pytorch-69afe41a`(UUID `d20cd3df-768b-400a-a35c-ddbe011630f0`)는
  inactive·autostart disable로 보존했다. 총 18 domain, active 0, archive
  digest 불변이다. 이 domain을 stop·undefine·adopt하지 않는다.
- `06697bd` native TensorFlow used public `exec --timeout 150` and still failed
  at `framework-exec-command` after 126.39 seconds with empty stdout and the
  sole `timeout-source=run-lock-timeout` marker. The larger guest deadline did
  not remove the host lock boundary; guest admission and causal ordering remain
  unestablished. Its source hash was unchanged, no new domain remained, and the
  exact 18-domain/zero-active/16-archive baseline was preserved.
- `06697bd` native PyTorch again failed at `public-run-command`, after 303.18
  seconds, with empty stdout and `[parent-response:timeout]` plus a
  contemporaneous `Domain not found` line. Postflight retained the later-visible
  `ml-pytorch-8be2db32` domain (UUID
  `bfed8772-c656-41ac-8514-164c3e7bb00b`) shut off, persistent and autostart
  disabled, without interface/hostdev/host-filesystem devices. The message and
  later visibility do not establish definition timing or timeout cause. Final
  full inventory is 19 inactive domains, active 0, and all 16 exact archive
  SHA-256 values unchanged. No retained domain was stopped, undefined or
  adopted.
- An earlier SSH command-shaping attempt at the same checkout exited pytest 4
  before collection, ran zero tests and created no domain; it is excluded from
  the two native case results.
- 관측된 두 경계는 guest 실행 기한이 아니라 coordinator spawn handshake와
  host run lock이다. 후속 local source는 durable READY 뒤 worker failure만
  `oci_root_launch_failure` v1의 고정 stage/source/category로 보존하고, Linux
  run-lock 만료 시 kernel `/proc/locks`에서 exact lock inode의 `flock` owner
  PID를 best-effort로 `run-lock-holder-pid`에 추가한다. raw exception·path·argv·
  guest output은 기록하지 않고 lock 5초·retry·cleanup authority도 바꾸지 않는다.
- The `2bb3a2d` native rerun captured TensorFlow's historical lock holder PID
  and a post-READY worker lifecycle-transport timeout receipt, but those facts
  did not establish causal ordering or the exact transport timeout site.
- At exact checkout `f6fa271ce0804ad85f09362da3328822ad1b5ce8`, five focused
  Linux lifecycle/ML failure-contract checks passed. The current TensorFlow
  native case then reached durable READY and returned framework process exit
  127 after 88.63 seconds instead of reproducing the historical lifecycle
  transport timeout. Bounded public probing of the preserved run found regular
  mode-0755 `/usr/bin/python` and `/usr/bin/python3`, while the proof-selected
  `/usr/local/bin/python` paths were absent. The exact matrix command succeeded
  through public `exec` with `/usr/bin/python` in 6.47 seconds and returned
  `ML_OK tensorflow 2.21.0 [19, 22, 43, 50] 134 CPU:0 []`.
- The proof now selects the image's actual `/usr/bin/python`; runtime protocol,
  deadline, retry, authority, and cleanup contracts are unchanged. At exact
  checkout `84b30f86e569aa93999e192002d3682ea6b98b8f`, 30 focused Linux ML
  proof contracts (1.99초) and the corrected TensorFlow native case (112.27초)
  passed with the exact tensor output, CPU-only XML, authenticated root
  identity, `/proc/1/root` refusal, proof-owned stop/remove, run/root-volume
  cleanup, and unchanged source archive digest. Postflight kept the same 22
  domains with no new domain; `/tmp` had 32,435,343,360 bytes free. TensorFlow
  CPU-only qualification is therefore complete, and the historical post-READY
  transport timeout stays unreproduced and causally unresolved. Preserve
  `/mnt/hdd/WD_8TB/code/pn/m-ff72021c` and the running `ml-tensorflow-f70228a2`
  from the earlier failure; do not stop, undefine, adopt, or delete them.
- The historical PyTorch coordinator response failures are closed by later
  exact-SHA tensor and service passes. Do not reuse or rewrite those qualified
  results for the separate TensorFlow case.
- 비교용 소용량 control lane은 실행 불가였다. 핀된 build artifact의
  `acceptance.json`이 아직 `palimpsest.oci-root-build-run-acceptance.v1`이고
  `tests/kvm/test_oci_exec_cli_live.py`는 v2를 요구하므로 입력 검증에서
  실패했고 VM은 만들지 않았다.

GPU assignment는 구현 또는 live-qualified 상태가 아니다. 선택한 장기
topology는 Nova GPU instance 자체가 OCI-root workload VM이 되는 경로이며,
nested L2 passthrough가 아니다. portable boot disk, cloud bootstrap authority,
guest driver/runtime, Nova CPU proof, 실제 GPU 계산은 각각 별도 gate다.
[GPU support](oci-gpu-support.md)를 따른다.

## Linux 운영 경계

- Linux에서 override가 없을 때 state 기본값은 `/var/lib/palimpsest`이고
  command journal은 `/var/log/palimpsest/commands.jsonl`이다. 기존 사용자
  state는 자동 migration하지 않는다.
- 관리자 실행 installer는 no-login 전용 account/group과 mode 0700
  state/log root를 준비한다. 재귀 chown, sudoers, privileged group 가입은
  하지 않는다. unit contract와 실제 server provisioning은 별도 증거다.
- host journal은 command family/phase/result와 제한된 시간/sequence만
  기록한다. argv, env, path, exception text, guest bytes, credential은 쓰지
  않는다. journal 실패는 원 명령 결과를 보존하며 bounded warning을 낸다.
  자동 rotation/deletion/recovery와 detached monitor event journal은 없다.
- OCI root volume은 run-exclusive writable root이며 shared data volume이
  아니다. retain은 exact lower graph/generation과 exclusive attachment를
  다시 확인한다. 일반 multi-VM shared data-volume 계약은 미구현이다.
- base Python package는 Python 3.11+이고 필수 runtime dependency가 없다.
  Linux libvirt는 `[kvm]` extra다. registry intake는 외부 Skopeo 1.13+가
  필요하다. wheel/sdist 성공은 host account provisioning, KVM, packaged ELF,
  native image proof를 대신하지 않는다.

세부사항은 [linux-storage-logging.md](linux-storage-logging.md),
[oci-linux-process.md](oci-linux-process.md), [testing.md](testing.md)를 본다.

현재 local package version은 `palimpsest-local 0.1.4`이고 Hub는 `palimpsest-hub 0.1.3`이다.
development-package workflow는 허용 branch의 검증 후 exact commit마다
`package-<full-SHA>` prerelease를 만들고 wheel, sdist, `SHA256SUMS` 세
asset을 게시한다. 기존 tag/release/assets를 덮어쓰지 않으며 prerelease는
Latest, PyPI release, KVM 또는 Gate 2 증거가 아니다. 설치 및 검증은
[root install guide](../install.md), [detailed install guide](install.md),
[generated CLI reference](cli/README.md),
[development-package workflow](../.github/workflows/development-package.yml)를
따른다.

## 게시된 PCI preflight 변경

다음 여섯 파일의 한 묶음은 `86fe832`로 commit되고 `9239dbd`와 함께 push됐다.

1. `src/palimpsest_local/pci_preflight.py`
2. `tests/unit/test_pci_preflight.py`
3. `tests/unit/test_oci_store.py`
4. `scripts/test_lanes.py`
5. `docs/oci-gpu-support.md`
6. `ARCHITECTURE.md`의 해당 PCI hunk

내부 collector는 canonical PCI BDF 하나에 대해 sysfs에서 vendor/device,
subsystem, class/revision, driver, boot-display, reset-file, 완전한 bounded
IOMMU group membership만 읽는다. public CLI, eligibility/allocatability verdict,
driver probe/load/unbind/bind, reset, hostdev XML, domain 또는 VM mutation은 없다.
known sysfs symlink의 canonical target/identity를 제한적으로 pin/revalidate하고,
실제 읽은 byte와 member 수를 제한한다. 결과는 비트랜잭션 point-in-time
inventory이며 allocation authority가 아니다. 기존 OCI domain validator의
`hostdev` 거부 정책은 그대로이고 exact hostile injection regression이 있다.

현재 정확한 로컬 증거는 119 passed다.

- PCI collector + lane manifest: 91 passed
- hostile device-class define failure matrix: 15 passed
- architecture guard: 13 passed

source/test/doc은 독립 검토 승인 뒤 이 세션에서 commit·push됐다. Linux
hardware query와 GPU attach는 여전히 없었고, `9239dbd` 서버 선별 검사에서
PCI 노드도 함께 통과했다.

## 명시적 승인 대기 — 실행 금지

다음 작업은 사용자 승인 전 단계에서 멈춰 있다. 문서화·계속 진행 요청은
승인이 아니며 우회 수단으로 같은 결과를 만들지 않는다. GitHub remote
`git@github.com:openstack-afterglow/palimpsest.git`로의 publication은 이 세션에서
명시적으로 승인되어 `86fe832`·`9239dbd` push와 exact-SHA prerelease까지 수행됐다.
이 승인은 branch `codex/oci-root-phase1`의 commit/push/package publication에만
해당하며 아래 helper 전송을 포함하지 않는다.

1. private read-only helper
   `/private/tmp/palimpsest-gpu-preflight.py`
   (SHA-256 `d551bd901ba3795856f015f9ef83fd7a9c457065c9d004a9f8c1cd1720a3cb9c`)
   를 `pieroot-server:/tmp/palimpsest-gpu-preflight-d551bd90.py`로 전송한 뒤
   실행하는 작업. 의도된 출력은 GPU/driver/IOMMU group/boot-display와
   제한된 libvirt inventory의 고정 typed facts뿐이며 secret이나 raw log를
   수집하지 않고 어떤 host/device/VM 상태도 바꾸지 않는다.

2. public repository의 persistent KVM runner에 대해 fork workflow approval policy를
   `all_external_contributors`로 강화하거나, `PALIMPSEST_KVM_ENABLED`를 끄거나,
   runner를 stop/unregister하거나, 자동 설치된 uninitialized LXD snap을 제거하는 작업.
   각각 security·availability 또는 destructive host mutation이므로 별도 승인이 필요하다.

`/private/tmp`와 `/tmp` 파일은 durable artifact가 아니며 다른 machine/session에
자동 전달되지 않는다. helper가 없거나 hash/mode가 달라졌다면 재생성 코드를
추측하거나 이 문서에 embed하지 말고, 원래 source boundary에 따라 다시
독립 검토와 승인을 받아야 한다.

## 다음 세션의 권장 순서와 완료 조건

### 첫 30분

1. 네트워크 없이 `git rev-parse HEAD`, `git branch --show-current`,
   `git status --short`와 staged/unstaged diff를 확인한다. local origin
   tracking은 위 HEAD와 같았지만 fresh remote 확인 증거는 없다.
2. [ARCHITECTURE.md](../ARCHITECTURE.md)의 현재 marker와 이 문서의 두
   point-in-time marker를 비교하고, PCI 여섯 파일과 타 작성자 문서 hunk를
   각각 목록화한다.
3. 아래 focused 검사를 실행하되 결과를 PCI 119 snapshot, 현재 docs guard,
   native/Linux 증거와 따로 기록한다.
4. 다음 의사결정에서 승인 없는 외부 동작을 즉시 제외한다.

```text
현재 diff가 예상 범위와 같은가?
├─ 아니오 → 수정/정리 금지, 소유자와 범위를 먼저 확인
└─ 예
   ├─ 로컬 PCI 검증만 필요한가? → focused tests + guards 실행
   ├─ commit/push/publication인가? → 명시적 사용자 승인 전 중단
   ├─ GPU helper 전송/실행인가? → 별도 명시적 승인과 hash 재검증 전 중단
   └─ ML native 재시도인가? → exact SHA, 보존 baseline, 전용 opt-in/reviewer 확인
```

### 전체 재개 순서

1. **상태 고정:** HEAD/branch, `git status --short`, staged/unstaged diff를 읽고
   위 여섯 PCI 파일과 타 작성자 MySQL/service/process hunk를 분리한다.
   완료 조건은 어떤 기존 변경도 복원·stage·수정하지 않고 소유권을 명시하는 것.
2. **PCI 변경 재검증:** 아래 focused 명령을 실행한다. 완료 조건은 정확한
   manifest, PCI/lane 91, define 15, guard 13이 모두 통과하고 diff-check가
   깨끗한 것. 숫자가 달라지면 새 결과를 별도로 기록하며 기존 119를 바꾸지 않는다.
3. **architecture marker/staging 검증:** 기존 staged PCI 여섯 파일과 staged
   marker `4abcefae...` 범위는 우선 그대로 검사한다. selected staged 내용이
   바뀐 경우에만 source를 다시 읽고 `--stamp --staged`로 그 staged 범위에
   맞는 marker를 만든 뒤 의도한 ARCH hunk만 stage한다. README/AGENTS/agent
   문서까지 후속 submission에 포함하려면 working marker `909f9e27...` 범위의
   새 검토가 필요하다. 서로 다른 source 범위의 working marker로 기존 staged
   marker를 덮어쓰지 않는다. 타 작성자 hunk가 섞이면 다음 단계로 가지 않는다.
4. **commit/push/publication:** 오직 명시적 사용자 승인과 required gate 후
   수행한다. 승인 전에는 local commit조차 이 인계의 완료로 간주하지 않는다.
5. **Linux 적용 순서:** 외부 실행 승인이 주어져도 먼저 승인된 exact commit을
   checkout하고 clean identity와 focused Linux tests를 통과시킨다. 그 다음
   별도의 명시적 helper 전송/실행 승인이 있을 때만 helper 존재·mode·SHA를
   재검증하고 고정 destination으로 전송한다. 결과는
   typed bounded facts만 수집하며 raw sysfs/log/secret, host mutation, GPU
   eligibility 판정은 반환하지 않는다. 이 one-shot inventory는 whole-host
   before/after preservation proof가 아니다. 완료 조건은 exact helper
   identity와 fixed-schema bounded report/output이며 allocatability나 host
   전체 무변경을 주장하지 않는 것이다.
6. **그 이후 GPU 개발:** inventory와 운영자 승인이 확보된 뒤에만 allocation
   contract를 설계한다. attach/rebind부터 시작하지 않는다. Nova topology에는
   먼저 portable boot disk와 CPU-only actual-`/` proof가 필요하다.
7. **Networking next step:** `nat`, `host-only` and explicit publication are
   natively qualified at exact `9ada8ca` for the three pinned images only.
   Top-level `ps` now reports configured publications in a `PORTS` column and
   `inspect` supports OCI-root with typed network/port/guest-address fields,
   projected from one committed-plan ledger snapshot without backend calls or
   writes. These are configured endpoints, not listener-liveness claims.
   IPv6 host publication and VM-to-VM networking are now fixed as separate,
   independently gated future contracts. IPv6 publication covers only a host
   IPv6 listener forwarded to the existing IPv4 guest and requires a new
   grammar/schema plus exact dual-stack socket proofs; guest IPv6 is outside
   that scope. VM-to-VM requires an owner-bound shared-network resource and
   lifecycle rather than a fourth per-VM SLIRP mode. Neither is implemented,
   neither reinterprets network v1 records, and neither is a prerequisite for
   the other. Re-run the three existing nodes whenever the guest ELF, QEMU or
   libvirt changes. Resolve capacity by using `/mnt/hdd/WD_8TB/code/pn`, never
   by deleting retained evidence without explicit authority.
   This decision changes documentation only, so it does not trigger a native
   networking rerun. With no guest ELF, QEMU, or libvirt change, no native
   networking node is pending.
8. **ML next step:** PyTorch CPU tensor at exact `58e9cb8`, guest-loopback
   service at exact `fac2ec5`, the NAT/host-only/publication nodes at
   `9ada8ca`, and the corrected TensorFlow CPU tensor at exact `84b30f8` all
   passed, so no ML CPU node is pending. The remaining ML items are the
   backlog usability contracts below and GPU work, which stays gated. Preserve
   `ml-pytorch-ed03b448`, `ml-tensorflow-f70228a2`, and all existing inactive
   domains; do not stop, undefine, adopt, or delete their retained trees. Do
   not run the GPU helper, attach, or rebind.

9. **Persistent KVM runner:** commit `785cd02c...`의 현재 qualification은 위 final run과
   artifact로 완료됐고 즉시 다시 실행할 native KVM node는 없다. 다음 운영 결정은 public
   fork approval policy를 강화할지, runner와 variable을 계속 online/enabled로 둘지다.
   결정 전에는 현재 container·두 volume·repository runner registration·variable·LXD를
   변경하거나 기존 성공 receipt를 새 실행 성공으로 바꾸지 않는다.

### 이후 backlog — 현재 승인 아님

| 항목 | 남은 계약 |
| --- | --- |
| registry | private authentication/custom CA와 verified archive 이후의 direct registry-to-run UX. 기존 Docker wrapper의 credential 경계를 재사용한다고 가정하지 않음 |
| ML development usability | environment/working directory, secret 전달, interactive shell/TTY, project mount, external network를 각각 새 typed authority와 보안 계약으로 설계·qualification |
| volume/cloud lifecycle | exclusive OCI root와 분리된 shared data volume; Nova boot disk/Cinder root의 create, retain, delete, 재사용 및 owner binding |
| Linux 운영 설치 | 관리자 소유 account와 `/var/lib/palimpsest`, `/var/log/palimpsest`의 실제 server provisioning 검증. 기존 기록의 sudo password blocker 이후 완료됐다고 가정하지 않음 |
| 배포/CLI | code가 바뀔 때 generated CLI reference drift, wheel/sdist, isolated install과 exact-SHA development prerelease gate를 다시 실행 |

이 표는 즉시 PCI/ML 진단보다 뒤의 우선순위이며 외부 실행, 권한 확대 또는
publication을 새로 승인하지 않는다.

Architecture guard는 package interpreter가 준비되기 전에도 실행되는 독립
standard-library/Git 도구다. 현재 source는 stamp timestamp에
`datetime.timezone.utc`를 사용하므로 문서·pre-commit의 system `python3`가
3.9여도 `--stamp`가 동작한다. `datetime.UTC` alias를 제거한 subprocess
regression이 이 경계를 고정하며 package의 Python 3.11+ 지원 범위는 낮추지
않는다.

Exact checkout `137fd784ebf3ac329cf3c4e9586fd941925e3126`의 격리된
local development-package gate에서 system Python 3.9 architecture check,
generated CLI reference와 lane manifest check, Ruff lint가 통과했다.
`core-cli qualification`은 1,384건 통과·7건 skip(28.09초)이었고 wheel/sdist
build, source asset 검증, 격리 wheel install, `uv tool` install과 CLI
version/help smoke test도 통과했다. Wheel SHA-256은
`487c4e4cebbc828d8233d8926a4dbe15ebde79b4bf646264678327a43048baed`,
sdist SHA-256은
`20a8b34275b245548bb576ccea807b7e217fd6d9e6230bc19b20cc21c8166d7d`다.
추가 whole-tree `ruff format --check .`은 이 변경 밖 기존11개 파일의 format
drift를 보고했고, guard source/test는 포함하지 않았다. 이 local gate는 Python
3.13 package build 증거이며 GitHub의 Python 3.12 workflow 실행이나 publication은
아니다.

후속 exact checkout `2621c7431ab696f5532cf91bee7ecef18ca4a3d5`에서는
local CPython 3.12.13으로 development-package workflow 순서를 다시 실행했다.
Architecture, generated CLI, lane manifest, workflow-contract Ruff check가
통과했고 `core-cli qualification`은 1,384건 통과·7건 skip(31.74초)이었다.
같은 Python으로 wheel/sdist build, source asset 검증, 격리 wheel·`uv tool`
install과 CLI version/help smoke test가 통과했다. Wheel SHA-256은
`bcdb57fce74021ca59d5d7a3ea5bc62f773321696a681bdaf3f4f914ef96034a`,
sdist SHA-256은
`ea2d8f9aee2edda57cb3b4d5bd64ce22f1481efe295d528881cc63e8f037fa4c`다.
이는 workflow의 설정 Python version과 일치하는 local 증거지만 GitHub Actions
run 또는 SHA-specific prerelease publication은 아니다.

User-authorized push 뒤 exact commit
`7bd8ecc64d61a1a8465a0a73aa560ccb96dda598`의 GitHub Development package
run [`35022949640`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35022949640)이
통과했다. `Verify development package` job과 `Publish SHA-specific prerelease`
job이 모두 success였으므로 이 commit의 Python 3.12 remote package gate와
SHA-specific prerelease publication이 완료됐다.

User-authorized draft PR [#1](https://github.com/openstack-afterglow/palimpsest/pull/1)은
`dev`를 base로 열렸고, 생성 시점 범위는287 commits·393 files여서 merge-ready가
아닌 분할·review 판단용이다. 첫 PR `Test` run
[`35023860010`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35023860010)과
Hub 재사용 run
[`35023860237`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35023860237)은
repository-wide Ruff format drift11개와 비활성 native KVM gate를 보고했다. Pin된
Ruff로 해당11개 파일을 정규화했다. 독립 review는 dev-cover portable test의 한
함수가 Python behavior가 아니라 두 source substring의 기존 줄 배치를 고정해
formatter 뒤 실패할 것을 발견했다. 해당 source-text 함수는 삭제하고 실제
initramfs 생성 behavior test와 opt-in native proof는 유지했다. 이후 전체336개
파일 format check·Ruff lint, `core-cli qualification` 1,384건 통과·7건
skip(30.61초), `oci-guest` 629건 통과·114건 skip(19.99초)을 확인했다. 이 변경은
production/runtime 계약을 바꾸거나 native KVM requirement를 완화하지 않는다.
KVM job은 `vars.PALIMPSEST_KVM_ENABLED`가 `true`이고 `[self-hosted, linux, x64,
kvm]` runner에서 실제 proof가 success일 때만 gate를 통과한다. 현재 variable은
비어 있고 job result는 skipped였으므로 이는 코드 수정으로 우회할 대상이 아니다.

Formatting remediation commit `eff7d6b728c59f039e76ae10fda625a4c8ff72b3`의 후속
`Test` run [`35025114784`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35025114784)에서
`Lint, manifests, and package`는 통과했다. 별도 `OCI filesystem proof
(privileged Linux)` job은 probe 실행 전 `sudo: uv: command not found`로 실패했다.
setup-uv가 제공한 executable을 caller shell에서 절대 경로로 해석해 `sudo`에
전달하도록 두 privileged invocation을 수정한다. proof marker·test selection·root 및
CAP_SYS_ADMIN requirement·두 process evidence 비교·artifact retention은 바꾸지 않는다.
같은 run의 native KVM skip/required-gate 실패도 이 workflow lookup 수정으로 우회하지
않는다.

Workflow lookup fix commit `8a22cf601baaac5c4596397c4ff38003827bf320`의
`Test` run [`35025567423`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35025567423)은
strict privileged probe까지 도달했다. Candidate probe와 production packer가 동일한
normalized tar digest를 만들었지만 base SquashFS digest는 각각
`ae84880f28360df6acc0564675bcbeb968530a891ad7b26c93f21f6c75f3ede9`와
`7b86d6738665683625a33cfd002d126ce67318d4927bd1315936f287d78753dd`로
달랐다. 원인은 candidate가 packer 기본 compression을 사용하고 production은 zstd
level3를 명시한 기존 argv 분기다. Deterministic fixed argument sequence를
`oci_packer` 한 곳에서 소유하고 candidate/prod 두 경로가 공유하도록 수정한다.
Production argv·contract ID·artifact bytes는 바뀌지 않고 candidate evidence만
production format으로 맞춘다. 이 run의 native KVM skip/required-gate 실패는 별도
외부 gate로 유지한다.

Alignment commit `315222840c0cfb9161c4e20a469bcf3d3ff1c92a`의 draft PR
`Test` run [`35026142507`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35026142507)에서
`OCI filesystem proof (privileged Linux)` job이 통과했다. Shared zstd argv로 candidate와
production artifact를 비교한 현재 x86_64 strict proof다. Development package run
[`35026137691`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35026137691)도
package verification과 SHA-specific prerelease publication을 모두 통과했다. 같은 Test
run의 native KVM job은 variable 부재로 skipped이고 required gate는 실패했으므로 PR은
draft/blocked 상태를 유지하며 KVM 성공을 추론하지 않는다.

같은 run `35026142507`의 portable shard 실패는 세 가지 테스트 경계 결함이었고
production 동작은 바꾸지 않았다. (1) host journal 경고가 없는 host 전용 경로
`/var/log/palimpsest` 부재로 CLI stderr 계약이 깨졌다. `tests/conftest.py`의
session fixture가 `PALIMPSEST_LOG_HOME`을 private `0700` 임시 경로로 고정한다.
`tests/unit/test_host_journal.py`는 여전히 missing/malformed/unsafe 경로로
production 경고를 요구한다. (2) macOS shard의
`NotImplementedError: dir_fd unavailable on this platform`은 `state.py`가 아니라
`tests/unit/test_runtime_dispatch.py` race harness의 `os.mkfifo(..., dir_fd=...)`
호출이었다. macOS build에 `mkfifoat`이 없을 수 있어 harness는 절대 경로로 FIFO를
만든다. reader의 pinned directory descriptor와 거부 계약은 그대로다.
(3) `test_project_callbacks_fail_closed_on_partial_or_oci_run_ledgers_before_backend_use`의
`inspect` 기대값은 stale이었다. `runtime_dispatch.inspect_run`은 state-only
projection이고 `(oci-root, kvm)`에서 허용된다. 해당 parameter를 지우는 대신
`test_project_inspect_callback_projects_oci_ledger_without_backend_probes`가
dispatch key·lifecycle·손상 ledger의 `StateError`·backend 미접근을 검사한다.
로컬 `uv run python scripts/test_lanes.py run portable` 최종 실행은 6,044개 node 중
5,827건 통과·217건 skip·7건 warning이었다. 이는 Linux/macOS CI shard 실행을 대신하지 않는다.
`PALIMPSEST_KVM_ENABLED` variable이 비어 native KVM job은 skip되고 `Required native
KVM proof` gate는 실패했다. 이 run은 runner availability를 입증하지 않으며, 변경은 gate를 우회하거나 완화하지 않는다.

Portable remediation commit `785cd02c638a339acfab9c9f1a6bcb7e97683a5e`의 최종
`Test` run [`35029001178`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35029001178)은
Linux portable 6/6, macOS portable 4/4, `Pure contracts (Python 3.12)`,
`Unit tests (macOS 15)`, lint·manifest·package, Hub, guest stage-1, local OCI
product build, privileged OCI filesystem proof를 모두 통과했다. 유일한 failure는
비어 있는 `PALIMPSEST_KVM_ENABLED` 때문에 native job이 skip된 뒤 실패한
`Required native KVM proof`다. Development package run
[`35028985909`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35028985909)은
package verification과 SHA-specific prerelease publication을 모두 통과했다. 재사용
workflow run [`35029003724`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35029003724)도
같은 portable·filesystem·package matrix를 통과했고 required KVM gate 때문에 Hub image
push는 skip됐다. 이 checkpoint는 local-only 후속 기록이며 PR을 ready로 바꾸거나 KVM
variable·runner를 활성화하는 승인이 아니다.

후속 read-only readiness 확인에서 repository variable API의
`PALIMPSEST_KVM_ENABLED` 조회는 `404 Not Found`, repository runner API는
`total_count: 0`을 반환했다. `pieroot-server`는 `x86_64`이고 `/dev/kvm`은
`root:kvm`·`0660` character device이며 SSH operator `pieroot`는 `kvm` group에
속한다. Host의 다른 principal 소유 `Runner.Listener` process는 재사용·중단·재구성하지
않았다. 이후 사용자가 persistent dedicated runner와 gate 실행을 명시적으로 승인했다.

승인 범위에서 repository runner `pieroot-server-palimpsest-kvm`(id `21`, labels
`self-hosted`, `Linux`, `X64`, `kvm`)를 `pieroot-server`의 persistent Docker
container `palimpsest-gh-runner`로 등록했다. Runner는 `2.337.0`, restart policy는
`unless-stopped`다. 공식 runner tar digest는
`sha256:70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613`, local image는
`palimpsest/github-runner-kvm:2.337.0` / `sha256:5715aa3aad4d76423f15ba4b320082ec9c7bbe1a567eec459234414b80bdb2ed`다.
Container는 privileged가 아니며 모든 capability를 drop한 뒤 setup-uv tar의
ownership·mode 복원에 필요한 `CHOWN`, `DAC_OVERRIDE`, `FOWNER`만 더한다.
`no-new-privileges`와 PID limit를 유지하고 Docker socket·host home은 mount하지
않으며 `/dev/kvm`, writable runner-state volume, read-only kernel volume만 제공한다.
Kernel volume의 root-owned `0400` single-link kernel/config digest는 각각
`sha256:89f7d4f31f6ef77d0f8d45810de9e19e3f8dededf3dd6ebf89bb540d26d8c0fd`,
`sha256:4d4aaaed367bd2fb6ed1238b94ea9bb095a5e5a0d0cc67d1cb70595643cc795a`이고 source
preflight가 두 artifact와 KVM API `12`를 승인했다. Attempt 2는 `DAC_OVERRIDE`만으로
setup-uv tar ownership을 바꾸지 못해 proof 전에 실패했고, `CHOWN`을 더한 attempt 3은
mode 변경용 `FOWNER`가 없어 proof 전에 실패했다. 최종 최소 capability 집합은 두
provisioning 원인을 제거했다.

Repository variable은 `PALIMPSEST_KVM_ENABLED=true`로 설정했다. Commit
`785cd02c638a339acfab9c9f1a6bcb7e97683a5e`의 `Test` run
[`35029001178`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35029001178)
attempt 4는 native job `104817871943`, required gate `104818817590`을 포함해 전체
성공했다. Native artifact `10448739883`은 450,650 bytes다. 재사용 run
[`35029003724`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35029003724)의
native proof도 성공해 artifact `10448829655`(450,902 bytes)를 남겼다. 같은 run의
attempt 4는 main run에서 통과한 동일 Linux 6/6 shard가
`test_console_identity_damage_never_returns_foreign_bytes_or_success[symlink]`의
0.1초 lock timeout 한 건으로 실패했다. Failed-job attempt 5에서 해당 shard,
`Pure contracts`, required KVM gate, Hub image build가 모두 통과해 최종 run은
success다. PR event의 image steps는 계약상 `push: false`이므로 registry push 성공은
주장하지 않는다.

두 artifact를 각각 내려받아 source-defined 45개 파일의 exact set과
`palimpsest.oci-stage1-kvm-proof.v20` receipt를 검사했다. 두 receipt 모두 44 QEMU
invocation·43 executed boot, native `kvm`/`x86_64`/`-cpu host`, live PID 1, KVM API
12, authenticated OverlayFS의 `/` 전환, `switch_root=true`, `pivot_root=false`, reconnect
proof와 negative-input proof를 기록한다. Runner id `21`은 현재 online/idle이고 variable도
enabled 상태다.

남은 운영 위험: repository는 public이고 fork workflow approval policy는
`first_time_contributors`다. Container isolation은 host credential·Docker socket·home
노출을 제거하지만 `/dev/kvm` attack surface와 persistent runner-state 위험은 남긴다.
모든 external contributor approval로 강화하거나 runner/variable을 정지하는 것은 별도
security/availability 결정이므로 이번 승인에서 임의로 적용하지 않았다.

또한 read-only prerequisite 확인 중 `lxc` command-not-found wrapper가 LXD snap
`5.21.7`을 자동 설치했다. LXD는 initialize하지 않았고 container·storage resource도
만들지 않았으며 native proof에는 사용하지 않았다. 제거는 별도 destructive host
mutation이므로 임의로 수행하지 않았다.




안전한 로컬 focused 명령:

```sh
uv run python -m pytest -q tests/unit/test_pci_preflight.py tests/unit/test_test_lanes.py
uv run python -m pytest -q tests/unit/test_oci_store.py::test_oci_root_define_failure_cleans_only_exact_new_owned_domain
uv run python -m pytest -q tests/unit/test_architecture_guard.py
uv run python scripts/test_lanes.py list --check
uv run python scripts/test_lanes.py plan --changed HEAD
python3 scripts/check_architecture.py
git diff --check
```

`plan --changed HEAD`의 계획은 실행 결과가 아니다. native KVM, package build,
full portable suite, server test는 변경 범위와 승인에 맞춰 별도로 실행한다.

작업 역할은 Astra가 계획·범위·architecture 및 위험 경계를 조정하고, Sol이
source/test 구현을 담당하며, 별도 reviewer/verifier가 author와 독립적으로
승인하는 형태를 유지한다. reviewer 승인은 commit, push, SSH, helper 실행,
GPU/VM mutation에 대한 사용자 권한을 대신하지 않는다.

## 설치·명령 문서 재작성 (문서 범위)

사용자 설치 경로를 checkout build에서 pip VCS URL로 바꾸고 명령별 workflow
문서와 diagram을 추가했다. 이 작업은 문서와 CLI reference 생성기 설명 문자열만
바꾸며 runtime, schema, 의존성, 테스트 정의는 건드리지 않는다.

- 설치 진입점: [`install.md`](../install.md)는 `python3.12 -m venv` 뒤
  `pip install "palimpsest-local @ git+https://github.com/openstack-afterglow/palimpsest.git@<full-SHA>"`,
  `[kvm]` extra, `palimpsest-hub @ ...#subdirectory=hub`를 사용한다.
  [`docs/install.md`](install.md)에 패키지 카탈로그, 지원 host/runtime 표,
  기능별 외부 전제, 설정 우선순위, Hub 설정 표, 관리자 설치, upgrade/uninstall,
  기여자 전용 `scripts/build_package.py`를 분리해 정리했다.
- 명령 문서: [`docs/cli/workflows.md`](cli/workflows.md)가 Hub artifact,
  registry/Docker, build, cloud-image VM, OCI-root, Compose, store/UI/completion
  순서를 실제 명령과 상태 변화, 플랫폼 제한과 함께 기술한다.
  [`docs/cli/README.md`](cli/README.md)와 `README.md`에서 연결한다.
- 발견한 문서 결함 수정: `scripts/generate_cli_reference.py`의 `run --network`
  설명과 [`docs/cli/usage.md`](cli/usage.md)가 OCI-root를 `none` 전용으로
  기술했으나 `src/palimpsest_local/oci_network.py`의 현재 계약은
  `nat`(기본)·`host-only`·`none`이다. 설명을 source에 맞추고
  `docs/cli/reference.md`를 재생성했다.
- Diagram: `docs/diagrams/`에 Archify workflow 8종의 편집 가능한 JSON과
  standalone HTML을 커밋한다. PNG/contact-sheet sidecar는 저장하지 않는다.
  재생성은 archify skill의 `deliver` + `visual-check`로 수행한다.
- 실행한 검사: `python3 scripts/check_architecture.py`(working) 통과,
  `uv run python scripts/generate_cli_reference.py --check` 통과,
  `uv run python scripts/test_lanes.py run core-cli` 1,110건 통과(29.02초),
  `uv run ruff check`/`ruff format --check scripts/generate_cli_reference.py`
  통과. Archify `deliver`는 8종 모두 showcase 9/9·오류 0, `visual-check`는
  1440×900·1600×1000·1920×1080·2048×1320 containment pass다.
- 이 작업은 native KVM, Gate 2, OCI-root 실기, GitHub 게시나 원격 helper
  전송을 실행하거나 승인하지 않는다. 기존 승인 대기 항목은 그대로다.

## Hub web API build 2026-09-19

현재 branch `codex/oci-root-phase1`, 기준 HEAD
`785cd02c638a339acfab9c9f1a6bcb7e97683a5e`이며 새 Hub 구현은 **미커밋**
working-tree 변경이다. 이전 설치·명령 문서 작업과 사용자 보유의
`docs/docker-hub-service-matrix.md`, `docs/oci-linux-process.md` 및 architecture의
MySQL/cloud-image 관련 hunk를 이 변경과 섞지 않는다. GitHub 게시, 원격
helper 전송, 서버 배포 및 실제 KVM 빌드 실행에 대한 새 승인은 없다.

### 구현된 호출 경로와 결정

1. `hub/src/palimpsest_hub/main.py`의 `/app`은 same-origin browser console이고,
   `static/hub.js`가 입력한 project-scoped Keystone token을 tab 메모리에서만
   유지한다. 기존 `/v1/layers` query/download와 `POST → PATCH(Upload-Offset) → PUT`
   upload endpoint를 그대로 호출한다. Browser는 8MiB 청크로 업로드하며 완료
   후 server SHA-256 digest를 받는다. Chromium은 streaming save picker,
   다른 browser는 64MiB 이하 메모리 download만 지원한다.
2. 새 `POST /v1/builds`는 Keystone **system-admin** + project scope, 보이는
   x86_64 raw/qcow2 cloud base와 순서/parent/base가 맞는 SquashFS chain을
   확인하고 SQL `palimpsest_hub_builds` job만 기록한다. `GET /v1/builds`와
   `GET /v1/builds/{id}`는 같은 project만 조회한다. Recipe 원문과 사용자
   identity/path를 response에 내보내지 않는다. API container는 guest command를
   실행하지 않는다. 설정되지 않은 build worker는 503, project queue 4개
   초과는 429, 생성은 6/hour로 제한한다.
3. 별도 Linux `/dev/kvm` host의 `palimpsest-hub-build-worker`가 같은 Hub
   SQL/blob path를 사용한다. singleton store flock 후 queued job을 claim하고
   입력 권한·digest를 다시 확인한다. `PALIMPSEST_HUB_BUILDER_PYTHON`에 지정한
   같은 reviewed ref의 `palimpsest-local[kvm]` interpreter가 pinned input과
   `Palimpsestfile` `FROM`/`LAYER`를 확인하고 `build_layer(network="none")`를
   guest에서 실행한다. Worker는 output size/sha256을 검증한 뒤 private layer
   metadata/grant를 commit하고 기존 `/v1/layers/{digest}/blob`에서 배포한다.
   Timeout/restart에 VM cleanup을 시도하며 확인 불가하면 `cleanup_failed`로
   worker를 중단하고 private state를 남긴다. Docker socket/chroot runner를
   Hub API에 붙이지 않는다. 비교한 Afterglow의 admin build 구조는 project
   boundary를 재사용하지 않았고 legacy global integer build id도 이식하지 않았다.
   독립 reviewer의 worker-death 결함을 반영해 child는 parent-death signal과
   fsync한 boot ID/PID/start ticks marker를 남긴 뒤에만 guest 작업을 시작한다.
   Recovery는 같은 process group을 확인·종료한 후 guest를 청소하며 identity가
   모호하거나 cleanup이 실패하면 job tree를 보존하고 중단한다.
4. 기존 upload는 project_id=NULL session 재사용을 거부하고 같은 session의
   PATCH/PUT/DELETE를 filesystem lock으로 직렬화한다. Interrupted append residue는
   committed offset으로 절단하고 fsync 뒤 DB offset을 갱신한다. Project별 활성
   session 4개를 넘기지 않고, 새 session 생성 때 24시간 idle session을 제거한다.
   DB migration은 새 build table과 기존 project layer grant table을 포함한다.
   Cancel된 flock waiter의 FD도 eventual acquisition 뒤 반환한다. 같은 digest
   등록과 project build queue cap은 store-backed lock으로 commit까지 보호한다.
   Upload는 blob 승격 후에도 별개 staged file을 보존해 DB commit 전 중단 시
   같은 offset의 PUT을 재시도한다. Browser token 교체는 진행 요청을 abort하고
   이전 project의 active upload session을 버린다. 악성 bundle digest가 다른
   기존 blob을 삭제하지 않으며 suffix byte Range를 올바르게 처리한다.

### 실행 증거와 아직 충족되지 않은 전제

Hub의 project-scoped ASGI HTTP fixture는 업로드→base 등록→build enqueue→worker
claim→private layer 등록→다운로드를 호출했다. 이 fixture는 **VM executor만**
대체하므로 guest boot·실제 SquashFS mount 증거가 아니다. 별도 host builder
unit fixture는 base byte tamper와 recipe `FROM` 불일치를 거부하고 guest
handoff의 network-none 계약을 확인한다. Local `/app` process를
`uvicorn`으로 띄워 isolated Chrome에서 desktop/mobile 레이아웃, token 연결,
build form, 청크 upload form을 확인했다. Form 작업의 API 응답은 browser
interception이므로 Keystone 인증이나 live KVM을 입증하지 않는다. Hub wheel의
static asset과 `palimpsest-hub-build-worker` entrypoint 포함도 확인했다.

수정 후에도 같은 `/app`을 local `uvicorn`과 isolated Chrome에서 다시 열어
project A upload의 PATCH를 browser interception으로 지연시키고 token을 B로
교체했다. B의 새 `POST /v1/uploads` → B session PATCH/PUT이 등록 메시지로
끝났으며 A session GET 재사용은 **0건**이었다. 요청 header의 token도 각
project에 맞았다. 이는 실제 UI client의 state 전환 관찰이며 interception
응답을 이용했으므로 Keystone·DB·KVM의 live end-to-end 실행은 아니다.
첫 UI server 기동은 필수 `DATABASE_URL`, `REDIS_URL`, `OS_*` 등 7개 설정
누락으로 시작 전에 실패했고, 비밀이 아닌 local fixture 값으로 재기동해
UI 자산만 검증했다. 실제 배포 설정·인증 성공으로 기록하지 않는다.

2026-09-19 최종 upload contract 정리 후 재실행: `cd hub && uv run pytest -q`
**58 passed**, `uv run python scripts/test_lanes.py run build-registry`
**208 passed**. 이 정리 직전의 `tests/unit/test_hub_builder.py` **1 passed**와
`uv run python scripts/test_lanes.py run core-cli` **1,110 passed**는 별도 실행 결과다.
새 test 파일을 `scripts/test_lanes.py`에 분류한 뒤
`uv run python scripts/test_lanes.py list --check`가 모든 파일을 분류했고,
`plan --changed HEAD`는 Hub 별도 lane과 portable 관련 lanes를 권고했다.
초기 `core-cli` 시도는 미분류 새 파일 3개 때문에 실행 전 실패했으며
분류 후 재실행은 통과했다. 이는 native KVM proof 결과가 아니다.

독립 read-only reviewer는 8개 실질 결함을 보고했다: bundle mismatch의 공유
blob 삭제, detached builder의 worker-death 생존, 취소된 flock waiter, blob
승격/SQL commit 사이 retry 손실, 동일 digest 동시 등록, project queue cap
경쟁, browser token switch의 stale upload, suffix Range. 위 보정과 해당
regression fixture를 같은 작업트리에 반영했다. 이 portable fixture의
`cleanup_failed` marker 테스트는 Linux kernel의 PDEATHSIG/reap과 libvirt
guest 제거를 실행하지 않는다.

최종 review marker의 working-tree source SHA-256은
`f28c16efec598365945f5d50ddd97590807371d24c9ecb3585a2141279b40897`
(398 files)이다. `python3 scripts/check_architecture.py` working과
`--staged` baseline (388 files)은 각각 통과했다. 이 guard는 `docs/`,
`ARCHITECTURE.md` 및 대부분의 root Markdown을 hash에서 제외하지만, 이전 문서 작업의
`scripts/generate_cli_reference.py` 변경은 포함한다. 따라서 이 값은 Hub만의
불변 배포 commit이나 기존 문서 변경의 review/승인이 아니다.

남은 검증은 같은 reviewed source ref를 별도 Linux KVM/Keystone/SQL/shared
storage host에 승인된 방법으로 설치하고, 실제 pin된 bootable x86_64 cloud
image의 upload와 정상 Palimpsestfile build를 한 job씩 실행해 guest receipt,
download digest, parent/base chain, libvirt guest 제거를 대조하는 것이다.
이는 로컬 portable test와 독립이며 현재 원격 helper·배포 차단을 우회하지
않는다. 완료된 순서: 독립 reviewer 지적 보정 → Hub/portable test와
architecture working/staged guard. 다음 작업은 (a) 소스와 기존 문서·예전 미승인
변경을 분리해 운영 승인 요청, (b) 승인된 Linux KVM host에서만 동일 검토
source ref로 실제 build를 검증하고 guest·digest·정리 결과와 SHA를 새 checkpoint로
기록하는 것이다. 승인 전 배포나 원격 helper 전송은 하지 않는다.
Astra 계획/오케스트레이션 모델은 이 호스트에서 직접
선택할 수 없어 Sol 구현과 독립 reviewer 검토로만 진행했으며 역할 대체를
완료된 Astra 승인으로 간주하지 않는다.

### 승인 전 변경 분리와 단일 build 실기 게이트 (2026-09-19)

현재 HEAD는 위 `785cd02c...` 그대로이고 index에는 staged 변경이 없다. Hub
runtime·test 범위는 `hub/pyproject.toml`, `hub/src/palimpsest_hub/`의
API/config/main/migrate/models/store/build worker와 `/app` static,
`hub/tests/test_builds.py`, `test_upload_limits.py`, 기존 Hub test 수정,
`src/palimpsest_local/hub_builder.py`, `tests/unit/test_hub_builder.py`,
`scripts/test_lanes.py`다. 별도 새 commit/ref나 package publication은 없다.

`ARCHITECTURE.md`, `README.md`, `install.md`, `docs/install.md`,
`docs/testing.md`, 이 handoff는 Hub hunk와 이전 작업 hunk가 섞여 있어
파일 전체를 Hub 변경으로 stage/commit하지 않는다. 이전 작업의
`docs/docker-hub-service-matrix.md`, `docs/oci-linux-process.md`,
`docs/cli/*`, `docs/diagrams/`, `scripts/generate_cli_reference.py`도
보존한다. Source guard SHA에는 마지막 Python script 변경도 포함되므로
Hub-only source provenance가 필요할 때는 원저자 변경을 침범하지 않는
별도 검토·분리 절차가 필요하다. 이 checkpoint에서는 index를 변경하지 않았다.

배포 전 확인해야 할 입력은 승인된 별도 Linux x86_64 KVM host와 실행 계정,
동일 절대 경로를 공유하며 flock/fsync가 작동하는 Hub blob store, 준비된
Keystone/SQL/Redis/HTTPS, 백업·복구 계획과 schema 변경 허가, 같은 검토
source의 Hub/Local interpreter, 실제 bootable x86_64 base 및 project-scoped
system-admin token이다. 비밀값을 이 문서나 shell 명령에 기록하지 않는다.
`PALIMPSEST_HUB_BUILDER_PYTHON`의 절대 경로만으로 API가 요청을 받으므로
worker가 살아 있음을 뜻하지 않는다. `/v1/health`도 process 상태뿐이다.
Project/store 전체 용량 quota 및 완료 build GC가 없고 staging과 blob가
동시에 디스크를 차지하므로 운영자가 용량·보존 정책을 먼저 정해야 한다.
미커밋 working tree는 pip의 Git VCS URL에서 `HEAD`로 설치할 수 없다. 현재
`785cd02c...` ref에는 이 Hub 기능이 없으므로, 운영 승인이 있더라도 먼저
코드·package digest가 고정된 동일 source artifact를 별도로 준비·대조해야 한다.
기존 queued job을 worker가 먼저 가져가지 않도록 별도 staging SQL/blob 경계와
빈 build queue를 확인하기 전에는 worker를 시작하지 않는다.

운영자의 **별도 명시 승인과 대상 지정 후**에만 (1) 기존 상태·DB 백업과
읽기 전용 `/dev/kvm`/libvirt/storage/Keystone preflight, (2) 동일 검토
source로 Hub와 별도 worker 설치·schema 준비, (3) pinned base 1건 업로드,
(4) project-scoped system-admin token으로 Palimpsestfile build 1건,
(5) output blob SHA-256·parent chain·guest receipt·domain 및 임시 상태
제거를 검증한다. Worker가
`cleanup_failed`이면 자동 재시작·job tree 삭제·기존 VM 제거 없이 중단하고
운영자가 증거를 확인한다. GitHub push/commit, private GPU helper 전송,
기존 runner 설정·장치·VM 변경은 이 단일 실기 승인에 포함되지 않는다.

### 사용자 선택: 로컬 패키징만 (2026-09-19)

사용자가 위 두 범위 중 **로컬 패키징만** 선택했다. 현재 HEAD의 root/Hub
package source를 `git archive`로 임시 스냅샷에 복원한 뒤 Hub runtime 변경
14개 파일만 덮어썼다. `git status --untracked-files=all`에서 해당 runtime
변경 집합과 overlay 집합이 정확히 일치함을 확인했다. Root `README.md`와
`pyproject.toml`은 HEAD 원본을 사용해 앞선 문서 재작성과
`scripts/generate_cli_reference.py` 변경을 package 입력에서 제외했다.
현재 작업트리와 index는 stage/commit하지 않았다.

로컬 전용 산출물은 gitignored
`dist/hub-local-only-785cd02-73690b1b404e/`의 두 wheel,
`source-snapshot.tar.gz`, `SOURCE_MANIFEST.json`, `SHA256SUMS`다.
Archive SHA-256은 `73690b1b404e761cd34edc13265c5f6945fd486bff45f6a6f692ec8ed2667d16`,
Hub wheel은 `7ea5f7cd2f3d7b9630dc428a0ffbd2e59a4c6eeaf297a0bdea0c52a3c62d5872`,
Local wheel은 `7996f4f4aa0e7beff08df9bbe9e3d833cde1e4f5b3177e1e9c256726a9a48213`이다.
`SOURCE_MANIFEST.json`은 기준 commit과 모든 overlay hash를 기록한다.
`shasum -a 256 -c SHA256SUMS`가 네 파일 모두 통과했다.

`SOURCE_DATE_EPOCH=1789509812`로 두 package를 각각 세 차례 offline
빌드했고 원본 임시 소스 및 보관 archive에서 재복원한 소스의 wheel byte가
각각 동일했다. Wheel 안의 Hub static/build worker/helper bytes와 worker
entrypoint를 확인했다. Python 3.13 임시 venv에서 dependency 설치를 생략한
offline wheel 설치, Local CLI `--help`, Hub worker entrypoint metadata도
통과했다. 이는 Linux runtime dependency 설치, Keystone/SQL 연동 또는 native
KVM 실기가 아니다. 원격 전송·호스트 설치·DB/schema 변경·VM 실행과 GitHub
게시는 승인되지 않았으며 모두 실행하지 않았다. 다음 원격 단계는 별도 명시
승인과 대상 지정, 호스트 적합성 및 빈 전용 staging queue 확인 뒤에만 진행한다.

### 후속 승인 범위와 읽기 전용 host preflight (2026-09-19)

사용자는 그 뒤 **격리 스테이징 실기 1건**을 선택하고 대상은
`pieroot-server`, 전용 Keystone·SQL·Redis·HTTPS·blob 서비스 준비 여부는
**모름**이라고 답했다. 이 선택은 앞의 로컬 전용 checkpoint 다음 승인
범위이며 기존 runner/VM 변경, GitHub 게시, private GPU helper 전송까지
허용하지 않는다. `ssh`는 BatchMode와 기존 host key 검사를 유지한
읽기 전용 명령만 사용했다.

현재 host는 Linux 6.8.0-139-generic x86_64, SSH UID 1000이 `kvm`과
`libvirt` group에 속하며 `/dev/kvm`은 `root:kvm` 0660이고 이 UID의
read/write `test`가 통과했다. `virsh -c qemu:///system` read-only 목록은
전체 domain 22개, running 0개, `builder-b-` prefix 0개다. Docker 목록은
container 34개 중 기존 `palimpsest-gh-runner` 1개이고 Hub 이름의
container 0개다. 이 domain/container를 재사용·삭제·중지하지 않았다.
원격 checkout `/home/pieroot/code/palimpsest`는 clean
`84b30f86e569aa93999e192002d3682ea6b98b8f`로 로컬 package 입력
`785cd02c...`+14 overlay와 다른 ref다. 원격 checkout을 교체하지 않았다.

`virsh`, `qemu-system-x86_64`, `qemu-img`, `mksquashfs`, `python3.12`는
PATH에 있으나 **`cloud-localds`가 없고 `cloud-image-utils`도 dpkg에
설치되지 않았다**. 현재 source의 `kvm.py`는 seed ISO 생성 시 이
executable을 필수로 호출하므로 지금 그대로는 실제 guest build가 실패한다.
`palimpsest-hub*` systemd unit과 Hub 이름의 container는 조회되지 않았다.
Host-local listener 443은 있었지만 5000/3306/6379/8020은 없었고,
Redis 유사 container 2개는 소유 범위 미확인이다. 이 사실은 외부
Keystone/SQL/Redis가 부재함을 입증하지도 기존 container 재사용을
허가하지도 않는다.

따라서 **전용 staging DB/blob 경로와 빈 queue, 비밀 주입 경로, Keystone
scope, HTTPS endpoint, 저장 용량·백업, bootable pinned base를 아직
확인하지 못했다.** 기존 공유 host에 `cloud-image-utils`를 설치할지와
독립 서비스 자원을 누가 어떻게 준비할지도 운영 결정이 필요하다.
검토한 local archive/wheel을 전송하거나 OS package 설치, schema bootstrap,
worker 시작, VM 생성은 하지 않았다. 다음은 전용 환경·OS dependency의
승인 범위 확정과 안전한 자격 증명 주입 후 빈 staging queue를 확인하는
것이며, 이때까지 실제 build 성공을 주장하지 않는다.

### 후속 선택: 신규 서비스 설계·host 도구 설치 승인 (2026-09-19)

사용자는 공유 host의 신규 staging 서비스는 **구축 범위를 먼저 설계**하고,
`cloud-image-utils` host 설치는 **승인**했다. 승인된 설치의 정확한
`apt-get -s --no-install-recommends --no-upgrade --no-remove install
cloud-image-utils` 계획은 Ubuntu 24.04 `genisoimage`와
`cloud-image-utils` **신규 2개**, upgrade 0·remove 0이었다. 하지만
`sudo -n true`는 `sudo: a password is required`로 실패했고
`sudo -n -l`에는 apt 관련 NOPASSWD 권한이 없다. 비밀번호를 채팅·명령에
요청하거나 우회하지 않았으며 **설치는 미실행**이다. 운영자가 안전한
관리 경로에서 이 두 package를 설치하거나 비밀을 노출하지 않는 권한
경로를 지정해야 한다. 설치 후 `cloud-localds` PATH와 실제 version을
별도로 확인한다. APT simulation은 변경을 적용하거나 재부팅·서비스
재시작 안전성을 입증하지 않는다.

다음은 **설계안이지 설치 지시나 실기 증거가 아니다**. 권장 topology는
**기존 Actions runner가 없는 신규 전용 x86_64 Linux KVM host**에 Hub build
worker와 일회성 guest를 두고, 다른 신규 staging 서비스 VM/host에 Hub API·
Keystone v3·MySQL·Redis·TLS endpoint를 두는 것이다. `pieroot-server`에는
`palimpsest-gh-runner`가 이미 KVM과 libvirt system URI 및 CPU·RAM을
공유한다. 별도 UID/container/volume만으로 그 runner와 VM·자원 격리를
보장할 수 없으므로 이 host를 권장 worker 배치로 쓰지 않는다. 기존 443
listener, runner container, 22개 inactive domain, 소유 미확인 Redis
container는 사용·수정하지 않는다. Keystone은 별도 test domain/project,
project-scoped token 및 system scope의 admin role assignment가 필요하며
Hub service identity가 그 assignment 조회를 허가받아야 한다. Hub와
Keystone DB는 별도 빈 database/user이고 Redis도 전용 namespace여야
한다. Hub `/app`와 `/v1`은 같은 HTTPS origin으로만 노출하고 SQL/Redis는
비공개 network에 둔다. 서비스 비밀은 staging 전용 identity로 주입한다.

API와 worker는 **같은 Hub SQL DB와 같은 절대 blob path**를 본다.
공유 filesystem은 cross-host `flock`, atomic rename, fsync 동작과
사용량·백업을 별도로 검증해야 한다. 현재 lock 파일은 0600이므로 API와
worker가 같은 숫자 UID로 이를 열 수 있어야 한다. Group 권한만 주는
설계는 동작하지 않는다. API에는 Hub wheel만 설치하고 `/dev/kvm`,
libvirt socket, Docker socket을 제공하지 않는다. 신규 전용 worker
host에만 `qemu:///system`과 검토된 Hub·Local runtime을 설치한다.
전용 host를 제공할 수 없다면 `pieroot-server` 사용은 **대안이 아닌 별도
운영 결정**이다. 기존 runner의 명시적 drain/fence와 실행 전후 domain·
container·resource inventory 대조, CPU/RAM/스토리지 한도와 소유권 승인
없이는 worker를 배치하거나 게스트를 시작하지 않는다. 현재 runner 중지·
등록 해제·도메인 변경 승인은 없으며 이 경로도 실행하지 않는다.

신규 전용 host의 시작 조건은 자원 소유자·network/HTTPS route·CA,
비밀 주입 방법, 전용 empty DB/blob/queue, 동일 UID와 공유 파일 잠금,
bootable pinned x86_64 base image digest, disk headroom(업로드 staging +
blob + VM overlay), 백업과 cleanup/보존 정책의 명시다. 현재 보관된
`dist/hub-local-only-785cd02-73690b1b404e/`의 두 wheel은 이전 source
snapshot만 담은 **무의존성 설치 검사용** 산출물이다. 이후 수정 source와
일치하지 않고 Hub의 전이 의존성 및 `palimpsest-local[kvm]`이 요구하는
Linux-native `libvirt-python` closure도 없다. 전송·설치 가능한 package로 간주하지 않는다.
실제 배포 시에는 검토된 단일 source로 Linux x86_64/Python ABI용 Hub/Local wheel 및 전이 의존 wheel
전체를 잠그고 각 byte의 SHA-256·출처·ABI를 확인한 offline wheelhouse,
그리고 root-owned host binary와 `cloud-localds`가 필요하다. Host package
설치 후에도 별도 interpreter의 `python -I -m palimpsest_local.hub_builder
preflight`가 `/dev/kvm`/libvirt, tool ownership 및 x86_64 호환성을
통과하기 전에는 worker가 SQL을 열거나 queue를 claim하지 않는다.

로컬 source는 worker 전용 SQL/blob 설정으로 Keystone·Redis secret의
불필요한 전달을 제거하고, blob file/ancestor fsync가 실패하면 publication을
중단하며, 완료 직후의 crash로 남은 private job tree를 다음 시작에서
검출·정리하도록 보정했다. 이 진술은 source/portable test 범위이고 실제
host filesystem·guest teardown 검증이 아니다. 빈 schema bootstrap과
worker 시작은 위 전제와 **별도 명시적 배포 승인** 뒤의 순서이며 기존 DB
migration, runner stop/unregister, 기존 domain 삭제는 범위 밖이다.
서비스 기동 뒤에도 `/v1/health`만으로 준비됐다고 간주하지 않고 Keystone
token/SQL/blob reachability, 실제 one-job output digest, owned libvirt
guest 및 private scratch 정리를 각각 확인한다. Host 공급·package closure·
접근 권한·실기 범위가 준비되기 전에는 artifact 전송/설치·서비스 생성·DB
bootstrap·VM build를 하지 않는다.

독립 read-only staging reviewer가 runner-host 병치 격리 불성립,
worker preflight·dependency closure·worker secret 범위·CAS fsync·완료
scratch 누수의 6개 위험을 지적했다. 위 topology 및 source 보정은 이를
반영한 로컬 변경이다. Astra 계획 모델은 여전히 이 호스트에서 선택할
수 없어 Sol 구현과 독립 검토만 수행했고 Astra 승인으로 대체하지 않았다.

현재 working source digest는 `ARCHITECTURE.md` marker의
`f2d8dada271e334bed67a864d176fdde73ee841390be27b1b6e2170ae1ca049b`다.
`hub` 집중 52개/전체 62개, `build-registry` 208개, `core-cli` 1110개,
`test_hub_builder.py` 1개가 macOS portable에서 통과했다. `ruff check`,
`scripts/test_lanes.py list --check`, architecture working/staged check도
통과했다. macOS의 실제 `python -I -m palimpsest_local.hub_builder preflight`
실행은 의도대로 `builder host preflight failed` / exit 1이었다.
`uv build --offline --wheel`로 현재 **dirty working tree**에서 별도
`dist/hub-local-only-785cd02-f2d8dada/`에 두 application candidate를
빌드하고 `SHA256SUMS`를 검증했다. Hub wheel SHA-256
`035779691d0f3fbc762825beca1f232f860d7b359dcb3a939c71bca213ec2beb`,
Local wheel SHA-256
`97df5045cae2a4ba1323efcdda5d887647de7296dad6c094744c897a10f708f6`.
`SOURCE_MANIFEST.json`은 HEAD·working-source digest·한계를 기록한다.
이 wheel은 이전 후보도, clean-ref 재현 archive도, Linux 의존 closure도
아니므로 **전송·운영 설치 산출물이 아니다**. 원격 설치·서비스/DB 변경·
VM 기동·GitHub 게시·private helper 전송은 모두 하지 않았다.

정확한 재개 순서: 운영자가 새 전용 x86_64 KVM host 및 전용 서비스
VM/host·network/identity/store 자원을 지정한다(또는 runner drain/fence와
자원 대조를 별도 승인한다). 승인된 `pieroot-server`의
`cloud-image-utils` 설치는 sudo 비대화형 경로가 없어 미실행이며 운영자
관리 설치 또는 비밀 노출 없는 설치 채널이 필요하다. **새 전용 host의 OS
package 설치는 이 승인에 포함되지 않는다**. 선택한 worker host에서
`cloud-localds`를 확인하고, 같은 검토 source의 Linux-native hashed
dependency closure, 정확한 worker preflight, 빈 staging DB/blob/queue와
공유 FS 잠금을 확인하고, **별도 배포/실기 승인** 후에만 schema bootstrap,
서비스 기동, pinned base 1건·build 1건·digest/guest/scratch 정리를
실제로 관찰한다. 기존 runner/DB/domain은 그 절차에 포함되지 않는다.

### 직접 Git 설치와 branch publication 승인 (2026-09-20)

사용자는 Python package를 repository URL 자체로 설치할 수 있게 하고,
README에 pip와 uv 절차를 설명한 뒤 현재 작업을 commit/push하여 `dev`와
`main`에 모두 merge하라고 명시했다. 이 지시는 이번에 검증한 repository
변경의 GitHub publication 및 두 target branch merge 차단을 해제한다.
Private helper 전송, host package/서비스 설치, DB 변경과 KVM 실행 승인은
포함하지 않는다.

게시 전 원격 default branch에서 정확한 unpinned
`python3.12 -m pip install
"git+https://github.com/openstack-afterglow/palimpsest"`와 throwaway uv
project의 `uv add "git+https://github.com/openstack-afterglow/palimpsest"`
를 각각 실행했다. 두 경로 모두 `palimpsest-local==0.1.0.dev0`를 설치하고
`palimpsest --version`/`uv run palimpsest --version`이 `0.1.0.dev0`을
반환했다. uv는 당시 default commit
`13f06562357709612f1c3532563a8577431e1e3d`를 lock했다. Full SHA
`785cd02c638a339acfab9c9f1a6bcb7e97683a5e`를 URL 뒤에 붙인 pip/uv
설치도 별도 통과했다. 생성한 네 throwaway environment는 제거했다.
Package metadata는 이미 repository-root VCS build와 `palimpsest`
entrypoint를 제공하므로 바꾸지 않고 `README.md`, `install.md`,
`docs/install.md`에 unpinned quick path, SHA pin, uv lock 동작을 명시했다.
Unpinned URL은 moving default branch이므로 운영 재현성에는 full SHA를
사용한다.

`origin/dev` 통합 (2026-09-20): 게시 전에 `origin/dev`를 작업 branch로
merge했다. Conflict 16개 중 formatting-only 및 centralization 충돌은 현재
source 계약(`runtime_dispatch.platforms` 경유 backend 선택, `inventory`
단일 mutation 경로)을 유지하는 쪽으로 해결하고, dev의 package 사실
(`palimpsest-local 0.1.4`, `palimpsest-hub 0.1.3`, `requires-python >=3.11`,
root wheel의 Kolla role shared-data, `docker/hub/Dockerfile`)은 그대로
받았다. 위 설치 문서의 version·Python 하한도 이 값으로 맞췄다. 앞의 설치
검증이 보고한 `0.1.0.dev0`은 당시 원격 default branch(main) 값이며, 이
merge 이후 같은 명령은 `0.1.4`를 설치한다.

Merge가 `tests/unit/test_inventory.py`의 `import json`을 자동으로 떨어뜨려
`ruff`가 F821로 잡았고 복구했다. dev의 host isolation fix는
`project_adapter.platforms`를 mock했는데 현재 source에는 그 attribute가
없어 두 test가 AttributeError로 실패했다. 같은 의도를
`project_adapter.runtime_dispatch.platforms.detect_host` mock으로 옮겼다.
dev가 추가한 `tests/test_kolla_assets.py`,
`tests/test_kolla_palimpsest_image_ref.py`,
`tests/test_kolla_role_contracts.py`는 `scripts/test_lanes.py`의 portable
`core-cli` lane에 명시 분류했고, release workflow의 unit 단계도 그 세
파일을 포함하도록 dev 쪽 명령과 기존 guest/ELF·BuildKit 증거 단계를 합쳤다.

게시 결과 (2026-09-20): merge commit `1a23f9c11763eb75bf554d0614fdb709c7585931`을
`codex/oci-root-phase1`에 만들고 원격에 push한 뒤, 같은 commit을 `dev`와
`main`에 fast-forward로 게시했다. 세 ref가 모두 이 SHA를 가리킨다. dev는
다른 worktree(`/tmp/palimpsest-root-package`)가 checkout 중이라 local
branch를 건드리지 않고 `git push origin codex/oci-root-phase1:dev`로
올렸다. 게시 전 검사는 `ruff check .`/`ruff format --check .`, 전체
portable 8 lane 5,851건 통과·217건 skip, Hub 62건 통과, lane manifest,
generated CLI reference, architecture working/staged check다. 게시 후 실제
`main`에서 unpinned pip와 uv 설치를 다시 실행해 두 경로 모두
`palimpsest-local 0.1.4`를 설치하고 `0.1.4`를 보고했으며 uv는 `1a23f9c`를
lock했다. Native KVM, Gate 2, Keystone staging, Kolla 실제 배포와 Hub
image publication은 여전히 검증되지 않았다.

게시 후 cleanup 순서 보정 (2026-09-20): 독립 검토가 `_run_guest_build`의
timeout·nonzero-exit 경로가 caller의 process-group fence 이전에
`_cleanup_guest`를 호출한다고 지적했다. `child.wait()`는 leader 종료만
증명하므로 descendant가 살아 있는 동안 guest state를 회수할 수 있다.
두 inline 호출을 제거해 실패를 그대로 전파하고, 기존
`_stop_interrupted_builder` → `_cleanup_guest` 경로만 cleanup을 수행한다.
회귀 2건을 추가했다. 실제 nonzero-exit builder 실행에서 cleanup이
호출되지 않아야 하고, group 정지를 확인하지 못하면 cleanup 없이
`cleanup_failed`와 job tree 보존으로 멈춰야 한다. 보정 전 code로
되돌려 첫 회귀가 실패함을 확인한 뒤 원복했다. Hub 집중 7건과 전체
62건이 통과했으며 이는 Linux group reaping·libvirt teardown 실기가
아니다.

게시 후 CI 결과 (2026-09-21, commit `22fc4046c796d8b34ebed5fe41100c0a7495565a`):
`main`의 `Test` run `35601184854`는 19개 job 전부 success였고 여기에는
`Native KVM stage-1 proof`와 required `Required native KVM proof`,
privileged OCI filesystem proof, guest stage-1 binary, local OCI product
build, Hub job, Linux 6/6·macOS 4/4 portable shard가 포함된다. `main`의
`Build and Push Palimpsest Hub` run `35601184858`도 image build/push까지
success다. `Development package`는 branch run `35601173619`에서 success로
`package-22fc4046c796d8b34ebed5fe41100c0a7495565a` prerelease(wheel,
sdist, `SHA256SUMS`)를 게시했고, 같은 SHA를 `main`에 올려 다시 실행된
run `35601184840`의 publish job은 `gh: Reference already exists (HTTP 422)`로
실패했다. 이는 기존 tag/release를 덮어쓰지 않는 workflow 정책이 동작한
결과이며 새 결함이 아니다. `dev`의 같은 workflow run은 concurrency로
cancelled였다. 동일 SHA를 세 branch에 게시하면 이 중복 publish 실패가
반복된다.

### Development-package recovery source checkpoint (2026-09-22; remote unverified)

`development-package.yml` now serializes by ref rather than SHA, so the three
allowed branch runs are no longer cancelled solely for sharing one commit. Its
publish job checks out the helper source, retains the transferred-artifact
checksum guard, then calls `scripts/publish_development_package.py`. The helper
uses `gh` to create-or-verify the immutable lightweight tag and prerelease:
the exact SHA, prerelease metadata, exact asset names, and downloaded
SHA-256 bytes are required for success. Tag/release creation conflicts are
re-read; an otherwise exact partial release may receive only its missing
expected assets. A different tag, metadata, extra/duplicate asset, or changed
bytes fails closed. It neither force-updates/deletes a ref nor deletes/clobbers
an asset.

`tests/unit/test_publish_development_package.py` provides fake-`gh` behavioral
contracts for these states, and the workflow invokes it directly.

### Development-package and Hub resource-boundary checkpoint (2026-09-22; remote unverified)

The helper now judges every mutation by re-reading remote state instead of by
its own exit status, so a tag, release, or asset creation whose response was
lost still converges; the original error surfaces only when the re-read remains
incomplete. Because ref-scoped concurrency lets three branches publish one
commit simultaneously, an asset another run is still uploading is awaited until
GitHub reports it `uploaded` and is never re-uploaded; a release that never
converges fails closed.

Two independent reviews then found three real defects in that Hub work, each
now fixed and covered:

1. The bounded worker gate waited for its semaphore inside the same thread pool
   that later ran the admitted work, so enough concurrent callers deadlocked the
   pool. A throwaway probe reproduced the hang with 64 concurrent calls.
   Admission moved to an `asyncio.Semaphore`, and flock waits moved to a
   dedicated executor so a lock waiter can never occupy a work thread.
2. Rollback deleted CAS bytes after any pre-commit exception, including an
   ambiguous commit that had actually landed, and it ran after the digest lock
   was released. Compensation now runs inside the lock and deletes only when SQL
   confirms no layer row references the digest; an unreadable database retains
   the bytes.
3. Bundle parsing re-decompressed the whole stream for each metadata read and
   each blob, and let `tarfile` read PAX/GNU extension payloads before any
   ceiling applied. A supported compressed bundle is now expanded once into a
   bounded seekable spool, a direct 512-byte physical header pre-scan charges
   every member payload to the expansion budget and caps extension members at
   4 MiB, and layer `mediaType` is preserved and validated as `HubLayerMeta`
   before registration instead of being hardcoded to SquashFS.

The ticket issuance response is now `Cache-Control: no-store` as well, the three
Hub ceilings are exposed as Kolla role variables, and `ARCHITECTURE.md` carries
one authoritative table of fixed and configurable Hub limits.

Executed locally on this checkpoint: `hub/` `ruff check .`, `ruff format
--check .`, and `uv run pytest -q` (87 passed, including HTTP bundle-import
proofs for chain registration, cloud-image descriptor preservation, contradictory
descriptor skipping, and a 413 expansion ceiling); root `ruff check .`, `ruff
format --check .`, `scripts/test_lanes.py run portable` (5,862 passed, 217
skipped), the publication/workflow/lane/architecture-guard and Kolla role
contracts, `scripts/test_lanes.py list --check`, and
`scripts/check_architecture.py`, which is stamped. No GitHub API, workflow run,
artifact download, tag, release, remote publication, real Redis/MySQL, large
compressed-tar, or native KVM behavior was exercised; do not present this
checkpoint as a green CI or remote publication result. Remote publication
remains blocked pending explicit approval.

Follow-up review closed two further defects in the same change:

4. Moving the worker gate to `asyncio.Semaphore` left lock waiting on a
   dedicated pool, which merely relocated the hang: fill that pool with waiters
   for a held lock and the owner's unlock queues behind them. A probe confirmed
   the previous design never completed 40 same-lock contenders. Every lock is
   now taken with a single `LOCK_NB` attempt plus event-loop backoff, unlock runs
   inline without a thread, and blob GC skips a locked blob instead of waiting.
   Acquisition is therefore not FIFO; per-project session and build caps bound
   contention. The regression runs 40 contenders on one lock, each acquiring a
   second lock and a bounded worker while holding the first, against a two-thread
   default executor.
5. `export_bundle(include_base_image=True)` referenced only the leaf config in
   the manifest, so every ancestor — including that base cloud image — was
   re-imported with an empty config, defaulted to SquashFS, and a qcow2 base was
   then skipped outright. Each layer descriptor now carries its own config blob
   digest as `dev.afterglow.palimpsest.config-digest`, and import restores that
   config after verifying its content digest and its agreement with the
   manifest-order parent. An export-to-import round trip over HTTP proves a
   cloud-image base plus two descendants all register with their real
   `kind`/`disk_format`/`arch`/`chain_id`/parents.

Hub verification for this follow-up: 91 passed, `ruff check .` and
`ruff format --check .` clean.


Python 3.10 host의 uv 설치 실패 보정 (2026-09-22): 사용자가 Ubuntu의
system Python 3.10.12에서 plain `uv init` 후 Git URL을 `uv add`하자 새
project의 `requires-python = ">=3.10"` 범위와 `palimpsest-local >=3.11`
계약이 교차하지 않아 resolver가 거부했다. 이 실패는 current interpreter만
보는 문제가 아니라 uv가 application의 전체 지원 범위를 해석한 결과다.
이어 실행한 `uv run palimpsest --version`의 `Hello from palimpsest!`는
실패한 dependency가 아니라 `uv init`이 만든 local application 출력이므로
설치 증거가 아니다.

최소 지원 버전을 실제 CPython 3.11.15로 검사하자 resolver와 wheel 설치는
통과했지만 `oci_changeset.py`의 `class ChangesetMember[PayloadT]`에서
`SyntaxError`가 발생했다. 같은 Python 3.12 PEP 695 문법은
`oci_changeset.py`, `oci_materializer_worker.py`, `oci_tar_emitter.py`에 있었다.
Python 3.11 Kolla control-node 계약을 폐기하지 않고 `TypeVar`/`Generic`
표현으로 보정하고 Ruff target을 `py311`로 내렸다. CPython 3.11.15에서 세
파일 compile, changeset/worker 집중 102건, `palimpsest --version`, sdist→wheel
build, 격리 wheel import와 `uv tool` 설치/`--help`가 통과했다. Main test
workflow에도 `uv run --python 3.11 --no-dev palimpsest --version`을 추가해
같은 syntax regression을 차단한다.

첫 full portable Python 3.11 run은 deep JSON fixture에서 한 건 실패했다.
3.11의 `json.loads`가 nesting limit을 `JSONDecodeError`가 아닌
`RecursionError`로 보고해 `_strict_json_load`의 `StateError` fail-closed
경계를 빠져나왔다. Parse와 canonical re-encode의 `RecursionError`를 같은
invalid-JSON `StateError`로 정규화했다. 기존
`test_strict_reader_rejects_deep_json_and_observed_replacement`가 보정 전
실패하고 보정 후 통과했으며, 재실행한 Python 3.11 portable 전체 결과는
5,851 passed / 217 skipped다.

문서는 CLI-only 경로를 `uv python install 3.11` + isolated
`uv tool install --python 3.11`로 안내한다. Project dependency가 필요하면
new project는 `uv init --bare --python 3.11` 후 `uv python pin 3.11`을 쓰고,
이미 `>=3.10`으로 초기화한 project는 `requires-python`을 `>=3.11`로 높인
뒤 `uv add`해야 한다. Pin만 바꾸거나 resolver hint의 `--frozen`을 쓰는
것은 지원 범위를 고치지 않는다.
`importlib.metadata.version("palimpsest-local")` probe가 local command
충돌과 실제 설치를 구분한다.

게시 결과 (2026-09-22): 보정 commit
`279f6a1371fd5c3cc6882af3ef23808722fa8fd1`을
`codex/oci-root-phase1`, `dev`, `main`에 모두 fast-forward 게시했다. 실제
remote `main`으로 Python 3.10 project fixture의 floor를 `>=3.11`로 보정하고
CPython 3.11.15를 pin한 뒤 unpinned Git URL을 `uv add`했다. uv lock은 정확히
`279f6a1`을 선택했고 distribution metadata와 `palimpsest --version`이 모두
`0.1.4`를 반환했다. 별도 `uv tool install --python 3.11` remote 설치도 같은
commit/package와 `0.1.4`를 확인했다. 생성한 proof environment와 artifact는
제거했다.

`main` Test run `35622426871`은 minimum-Python smoke가 포함된 lint/package,
Linux 6/6·macOS 4/4 portable, Hub, OCI filesystem, guest binary, local OCI
product, native KVM 및 required KVM gate를 포함한 19개 job 모두 success다.
Hub image run `35622426916`도 build/push까지 success이고, work-branch
Development package run `35622416954`는 verify와 SHA-specific prerelease
publication 모두 success다.

### CI critical-path checkpoint (2026-09-24)

기준 SHA는 `60fa42f`(= 당시 `origin/dev` = `origin/main`)이고, branch `ci-perf`에 local commit `2e37538`과 review 반영 commit으로 남겼다. 이 기록 시점(2026-09-24)에는 push·PR·저장소 설정 변경을 하지 않았다. push하면 이 문단에 push한 SHA를 적는다.

**변경 내용**

- `.github/workflows/test.yml`에서 `portable-linux`의 `max-parallel: 3`과 `portable-macos`의 `max-parallel: 2`를 제거했다. 이제 6+4 shard가 한 wave로 시작한다.
- 다음은 바꾸지 않았다: shard 수, job id와 순서, aggregator 이름과 판정, trigger, native KVM 필수 gate.
- `tests/unit/test_test_lanes.py`에 CI 형태 계약 두 개(3 node)를 추가했다.
  - shard 목록 `[1..N]`, `--shard …/N` 분모, `max-parallel` 부재 또는 N 이상, `fail-fast: false`, aggregator 이름·`if: always()`·`needs`를 고정한다.
  - gate job이 앞에 없는지와, self-hosted job이 `kvm` 하나뿐인지를 고정한다.
  - 임시 변형 workflow 네 가지(macOS cap 2, 분모 5, shard 5개, `needs: checks`)를 모두 잡는 것을 확인했다.
- `AGENTS.md`에 `CI 파이프라인 성능 규정` 12개를 추가했다.
- `ARCHITECTURE.md`(Development and verification, Change guide, Maintenance)와 [testing.md](testing.md)를 갱신하고 stamp했다.

**실측 기준.** 읽기 전용 `gh` 조회로 얻은 수치다.

- 표본은 현재 19-job 형태의 `Test` 완료 실행 21건이다.
- 크리티컬 패스는 중앙값 325초, p90 781초였다. burst 8건을 제외하면 303/346초다.
- `Unit tests (macOS 15)`가 21건 중 19건에서 마지막으로 끝났다.
- 두 번째 wave 대기: macOS 3/4·4/4가 151/170초, Linux 4–6이 91–105초.
- KVM job은 140초 걸렸고 대기 중앙값은 142초였다.

**기대 효과.** dev/PR 약 155–170초는 **추정**이다. push 후 20회 이상 재측정해야 하며, 재측정 전에는 효과로 기록하지 않는다. main push는 같은 SHA의 dev 실행과 단일 KVM runner·macOS 5개 한도를 두고 경쟁하므로 약 290초에 머물 것으로 본다.

**실행한 검사.** 모두 local macOS, Python 3.12에서 실행했다.

- `check_architecture.py`: stamp 후 working 통과.
- `uv sync --frozen --extra dev`, import smoke, fixture `--check`, CLI reference `--check`, `ruff check .`, `ruff format --check .`(351 files), `test_lanes.py list --check`: 통과.
- `plan --changed HEAD`: core-cli와 qualification을 선택했다.
- `build_package.py`: 통과. Python 3.11 `palimpsest --version`: `0.1.4`.
- focused 4 파일: 132 passed.
- `run core-cli qualification`: 1,424 passed, 7 skipped.
- portable 6 shard를 순차 실행해 합계 6,084 node, 5,867 passed, 217 skipped, 실패 0이었다.
- `check_filesystem_fixtures.py`: 통과.
- Hub(`hub/`의 sync, import, ruff, format, pytest 91 passed, `uv build`): 통과.
- `actionlint`: 기존과 같은 custom label `kvm` 경고 1건만 남았다.

**실행하지 않은 검사**

- Docker가 필요한 gate: BuildKit named OCI context, guest stage-1 binary, workload proof ELF 재현, Hub Docker image build. 이후 별도로 순차 실행할 예정이다.
- 권한이 필요한 OCI filesystem proof, native KVM, GitHub 실행.

**독립 검토 1차(2026-09-24).** `2e37538`에 대한 독립 검토가 medium 2건과 low 7건을 지적했다. 후속 commit `fix: address CI review round 1`에서 모두 반영했으며, 반영분에 대한 재검토는 아직 받지 않았다.

- **KVM runner 노출 근거 정정(medium).** 처음에 "공개 PR 코드가 KVM runner에서 실행됐다"고 인용한 run 세 건은 모두 같은 저장소 branch에서 온 PR 실행이었다(`35600862976`은 `dev`, `35600812317`·`35029001178`은 `codex/oci-root-phase1`; `head_repository = openstack-afterglow/palimpsest`). 2026-09-24 읽기 전용 조회에서 API가 나열한 `pull_request` 실행 22건 중 fork에서 온 실행은 없었다. 위험은 잠재적이다. 아래 승인 대기 1을 그렇게 고쳐 적었다.
- **aggregator 판정 계약(medium).** 처음 계약은 aggregator의 이름·`if: always()`·`needs` 포함 여부만 봤다. 그래서 verdict를 `true`로 바꾸거나 KVM skip을 받는 변형이 통과했다. 이제 다음을 고정한다.
  - 정확한 `needs`
  - 단일 verdict step의 `env`와 성공만 받는 `run` 문자열
  - `shell`·`defaults`·`continue-on-error`의 부재
  - aggregator 밖 job-level `if:`가 `kvm`의 변수 조건뿐인지
- **runner 판정(low).** `runs-on`의 mapping(`group`·`labels`) 형태까지 정규화한다. 범위를 모든 workflow로 넓히고 allowlist를 `test.yml`의 `kvm`과 `release.yml`의 `kvm-proof`로 두었다. `release.yml`이 `v*` tag push 전용인지도 고정한다.
- **규정 문구(low).** 다섯 가지를 고쳤다.
  - AGENTS.md 규칙 10을 `Test` workflow 범위로 한정했다.
  - runner group 제한은 저장소 수준 runner에는 쓸 수 없다고 적었다. `pieroot-server-palimpsest-kvm`은 저장소 수준 runner이고, org plan은 `free`이며, runner-group API는 403이었다.
  - 규칙 8의 PR diff를 merge-base(세 점) 기준으로 바꿨다.
  - 규칙 5의 wrapper 문장을 고쳤다.
  - 규칙 12의 node 수 기준에 출처를 붙였다.
- **node 수 기준.** 6,081은 `60fa42f`의 CI run `35826465548`에서 "Lane shard" 줄의 선택 node를 더한 값이다. Linux 6 shard와 macOS 4 shard의 합계가 같으며, pass 수가 아니다. 6,084는 `2e37538`의 로컬 순차 6 shard 합계이고, 계약 3 node를 더한 값이다.
- **변형 검사.** 임시 workflow 변형 14가지를 새 계약이 모두 잡았고, 끝난 뒤 `.github/`를 원복했다. 변형은 다음과 같다.
  - `pure`·`unit-macos` verdict를 `true`로 바꾸기, `pure`에 `|| true` 붙이기
  - KVM verdict를 `!= "failure"`로 바꾸기
  - `pure`의 `needs`에서 `checks` 빼기
  - KVM step에 `shell: bash {0}` 넣기, KVM env에서 `PALIMPSEST_KVM_ENABLED` 빼기, workflow `defaults.run.shell` 넣기
  - `portable-linux`에 `continue-on-error`나 job-level `if:` 넣기
  - `runs-on: {group: kvm-group}`, `runs-on: kvm`, 다른 workflow에 self-hosted job 넣기
  - `release.yml`에 `pull_request` trigger 넣기
- **반영 뒤 검사.** 모두 local macOS에서 실행했다.
  - `ruff check .`, `ruff format --check .`(351 files), `test_lanes.py list --check`: 통과.
  - focused 4 파일(`test_test_lanes.py`, `test_oci_convert_security.py`, `test_development_package_workflow.py`, `test_architecture_guard.py`): 136 passed.
  - `run core-cli qualification`: 1,435 node, 1,428 passed, 7 skipped.
  - portable `--collect-only`: 6,088 node.
  - `actionlint`(`test.yml`, `release.yml`): 기존 custom label `kvm` 경고 2건만 있었다. workflow 파일은 바꾸지 않았다.
  - 전체 portable 6 shard는 다시 실행하지 않았다. 바뀐 test 파일은 `core-cli` lane 하나뿐이다.

**승인 대기·소유자 결정.** 구현하지 않았고 설정도 바꾸지 않았다.

1. self-hosted KVM runner `pieroot-server-palimpsest-kvm`의 잠재 노출이다. 위에 인용한 run 세 건은 모두 같은 저장소의 trusted branch에서 온 PR 실행이다. 그러나 승인 정책이 `first_time_contributors`이므로, 이전에 merge된 기여가 있는 외부 contributor의 fork PR은 승인 없이 이 persistent runner에서 실행될 수 있다. `kvm` job에는 event 제한이 없고 같은 저장소 PR에서도 실행된다. 위 승인 대기 2번과 같은 항목이며 YAML `if:`로는 해결되지 않는다. 지금 쓸 수 있는 통제는 `all_external_contributors` 정책과 runner stop·unregister다. runner group 제한을 쓰려면 먼저 runner를 org runner group으로 옮겨야 한다.
2. 같은 SHA를 dev와 main에 3–6초 간격으로 push하는 문제다. 현재 형태 push 실행 14건 중 7건이 이 경우였고, 매번 KVM 대기 142–144초가 생겼다. dev가 green이 된 뒤 main을 fast-forward하는 방식 등으로 줄일 수 있다.
3. 다음 항목은 근거 부족이나 순이득 부족으로 보류했다.
   - pytest-xdist 도입: hermeticity가 미증명이고 "Lane shard" 증거 줄이 사라진다.
   - PR/push tree dedup: pre-job 비용이 절감보다 크다.
   - `hub-docker` 게이팅 변경: 테스트 크리티컬 패스 밖이다.

**다음 작업**

1. review 1차 반영분의 독립 재검토를 받는다.
2. 승인된 경우에만 push하고, 이 절 첫 문단에 push한 SHA를 적는다.
3. push 뒤 `Test` 20회 이상의 크리티컬 패스 중앙값·p90을 재측정해 이 절과 AGENTS.md 기준을 갱신한다.
4. 위 승인 대기 1·2의 결정을 받는다.

## 빠른 링크 맵

| 질문 | 먼저 읽을 곳 |
| --- | --- |
| 전체 구조·증거 marker·계약 | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| 어떤 테스트를 언제 실행하는가 | [testing.md](testing.md) |
| OCI가 실제 `/`가 되는 증거 | [oci-root-proof.md](oci-root-proof.md), [Gate 2 acceptance](oci-root-build-run-acceptance.md) |
| conventional VM와 Docker wrapper | [compatibility.md](compatibility.md) |
| 익명 registry archive 취득 | [registry-intake.md](registry-intake.md) |
| 공식 service image 결과 | [docker-hub-service-matrix.md](docker-hub-service-matrix.md) |
| TensorFlow/PyTorch CPU 현황 | [oci-ml-compatibility.md](oci-ml-compatibility.md) |
| GPU/OpenStack/PCI 경계 | [oci-gpu-support.md](oci-gpu-support.md) |
| Linux state·installer·journal | [linux-storage-logging.md](linux-storage-logging.md) |
| guest process·PID1·stdio | [oci-linux-process.md](oci-linux-process.md) |
| retained root와 shared volume 구분 | [oci-retained-root-inventory.md](oci-retained-root-inventory.md) |
| 사용자 설치와 패키지 선택 | [`../install.md`](../install.md), [install.md](install.md) |
| 명령별 실행 순서와 diagram | [cli/workflows.md](cli/workflows.md), `docs/diagrams/` |
