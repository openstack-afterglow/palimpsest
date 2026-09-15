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
- base Python package는 Python 3.12+이고 필수 runtime dependency가 없다.
  Linux libvirt는 `[kvm]` extra다. registry intake는 외부 Skopeo 1.13+가
  필요하다. wheel/sdist 성공은 host account provisioning, KVM, packaged ELF,
  native image proof를 대신하지 않는다.

세부사항은 [linux-storage-logging.md](linux-storage-logging.md),
[oci-linux-process.md](oci-linux-process.md), [testing.md](testing.md)를 본다.

현재 local package version은 `palimpsest-local 0.1.0.dev0`이다.
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
regression이 이 경계를 고정하며 package의 Python 3.12+ 지원 범위는 낮추지
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
