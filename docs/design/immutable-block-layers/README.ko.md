# 불변 블록 레이어 아키텍처 — 설계 제안과 참조 구현

OpenStack + Ceph RBD 데이터센터와 로컬 KVM 개발 환경에서 같은 불변 블록 레이어(OverlayBD)를 쓰는 개발·배포
흐름에 대한 설계 제안입니다. 2026-11-15~16에 작성했습니다.

**상태: 제안.** 승인된 아키텍처가 아니고, 제품 코드(클라이언트·Hub)와 운영 클라우드는 바꾸지 않았습니다.
참조 구현은 `examples/immutable-block-layers/`에 있으며 제품 CLI에 연결되어 있지 않고 CI에도 들어 있지
않습니다.

## 산출물

| # | 문서 | 코드 |
| --- | --- | --- |
| 1 | [통합 아키텍처와 데이터 흐름](01-architecture.ko.md) | — |
| 2 | [레이어 빌드와 배포 자동화](02-build-and-publish.ko.md) | `bin/blkstack`, `pipeline/build-ml-stack.sh`, `recipes/` |
| 3 | [인프라 프로비저닝과 마운트 설정](03-provisioning.ko.md) | `ceph/setup-pools.sh`, `compute/`, `openstack/cinder_blkstack_driver.py` |
| 4 | [개발자 워크플로우 전환 가이드](04-developer-workflow.ko.md) | `bin/blkstack` |

## 먼저 읽어야 할 것

요청하신 구성과 달라진 점:

1. **`ceph-immutable-object-cache`는 이 구조에 적용되지 않습니다.** 이 캐시는 librbd가 clone의 부모를 읽을 때
   동작하고, krbd로 매핑해 OverlayBD가 읽는 레이어에는 쓰이지 않습니다. 이 캐시가 필수라면 librbd 경로
   (대안 B: RBD clone 체인)가 맞습니다.
2. **`exclusive-lock`**: 요청대로 레이어 이미지에는 없습니다. 다만 다중 노드 읽기의 조건은 아니었습니다(레이어는
   스냅샷으로 읽기 전용 매핑). 쓰기 가능한 delta에는 반대로 필요합니다.
3. **프런트엔드는 TCMU입니다.** OverlayBD 최신 릴리스 패키지에 ublk 프런트엔드가 없습니다.
4. **증분 레이어는 부모 체인에 묶입니다.** base를 바꾸면 위 레이어를 전부 다시 만들어야 합니다.
5. **라이브 마이그레이션은 지원하지 않는 것으로 설계했습니다**(정지 후 이동).
6. **지연 로딩은 SHA-256 검증 없이 블록을 읽습니다.** 데이터센터 게시와 검증이 필요한 실행은 전체를 받아
   확인합니다.

실행하면서 드러난, 요청에 없던 필수 조건(없으면 데이터가 손상되거나 유실됩니다):

7. **4 KiB 미만 쓰기의 손상.** OverlayBD의 sparse 쓰기 계층에 512바이트 쓰기가 그대로 내려가면, 장치를 다시
   열었을 때 같은 4 KiB 블록의 나머지가 0으로 읽혔습니다. `dm-ebs`로 4 KiB 정렬을 강제해 해결했고 같은
   시험이 통과합니다.
8. **flush 미전달.** 기본 설정에서는 게스트가 `fsync`한 데이터가 호스트 메모리에만 있을 수 있었습니다. SCSI
   디스크를 write-back으로 표시해 flush가 OverlayBD의 `fdatasync`까지 내려가게 했고, 내려가는 것을 확인했습니다.
9. **30초 I/O 타임아웃**을 올렸습니다(기본값의 영향은 소스 판독, 설정값만 확인).

근거와 대안은 [아키텍처 문서](01-architecture.ko.md) 6절과 10절, 설정은 [산출물 3](03-provisioning.ko.md) 2절에
있습니다.

