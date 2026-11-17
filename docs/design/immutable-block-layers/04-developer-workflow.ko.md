# 산출물 4 — 개발자 워크플로우 전환 가이드

상태: 설계 제안과 참조 구현(`examples/immutable-block-layers/bin/blkstack`)입니다. 제품 CLI(`palimpsest`)에는
들어가 있지 않습니다. 로컬 단계(1~6)와 클라우드 측 저장·조립 단계(7의 스크립트 부분)는 **같은 명령을 작은
대역 스택과 인증 없는 로컬 레지스트리로** 일회용 Linux VM 한 대에서 실행해 확인했습니다
(`poc/run-local-poc.sh`). 아래에 적힌 그대로(`ubuntu2204:torch`, 사설 레지스트리, `pip`)는 실행하지 않았고,
Nova/Cinder를 거치는 단계도 실행하지 않았습니다. 무엇을 실행했는지는 [README](README.ko.md)의 검증 표를
기준으로 삼아 주십시오.

## 지금의 흐름과 바뀌는 점

| | 지금(Palimpsest 0.3.x) | 블록 레이어 스택 |
| --- | --- | --- |
| 레이어 단위 | 파일 변경 집합(SquashFS), 게스트가 overlayfs로 합침 | 블록 변경 집합(OverlayBD), 호스트가 디스크 한 개로 합침 |
| 로컬 실행 | 레이어를 내려받아 저장한 뒤 VM 시작 | 매니페스트만 받고 필요한 블록만 레지스트리에서 읽으며 시작 |
| 환경 수정 후 공유 | `commit`으로 파일 레이어 생성, Hub에 올림 | 세션의 쓰기 계층만 `commit`, 새 블록 한 개만 레지스트리에 올림 |
| 클라우드 실행 | 게스트가 Hub에서 레이어를 받아 부팅 볼륨에 저장 | 컴퓨트 노드가 Ceph의 공유 레이어와 사용자 delta를 조립해 루트 디스크로 연결 |
| 부모 변경 | 파일 레이어도 부모 digest에 묶임 | 블록 레이어는 더 강하게 묶임: 부모가 바뀌면 그 위 레이어를 전부 다시 만들어야 함 |

마지막 줄이 가장 중요합니다. 블록 레이어는 "부모 디스크의 어느 블록이 바뀌었는가"이므로 다른 부모 위에
얹을 수 없습니다. CUDA 레이어는 그것을 만든 바로 그 Ubuntu 22.04 base 위에서만 의미가 있습니다.

## 준비물

| 환경 | 필요한 것 | 비고 |
| --- | --- | --- |
| Linux (KVM) | 커널 모듈 `target_core_user`, `tcm_loop`, `dm-ebs`; `overlaybd` 패키지; `qemu-system-*`, `cloud-image-utils`, `jq`, `oras`, `dmsetup`, `kpartx`, `parted`, `e2fsprogs` | Ubuntu 24.04(aarch64)에서 확인 |
| macOS | Lima VM(`vmType: vz`, `nestedVirtualization: true`) 안에서 위 Linux 구성 | Lima 문서 기준 Apple M3 이상·macOS 15 이상에서 중첩 가상화가 됩니다. 이 문서의 실행은 M5 Max에서 했습니다. `/dev/kvm`이 없으면 `blkstack boot`는 에뮬레이션으로 실행하며 느립니다 |
| Windows | WSL2(Windows 11) 안에서 위 Linux 구성 | WSL2 커널 설정에는 TCMU가 모듈로 켜져 있고 ublk는 꺼져 있습니다(설정 파일 기준). 모듈이 실제로 제공·적재되는지, `dm-ebs`와 `/dev/kvm`이 있는지는 확인하지 않았습니다. **실행하지 않은 조합입니다** |

OverlayBD는 Linux 호스트에서만 동작합니다. macOS와 Windows에서는 "Linux VM 안에서 OverlayBD와 QEMU를
실행"하는 중첩 구조가 됩니다.

```sh
sudo blkstack init                      # 모듈 로드, overlaybd-tcmu 서비스 시작, 작업 디렉터리 준비
sudo oras login registry.example.org    # blkstack이 root로 oras를 부르므로 root로 로그인합니다
```

