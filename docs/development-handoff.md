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
- HEAD: `cfa727133449c2b710babfc4f140a2990859054c`
- staged PCI snapshot의 architecture review marker source SHA-256:
  `4abcefaea57086a4856417bd3f8cce6becb755f0eee377b0b8384a6c302a473b`
  (381 files). 이후 문서 연결 작업으로 현재 working marker는
  `909f9e273842a5812466f9bf450446647f121bfb44ec9d93d9b850d3212806b9`
  (381 files)로 다시 stamp됐다. 두 값은 서로 다른 범위의 point-in-time
  증거이며 어느 하나로 다른 범위를 대체하지 않는다.
- 마지막 Linux 선별 검증 기준 commit: `aad3d492` — monitor-client timeout
  origin 계약의 client/exec 선별 105건이 로컬과 정확한 Linux checkout에서
  통과했다. 이는 ML native 성공이나 전체 regression 성공이 아니다.
- 현재 working tree에는 아래 PCI 변경 외에도 다른 작성자가 소유한 변경이
  섞여 있다. 특히 오래된 MySQL 관련 `ARCHITECTURE.md` hunk,
  `docs/docker-hub-service-matrix.md` 36줄, `docs/oci-linux-process.md` 8줄은
  아직 이 인계 작업의 검토 범위가 아니다. 복원, 정리, stage 또는 함께
  commit하지 않는다.
- 이 문서 작성 직전 문서-only 검증은 architecture guard 13 passed,
  working/staged architecture guard 및 lane `list --check` 통과였다. PCI
  source snapshot의 119 tests를 다시 실행한 결과는 아니다.

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

핀된 입력은 TensorFlow 2.21 CPU와 PyTorch 2.8 CUDA 12.6 runtime image다.
테스트는 original archive, Cmd-only override, network none, 8192 MiB/2 vCPU,
single-thread 2x2 matrix multiplication, GPU 미노출, 실제 root/PID1/lifecycle을
정의한다. 이것은 대화형 development image, CUDA, GPU 또는 외부 network
qualification이 아니다. [ML compatibility](oci-ml-compatibility.md)가
checkpoint 정본이다.

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
- PyTorch는 큰-image conversion 뒤 public run의 monitor handshake 경계에서
  실패했고 CPU tensor/root/PID1 proof에 도달하지 않았다. 장시간 준비 중
  동일 libvirt connection을 유지하는 startup event service와 coordinator의
  fixed-enum observability가 후속 구현·검토됐지만, 이 변경들로 PyTorch native
  성공이 새로 증명되지는 않았다. 실패 증거와 inactive resource를 임의로
  삭제하거나 성공으로 재분류하지 않는다.

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

## 현재 commit 전 PCI preflight 변경

다음 여섯 파일의 한 묶음이 현재 stage/index 또는 working tree에 있으며
**아직 commit/push되지 않았다**.

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

source/test/doc은 독립 검토 승인됐다. 아직 commit, push, Linux hardware query,
GPU attach는 없었다. 다음 세션은 먼저 현재 diff가 이 인계의 여섯 파일과
타 작성자 hunk를 그대로 보존하는지 재확인해야 한다.

## 명시적 승인 대기 — 실행 금지

다음 두 작업은 사용자 승인 전 단계에서 멈춰 있다. 이번 인계 문서 요청은
어느 쪽의 승인도 아니다. 우회 수단으로 같은 결과를 만들지 않는다.

1. GitHub remote `git@github.com:openstack-afterglow/palimpsest.git`로 publication
   (commit/push/package publication 포함).
2. private read-only helper
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
7. **ML 재검증:** 새 exact SHA와 보존 baseline에서 TensorFlow와 PyTorch를
   순차 실행한다. 새 timeout-source 오류 코드와 고정 coordinator code를
   별도 `ml-phase.json` phase/status/rc receipt와 함께 해석하고, framework
   matrix/root/PID1/cleanup이 실제 통과하기 전에는 ML 또는 GPU 성공으로
   표시하지 않는다.

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