## 무엇을 실행했고 무엇을 실행하지 않았는가

실행 환경: Apple M5 Max(macOS 27.2)의 Lima 2.2.1 VM 한 대(`vz`, 중첩 가상화, aarch64, Ubuntu 24.04,
커널 6.8.0-142), OverlayBD 1.0.18(`.deb`), QEMU 8.2.2, libvirt 10.0.0, Ceph 19.2.3(MicroCeph 단일 노드, loop
OSD 3개), `distribution` 3.1.2, `oras` 1.3.6. 이 VM이 빌더·개발자 머신·컴퓨트 노드 역할을 모두 했습니다.
실행 후 VM은 삭제했습니다. 아래 "실행함"은 최종 코드로 다시 실행한 결과입니다(예외는 표에 적었습니다).

| 항목 | 상태 | 확인 방법 |
| --- | --- | --- |
| base 레이어 빌드(jammy arm64 디스크 전체 → 블록 레이어, 크기 확장, inode 테이블 초기화) | 실행함 | `poc/run-local-poc.sh` 1단계 |
| 증분 레이어 빌드(chroot 레시피, trim, 저널 처리) | 실행함 | 1단계 |
| trim·저널 처리 유무에 따른 레이어 크기 비교 | 실행함 | `poc/measure-layer-hygiene.sh` |
| 레지스트리 게시, 이미 있는 레이어 건너뛰기 | 실행함 | 2·3단계 |
| RBD 게시(임시 이름 → 봉인 → 재검증 → 최종 이름), 이미 있는 이미지 건너뛰기 | 실행함 | 2·5단계 |
| 지연 pull(블롭 미저장) → 조립 → QEMU/KVM 부팅 → SSH | 실행함 | 3단계 |
| VM 안에서 변경·정리 → 종료 → delta만 commit → 새 블롭만 push | 실행함 | 3단계 |
| 4 KiB 미만 쓰기가 다시 연 뒤에도 온전함(`dm-ebs`) | 실행함 | 4단계. `dm-ebs` 없이는 손상됨을 별도 수동 실행으로 확인 |
| 검증 pull(`--blobs`, 크기와 SHA-256 확인) | 실행함 | 5단계 |
| 컴퓨트 노드 Ceph 자격으로 레이어 풀 변경 시도 5가지(스냅샷 생성, 메타데이터, 보호 해제, 삭제, 이미지 생성) 모두 거부 | 실행함 | 5단계 |
| 로컬 블롭 파일 / RBD 스냅샷 / 레지스트리(캐시 비운 지연 읽기)로 조립한 가상 디스크 20 GiB의 SHA-256 일치 | 실행함 | 6단계 |
| RBD 레이어 + RBD delta로 조립·부팅, 개발자 변경이 보임 | 실행함 | 7단계 |
| delta가 해제·재조립 뒤에도 유지, 다른 스택 이름으로 붙이면 거부 | 실행함 | 8단계 |
| 조립 도중 실패하면 매핑·세션이 남지 않음 | 실행함 | 9단계 |
| 조립 도중 SIGTERM을 받으면 되돌림(매핑 3~4개가 있던 시점) | 실행함 | 별도 수동 실행 |
| 첫 포맷이 기록 전에 중단된 delta를 이어서 조립, 이 도구가 만들지 않은 내용이 있는 delta는 거부 | 실행함 | 별도 수동 실행 |
| 한 노드에서 두 인스턴스 디스크를 동시에 조립했을 때 레이어 매핑 공유, 하나를 해제해도 다른 쪽 유지 | 실행함(조립과 읽기까지. 두 VM의 동시 부팅은 하지 않음) | 10단계 |
| flush가 호스트 `sync` 없이 delta 이미지에 도달, 타임아웃 설정값 | 실행함 | 11단계. 설정 전에는 도달하지 않음을 별도 수동 실행으로 확인 |
| libvirt `prepare`/`release` 훅 + 노드 에이전트로 장치 생성·해제, 없는 볼륨은 시작 거부하고 흔적 없음 | 실행함 | `poc/run-libvirt-hook-poc.sh`(Nova 없이 `virsh`, delta 이미지는 스크립트가 생성) |
| udev 규칙 예시(`69-blkstack-ignore.rules`)가 원시 장치와 매퍼 장치에 `UDISKS_IGNORE`, `SYSTEMD_READY=0`을 붙임 | 실행함(속성 확인까지. LVM 제외 설정은 미실행) | 별도 수동 실행 |
| `overlaybd-tcmu`를 `kill -9` → 자동 재시작 후 장치 재개, flush한 데이터 유지 | 실행함(한 번, I/O가 없는 상태, 수정 전 코드) | 별도 수동 실행 |
| 파이프라인으로 base → CUDA 툴킷 → cuDNN 빌드와 레지스트리 게시(arm64 `sbsa` 저장소, 40 GiB) | 실행함(수정 전 코드, RBD 게시 생략, 이미지 서명 미확인) | `pipeline/build-ml-stack.sh`, `STAGES="cuda cudnn"` |
| 그 CUDA 스택을 지연 pull로 부팅, `nvcc --version`과 cuDNN 헤더 확인 | 실행함(수정 전 코드. 사용자 공간만, GPU·드라이버 없음) | `nvcc` 12.6, `nvidia-smi` 없음 |
| amd64 빌드와 BIOS 부팅, `pytorch.sh`, 이미지 서명(`gpgv`) 검사 경로, 인증 있는 레지스트리 | **실행 안 함** | 정적 검사만 |
| Cinder 드라이버 스케치 | **실행 안 함** | 문법·린트만. Cinder에 올려 보지 않음 |
| Nova를 통한 볼륨 부팅, Kolla 컨테이너 안의 훅 | **실행 안 함** | 소스 판독에 근거한 설계 |
| 여러 호스트(동시 RO 매핑, `--exclusive` 충돌, 라이브 마이그레이션 거부, 노드 장애 후 재조립) | **실행 안 함** | 추론 |
| 호스트 전원 장애, 부하 중 데몬 중단, 레지스트리·Ceph 정지 | **실행 안 함** | 미검증 |
| GPU, CUDA 동작 | **실행 안 함** | 장치 없음 |
| 성능(처리량, 지연, `dm-ebs`·flush 비용, ublk 대 TCMU, NVMe 캐시) | **실행 안 함** | 아래 수치는 한 환경의 관찰값 |
| WSL2에서의 개발자 흐름 | **실행 안 함** | macOS는 위 Lima 구성으로 실행 |
| 동시 게시, 동시 조립 경쟁 | **실행 안 함** | 잠금과 rename으로 설계 |
| 운영 클라우드 | **변경 없음** | — |