자격 증명은 `oras`가 보관하고 `blkstack` 인자로 넘기지 않습니다. 사설 레지스트리에서 **지연 로딩**을 하려면
그와 별도로 OverlayBD가 읽는 자격 파일(`/etc/overlaybd/overlaybd.json`의 `credentialConfig`, 권한 0600)이
필요합니다. `blkstack`은 이 파일을 만들지 않으며, 이 구성은 실행하지 않았습니다(시험은 인증 없는 로컬
레지스트리).

## 단계별 명령

### 1. 스택을 지연 로딩으로 가져오기

```sh
sudo blkstack pull registry.example.org/ml/ubuntu2204:torch --name ubuntu2204:torch
```

매니페스트와 스택 설명만 받습니다. 레이어 블롭은 받지 않습니다(`--blobs`를 주면 전부 받아 SHA-256을
확인합니다).

### 2. 쓰기 계층을 붙여 세션 만들기

```sh
sudo blkstack session-create ubuntu2204:torch mywork
sudo blkstack attach mywork            # /dev/mapper/blk-<hash> 가 출력됩니다
```

`mywork`는 로컬 디스크의 sparse 파일(`local-delta`)입니다. 스택의 레이어는 읽기 전용이고, 쓰기는 모두
이 파일로 갑니다.

### 3. 부팅

```sh
cat > user-data <<'EOF'
#cloud-config
ssh_authorized_keys: ["ssh-ed25519 AAAA... me@laptop"]
EOF
sudo blkstack boot mywork --user-data user-data --ssh-port 2222 --memory-mb 8192 --cpus 4
ssh -p 2222 ubuntu@127.0.0.1
```

부팅에 필요한 블록만 레지스트리에서 읽습니다. 읽은 블록은 호스트의 OverlayBD 캐시에 남습니다.

### 4. 환경 수정

VM 안에서 평소처럼 작업합니다.

```sh
sudo apt-get update && sudo apt-get install -y libopenblas-dev
/opt/torch/bin/pip install einops
```

### 5. 정리하고 종료

```sh
sudo rm -rf /var/lib/apt/lists/* ~/.cache/pip
# 다른 사람이나 클라우드 인스턴스가 쓸 레이어라면: 이 VM 한 대에만 속하는 것을 지웁니다.
sudo cloud-init clean --logs --machine-id
sudo rm -f /etc/ssh/ssh_host_*
sync; sudo fstrim -av  # 지운 파일의 블록을 버립니다. sync가 먼저여야 방금 지운 블록까지 버려집니다
sudo poweroff          # 위 명령들과 한 번의 SSH 접속에서 실행하십시오(호스트 키를 지운 뒤에는 새 접속이 안 됩니다)
```

블록 레이어는 파일 삭제를 모릅니다. `fstrim`을 해야 삭제한 파일이 차지하던 블록이 delta에서 빠집니다.
부팅한 세션에는 첫 부팅이 만든 상태(SSH 호스트 키, machine-id, cloud-init 기록, 로그, 셸 기록)가 들어
있습니다. 위 정리는 그중 알려진 것만 지우며 전부를 보장하지 않습니다. 공유용 레이어는 가능하면
`build-layer` 레시피로 만드십시오(부팅 없이 chroot에서 적용).

### 6. delta만 레이어로 만들어 올리기

```sh
sudo blkstack detach mywork
sudo blkstack trim mywork        # 선택: 호스트에서 한 번 더 정리(fstrim, 저널 비우기). 자신의 세션에만 쓰십시오
sudo blkstack commit mywork --name ubuntu2204:torch-openblas --title "openblas, einops"
sudo blkstack push ubuntu2204:torch-openblas registry.example.org/ml/ubuntu2204:torch-openblas
```

`commit`은 세션의 쓰기 계층만 새 레이어 한 개로 봉인합니다. `push`는 레지스트리에 이미 있는 레이어를
건너뛰므로 새 레이어 한 개와 매니페스트만 올라갑니다. 전체 이미지를 다시 만들지 않습니다.

### 7. 클라우드에서 실행

운영 측 동기화 작업이 새 태그를 받아 Ceph에 반영합니다(새 레이어만 기록되고 digest를 다시 읽어 확인합니다).
digest 확인은 "개발자가 올린 그 블롭"이라는 것만 보증합니다. 컴퓨트 호스트의 root 데몬이 그 블롭을
해석하게 되므로, **이 경로는 플랫폼이 신뢰하는 게시자의 태그에만** 써야 합니다. 임의 사용자의 delta를 공용
레이어로 올리는 절차(플랫폼 측 재빌드나 격리된 변환)는 정해지지 않았습니다([아키텍처](01-architecture.ko.md) 7절).