정적 검사: `shellcheck -s bash -S warning` 통과 — `bin/blkstack`, `poc/*.sh`, `pipeline/*.sh`,
`ceph/setup-pools.sh`, `compute/blkstack-agent-handler`, `recipes/*.sh`; `bash -n` 통과; Python 파일
(`compute/libvirt-hook-qemu`, `openstack/cinder_blkstack_driver.py`)은 ruff(E,F,W)와 포맷 검사 통과.

독립 검토: 코드와 문서를 각각 별도 검토자가 읽었고(Astra/Sol 서명 아님), 그 지적으로 다음을 고쳤습니다 —
세션 삭제가 마운트를 따라 내려갈 수 있던 문제, 탐지 실패 시 delta를 다시 포맷할 수 있던 문제, 노드 전체
잠금과 실패 시 되돌리기, 게시 경쟁, 스택 설명 검증, 문서의 과장·오기. 코드는 고친 뒤 한 번 더 검토받아
남은 지적(중단된 첫 포맷, 종료 처리기의 오류 처리, 임시 이미지 정리, 바쁜 마운트의 지연 해제)을 반영했고,
그 뒤 위 실행을 다시 했습니다. 문서는 고친 뒤 다시 검토받지 않았습니다.

## 관찰값 (한 환경, 벤치마크 아님)

MB는 10^6바이트입니다. 시간에는 한 번의 실행값만 있습니다.

| 항목 | 값 |
| --- | --- |
| base 레이어 빌드(20 GiB 가상 디스크, inode 테이블 초기화 포함) | 15.7초, 블롭 949 MB |
| 작은 증분 레이어 빌드(`jq`) | 15.2초, 블롭 4.5 MB |
| 레지스트리 push(로컬) / RBD 게시(재검증 포함) | 0.9초 / 3.1초 |
| 지연 pull | 0.03초(블롭 0개 저장) |
| 지연 부팅 후 SSH가 응답하기까지(첫 부팅, cloud-init 완료 전일 수 있음, 로컬 레지스트리) | 35.3초 (직전 수정 단계의 코드로 한 두 번의 실행에서는 26.9초, 31.7초) |
| cloud-init 완료 시점에 레지스트리에서 내려와 있던 양 | 레이어 953 MB 가운데 232 MB(24%) |
| 디스크 전체(20 GiB)를 지연 읽기로 읽은 뒤 캐시 | 1,046 MB |
| 개발 세션 delta(첫 부팅 + 패키지 하나, 게스트에서 정리·`sync`·`fstrim` 후) | 92.9 MB (호스트 `blkstack trim` 전 94.9 MB) |
| delta commit / push | 0.3초 / 0.1초(새 블롭만) |
| 컴퓨트 노드: 조립(`node-attach`, delta 이미지 생성·포맷 포함) | 0.9초 |
| 컴퓨트 노드: RBD에서 부팅해 SSH 응답까지(첫 부팅. 직전에 같은 레이어를 전부 읽어 페이지 캐시가 따뜻한 상태) | 65.4초 (직전 수정 단계의 두 번의 실행에서는 33.1초, 26.9초. 차이의 원인은 분석하지 않음) |
| 인스턴스 delta, 게스트가 64 MiB 파일을 `fsync`한 직후(호스트 `sync` 없이) | `rbd du --exact` 138.6 MB |
| 같은 delta, 종료 후 | `--exact` 141.4 MB, 기본 `rbd du` 281.0 MB |
| 부팅하지 않은 delta 이미지 | `--exact` 5.1 MB, 기본 `rbd du` 88 MiB |
| flush 시험: 64 MiB 쓰고 `fsync`한 직후 delta 이미지 증가량 | 67.1 MB (설정 전에는 0) |
| 레이어 풀 저장량(레이어 3개) | 998 MiB(복제 전) |
| trim·저널 처리에 따른 레이어 크기(`jq` 레시피) | 그대로 240.0 MB / `fstrim` 8.3 MB / `fstrim`+저널 처리 4.5 MB |
| CUDA 스택 빌드·게시 전체(base + CUDA 툴킷 + cuDNN, 내려받기 포함, 40 GiB, 수정 전 코드) | 914초(실행 명령의 경과 시간. 로그 파일에는 없음) |
| CUDA 툴킷 레이어 / cuDNN 레이어 블롭(수정 전 코드) | 4,951 MB / 2,002 MB |
| CUDA 스택(7,896 MB) 지연 부팅 후 SSH 응답까지(수정 전 코드) | 80초, 그때까지 내려온 양 240 MB(3%) |