```sh
# 플랫폼 측(레이어 게시 권한을 가진 동기화 작업)
sudo blkstack pull registry.example.org/ml/ubuntu2204:torch-openblas --name ubuntu2204:torch-openblas --blobs
sudo BLKSTACK_RBD="rbd --id blkstack-publisher" blkstack rbd-publish ubuntu2204:torch-openblas --pool lib-layers
```

사용자는 스택을 가리키는 이미지로 볼륨 부팅 인스턴스를 만듭니다(이 부분은 설계이며 실행하지 않았습니다).

```sh
openstack volume create --image ubuntu2204-torch-openblas --type blkstack --size 40 mywork-cloud   # 스택의 가상 크기와 같아야 함
openstack server create --volume mywork-cloud --flavor gpu.a --network private mywork-cloud
```

컴퓨트 노드에서는 libvirt 훅이 인스턴스 시작 직전에 다음과 같은 조립을 요청합니다.

```sh
sudo blkstack node-attach - --instance <volume-id> --delta-image volume-<volume-id> \
     --layer-pool lib-layers --delta-pool <Cinder 볼륨 풀>      # /dev/blkstack/<volume-id>
```

## 자주 하는 실수와 규칙

- **실행 중인 세션은 commit할 수 없습니다.** VM을 끄고 `detach`한 뒤 commit합니다.
- **delta는 만들어진 스택 위에서만 유효합니다.** 다른 스택에 붙이려 하면 거부됩니다. 부모를 새
  버전으로 바꾸고 싶으면 새 스택에서 변경을 다시 적용해 새 레이어를 만듭니다. 자동 이전은 없습니다.
- **스택 이름은 덮어쓸 수 없습니다.** 같은 이름으로 다시 만들면 거부됩니다. 새 이름이나 새 태그를 씁니다.
- **비밀값을 레이어에 넣지 마십시오.** 한번 commit한 레이어의 블록은 그 위에서 파일을 지워도 아래
  레이어에 남습니다. SSH 호스트 키, 토큰, `cloud-init` 인스턴스 상태가 들어간 세션을 공유용으로 commit하지
  마십시오. 공유할 변경은 `build-layer`의 레시피로 만드는 편이 안전합니다(부팅하지 않고 chroot에서 적용).
- **GPU 드라이버는 레이어에 넣지 않습니다.** CUDA 툴킷·cuDNN·PyTorch는 사용자 공간이라 레이어에 들어가지만,
  커널 드라이버는 그 커널과 장치에 맞아야 하므로 실행 환경(로컬 VM, 클라우드 flavor)이 제공해야 합니다.
  레이어가 붙는다고 CUDA가 동작한다는 뜻은 아닙니다. 실제 장치에서 확인해야 합니다.
- **지연 로딩은 "검증 전 읽기"입니다.** 1단계처럼 받으면 읽은 블록은 SHA-256으로 검증되지 않습니다(CRC만).
  무결성이 중요한 실행은 `pull --blobs`로 전부 받아 검증한 뒤 시작하십시오.
- **쓰기 가능한 스택의 `/dev/sdX`에 직접 쓰지 마십시오.** `attach`가 출력한 `/dev/mapper/blk-…`만 쓰십시오.
  원시 장치에 4 KiB보다 작은 쓰기를 하면 다시 열었을 때 데이터가 손상됩니다([산출물 3](03-provisioning.ko.md) 2절).
- **discard한 블록은 0이 아니라 아래 레이어의 내용으로 돌아갑니다**(장치를 다시 연 뒤). 파일시스템의 빈
  블록에는 문제가 없습니다.

## 명령 요약

| 목적 | 명령 |
| --- | --- |
| 가져오기(지연) / 가져오기(검증) | `blkstack pull REF --name STACK` / `... --blobs` |
| 세션 | `session-create STACK NAME`, `attach NAME`, `detach NAME`, `session-rm NAME` |
| 부팅 | `boot NAME [--user-data F] [--ssh-port N] [--memory-mb N] [--cpus N]` |
| 호스트에서 정리(게스트 루트를 마운트해 `fstrim`, `e2fsck`, 저널 비우기) | `trim NAME` — 자신의 디스크에만 사용 |
| delta를 레이어로 | `commit NAME --name NEWSTACK [--title T]` |
| 올리기 | `push STACK REF` |
| 확인 | `ls`, `inspect STACK` |