증거 파일(로그, `report.json`)은 저장소 밖
`~/.local/share/palimpsest-proofs/spec-goal-20261103/blk-layers-poc-20261116/`에 있습니다(`final/`이 최종 코드의
실행, `intermediate-revision/`과 `earlier-revision/`이 그 이전 코드의 실행). 수동으로 한 시험(정렬 없이 손상 재현, flush 설정 전후
비교, 캐시 표시의 유지, 데몬 중단, 압축 비교, 레이어 크기 비교, SIGTERM 되돌리기, 중단된 첫 포맷, udev 규칙,
CUDA 스택 부팅)의 출력은 파일로 남기지 않았고 수치만 문서에 적었습니다. 중간 단계 실행 가운데 한 번의
로그도 남기지 않았습니다.

## 참조 구현 사용법

```sh
E=examples/immutable-block-layers
sudo $E/bin/blkstack help
sudo $E/poc/run-local-poc.sh /path/to/jammy-server-cloudimg-<arch>.img     # 일회용 호스트 전용
sudo $E/poc/run-libvirt-hook-poc.sh
```

`poc/` 스크립트는 파괴적입니다. 두 풀의 **모든 이미지를 지우고**, 호스트의 모든 blkstack 장치를 해제하며,
`/etc/overlaybd/overlaybd.json`을 고쳐 쓰고 `overlaybd-tcmu`를 재시작합니다. 다른 데이터가 있는 Ceph
클러스터나 호스트에서 실행하지 마십시오.

## 결정이 필요한 사항

[아키텍처 문서](01-architecture.ko.md) 12절: 데이터센터 방식(A 호스트 OverlayBD / B RBD clone 체인), 현행
파일 레이어와의 관계, 지연 로딩의 검증 수준, 격리된 시험 환경, delta 승격 절차, 상위 프로젝트 보고 여부.
운영 반영(컴퓨트 노드 데몬, Cinder 드라이버, Ceph 풀·권한)은 이 문서로 승인된 것이 아닙니다.

## 근거 자료

- OverlayBD v1.0.18 (`cee2186`): <https://github.com/containerd/overlaybd/tree/v1.0.18> — README(설정, TCMU 절차,
  쓰기 계층), `src/main.cpp`(`SYNCHRONIZE_CACHE` 처리, `tcmu_dev_set_write_cache_enabled(dev, false)`),
  `src/image_file.cpp`(상위 파일 열기), `src/tools/`(명령 옵션). ublk: 미출시 main의 `docs/standalone-usage.md`.
  타임아웃 값: <https://github.com/containerd/accelerated-container-image>의 스냅샷터.
- Linux: krbd <https://github.com/torvalds/linux/blob/v6.8/drivers/block/rbd.c>; TCMU
  <https://docs.kernel.org/target/tcmu-design.html>; dm-ebs
  <https://docs.kernel.org/admin-guide/device-mapper/dm-ebs.html>; ublk <https://docs.kernel.org/block/ublk.html>.
- Ceph: <https://docs.ceph.com/en/tentacle/rbd/rbd-persistent-read-only-cache/>,
  <https://github.com/ceph/ceph/blob/v20.2.0/src/librbd/plugin/ParentCache.cc>,
  <https://docs.ceph.com/en/tentacle/man/8/rbd/>,
  <https://github.com/ceph/ceph/blob/v20.2.0/src/tools/rbd/action/DiskUsage.cc>.
- Nova 2026.1 (`b6d2057`): `nova/virt/libvirt/driver.py`의 `VOLUME_DRIVERS`, `nova/virt/libvirt/volume/volume.py`
  — <https://opendev.org/openstack/nova/src/commit/b6d2057c0ea023c799a7da77958b1f12dd95fb85/nova/virt/libvirt/driver.py>.
- libvirt 10.0.0 훅: <https://gitlab.com/libvirt/libvirt/-/blob/v10.0.0/docs/hooks.rst>.
- OCI 배포 규격 v1.1.1: <https://github.com/opencontainers/distribution-spec/blob/v1.1.1/spec.md>.
- Ubuntu 클라우드 이미지: <https://cloud-images.ubuntu.com/jammy/current/>; NVIDIA 저장소:
  <https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/>.
