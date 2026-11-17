# 산출물 2 — 레이어 빌드와 배포 자동화

상태: 참조 구현입니다. `examples/immutable-block-layers/` 아래의 스크립트이며 제품 CLI가 아닙니다.
일회용 Linux VM(aarch64, Ubuntu 24.04, OverlayBD v1.0.18, 단일 노드 MicroCeph, 인증 없는 로컬 레지스트리)에서
실행했습니다. amd64와 사설 레지스트리로는 실행하지 않았습니다. 무엇을 실행했는지는
[README](README.ko.md)의 표가 기준입니다.

## 파일

| 파일 | 역할 |
| --- | --- |
| `bin/blkstack` | 빌드·세션·commit·레지스트리·RBD·노드 조립·로컬 부팅을 담당하는 CLI(bash) |
| `pipeline/build-ml-stack.sh` | base → CUDA → cuDNN → PyTorch를 차례로 만들고 단계마다 레지스트리와 RBD에 게시 |
| `recipes/cuda-toolkit.sh`, `cudnn.sh`, `pytorch.sh` | 레이어 하나의 내용(스택 안에서 chroot로 실행되는 셸 조각) |
| `recipes/poc-tools.sh` | 검증 실행에 쓴 작은 대역 레이어 |
| `ceph/setup-pools.sh` | 풀과 Ceph 자격 |
| `poc/measure-layer-hygiene.sh` | 아래 크기 비교 표를 만든 측정 스크립트 |

자동화 스크립트를 셸로 둔 이유: 이 단계는 외부 도구(`overlaybd-*`, `rbd`, `oras`, `qemu`, `dmsetup`)를 순서대로
부르는 일이 전부이고, 설계가 승인되기 전의 참조 코드입니다. 제품에 넣기로 하면 저장소의 새 구성요소
방침(Rust 우선)에 맞춰 다시 작성하고 CI에 넣어야 합니다. 현재는 CI에 들어 있지 않습니다.

## 한 번에 실행

```sh
sudo oras login registry.example.org                 # root로: blkstack이 root로 oras를 부릅니다
sudo REGISTRY_REPO=registry.example.org/ml/ubuntu2204 \
     LAYER_POOL=lib-layers BLKSTACK_RBD="rbd --id blkstack-publisher" \
     SIZE_GB=40 \
     examples/immutable-block-layers/pipeline/build-ml-stack.sh
```

기대하는 결과: 스택 `ubuntu2204:base`, `:cuda`, `:cudnn`, `:torch`가 각각 태그 `base`, `cuda`, `cudnn`, `torch`로
레지스트리에 올라가고, 레이어 블롭은 `lib-layers`에 이미지 1개씩 생깁니다. 이미 있는 단계와 이미 올라간
레이어는 다시 만들거나 올리지 않습니다. 이전 실행에서 남은 단계는 직전 단계의 레이어 위에 있을 때만
재사용하고, 아니면 중단합니다.

실제로 실행한 범위: aarch64에서 `STAGES="cuda cudnn"`, 로컬 레지스트리(`PLAIN_HTTP=1`), `LAYER_POOL` 없이
(RBD 게시는 검증 스크립트에서 따로 실행). `torch` 단계와 위 명령 그대로의 실행은 하지 않았습니다.

## 단계별 동작

### 1. base 레이어 (`blkstack build-base`)

```sh
sudo blkstack build-base --name ubuntu2204:base --image jammy-server-cloudimg-amd64.img --size-gb 40
```

1. `overlaybd-create -s data index 40` — 40 GiB 가상 크기의 sparse 쓰기 계층.
2. `overlaybd-apply --from_qcow2 --no-extract-rootfs IMAGE config.v1.json` — 클라우드 이미지 디스크 전체
   (GPT, ESP, 루트 파티션)를 쓰기 계층에 기록. 커널 장치 없이 사용자 공간에서 변환합니다.
3. 장치로 붙여 `parted`로 GPT를 디스크 끝까지 옮기고 루트 파티션을 늘린 뒤 `e2fsck`, `resize2fs`.
4. 루트 파일시스템을 한 번 쓰기 가능으로 마운트해 **inode 테이블 초기화를 끝냅니다.** ext4는 늘어난 블록
   그룹의 inode 테이블을 첫 쓰기 마운트 때 배경에서 0으로 채우는데, base에서 끝내 두지 않으면 그 위의 모든
   레이어 빌드와 모든 인스턴스의 첫 부팅이 같은 0을 자기 delta에 씁니다. 이 단계가 없을 때 같은 레시피의
   레이어가 7.3~13.0 MB로 실행마다 달랐고, 넣은 뒤에는 4.5 MB였습니다.
5. `overlaybd-commit -z --uuid U data index BLOB` — ZFile 블롭. SHA-256을 계산해 그 이름으로 보관합니다.

이미지 내려받기와 검증은 파이프라인 스크립트가 합니다. `SHA256SUMS`와 대조하고, 그 파일의 서명을 배포판
키링 패키지(`ubuntu-cloudimage-keyring`)의 키로 `gpgv` 확인합니다. 서명을 확인할 수 없으면 중단하며,
`ALLOW_UNVERIFIED_IMAGE=1`로만 넘어갈 수 있습니다. CUDA 스택을 만든 실행은 이 검사를 넣기 전의 스크립트로
했고 서명은 확인되지 않았습니다(체크섬 대조만).

### 2. 증분 레이어 (`blkstack build-layer`)

```sh
sudo blkstack build-layer --from ubuntu2204:base --name ubuntu2204:cuda \
     --recipe recipes/cuda-toolkit.sh --title "CUDA toolkit"
```

1. 부모 스택의 레이어를 하위로, 새 sparse 쓰기 계층(부모 UUID 기록)을 상위로 조립.
2. 루트 파티션의 저널을 떼고 마운트, `/dev` `/proc` `/sys` `/run`을 넣고 레시피를 chroot에서 실행
   (`policy-rc.d`로 서비스 기동 차단). 레시피가 남긴 프로세스는 끝낸 뒤 정리합니다.
3. `sync` → `fstrim` → 마운트 해제 → 같은 크기의 빈 저널 복원 → 장치 해제.
4. commit → 부모 스택 + 새 레이어로 새 스택을 저장.

3번이 레이어 크기를 좌우합니다. `poc/measure-layer-hygiene.sh`로 같은 base 위에서 같은 레시피를 세 번
적용해 잰 값입니다(MB는 10^6바이트, 압축된 블롭 크기):

| 레시피 | 그대로 commit | `sync` 후 `fstrim` | `fstrim` + 저널 처리(`build-layer`) |
| --- | --- | --- | --- |
| `jq` 설치(패키지 목록 받고 지움) | 240.0 MB | 8.3 MB | 4.5 MB |
| 아무것도 하지 않음 | 9.8 kB | 9.8 kB | 0.6 MB |
| 10 MiB 파일 추가 | 10.6 MB | 10.6 MB | 11.2 MB |

`fstrim` 앞의 `sync`가 필요합니다. ext4는 아직 커밋되지 않은 트랜잭션에서 해제된 블록을 discard하지 않으므로,
파일을 지운 직후의 `fstrim`은 그 블록을 놓칩니다(`sync` 없이 잰 값은 277.8 MB로 줄지 않았습니다).

저널을 다시 만드는 비용(약 0.6 MB)이 바닥값으로 생기지만, 메타데이터 변경이 많은 레이어는 훨씬 작아집니다.
discard된 블록은 다시 열면 하위 레이어의 내용으로 읽히므로, 레이어에는 "실제로 바뀐 블록"만 남습니다.

### 레시피 작성 규칙

- 레시피는 스택 안에서 root로 실행됩니다. 빌드 호스트는 일회용으로 쓰십시오.
- 비밀값을 쓰지 마십시오. 레이어에 남습니다.
- 커널 드라이버는 넣지 않습니다. `recipes/cuda-toolkit.sh`는 `cuda-toolkit-<버전>`(드라이버 제외)을 설치합니다.
  드라이버는 실행 환경의 커널·장치에 맞춰 따로 제공해야 하며, 레이어가 조립된다고 CUDA 동작이 보장되지는
  않습니다.
- NVIDIA 저장소는 아키텍처에 따라 `x86_64` 또는 `sbsa`(arm64 서버)를 씁니다.
- PyTorch의 CUDA용 휠은 CUDA 런타임을 `nvidia-*` 의존 휠로 함께 설치합니다(PyTorch 관리자 설명 기준,
  `pytorch.sh`는 실행하지 않음). 그러면 CUDA 툴킷·cuDNN 레이어와 내용이 겹칠 수 있으므로, 툴킷 레이어를 둘지는
  컴파일이 필요한지에 따라 정하십시오.
- `cudnn.sh`와 `pytorch.sh`는 버전을 고정하지 않습니다(`cudnn9-cuda-12`, `torch`). 빌드 시점의 최신이
  들어가므로 CUDA 툴킷 버전과 어긋날 수 있습니다.
- 끝에서 `apt-get clean`과 목록 삭제를 하십시오. 그 뒤의 `fstrim`이 블록을 돌려줍니다.

### 3. OCI 레지스트리 게시 (`blkstack push`)

```sh
sudo blkstack push ubuntu2204:cuda registry.example.org/ml/ubuntu2204:cuda
```

- 레이어마다 레지스트리에 이미 있는지 확인하고 없는 것만 올립니다(`oras blob push`).
- 스택 설명(JSON)을 config 블롭으로, OCI 이미지 매니페스트를 다음 형태로 올립니다(값은 예시).

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "artifactType": "application/vnd.palimpsest.blockstack.v1",
  "config": {"mediaType": "application/vnd.palimpsest.blockstack.config.v1+json", "digest": "sha256:…", "size": 625},
  "layers": [
    {"mediaType": "application/vnd.palimpsest.blocklayer.v1.overlaybd+zfile", "digest": "sha256:…", "size": 948719109,
     "annotations": {"org.opencontainers.image.title": "base:jammy-server-cloudimg-arm64.img"}}
  ]
}
```

- 이것은 컨테이너 이미지가 아닙니다. containerd의 OverlayBD 스냅샷터가 쓰는 형식(tar로 감싼
  `overlaybd.commit`, 전용 주석)과 다르며 `docker pull`로 실행할 수 없습니다. OverlayBD 백엔드 자체는
  매니페스트를 읽지 않고 "블롭 URL + digest + 크기"만 필요로 하므로, 매니페스트는 이 도구가 해석합니다.
- 받는 쪽(`pull`)은 매니페스트와 스택 설명의 digest·크기·UUID 형식, 레이어 수 상한, 부모 UUID 사슬을
  검사한 뒤에만 그 값을 경로나 인자로 씁니다.
- 지연 로딩은 레지스트리가 블롭 GET의 `Range`를 지원해야 동작합니다. 배포 규격 v1.1.1은 이를 SHOULD로
  둡니다. OverlayBD는 200 응답도 받아들이므로 [소스], `Range`를 무시하는 레지스트리나 프록시는 느려지거나
  CRC 오류로 드러납니다. 사용할 레지스트리에서 확인해야 합니다. 로컬 실행은 `distribution` 3.1.2로 했습니다.
- 자격 증명은 `oras login`이 관리하고 `blkstack` 인자로 넘기지 않습니다. 지연 로딩 때 OverlayBD가 쓰는
  자격은 `/etc/overlaybd/overlaybd.json`의 `credentialConfig`가 가리키는 파일입니다(0600, 저장소 밖).
  인증이 있는 레지스트리로는 실행하지 않았습니다.

### 4. Ceph RBD 게시 (`blkstack rbd-publish`)

```sh
sudo BLKSTACK_RBD="rbd --id blkstack-publisher" blkstack rbd-publish ubuntu2204:cuda --pool lib-layers
```

레이어마다: 최종 이름에 `@sealed`가 있으면 건너뜀 → 임시 이름으로 `rbd import`(기능은 `layering`만) →
메타데이터 기록 → `snap create @sealed` → `snap protect` → **그 스냅샷을 클러스터에서 다시 읽어 SHA-256 비교**
→ 최종 이름 `blk-<sha256>`으로 `rbd rename`. 검증에 실패하면 임시 이미지를 지웁니다. rename은 이름이 이미
있으면 실패하므로 두 게시 작업이 같은 레이어를 동시에 올려도 한쪽만 남습니다(동시 실행은 시험하지 않음).

레지스트리와 RBD "동시 배포"는 같은 블롭을 두 곳에 올리는 것입니다. 원자적이지는 않습니다. 순서는
레지스트리 먼저, RBD 다음이고, 데이터센터 실행은 RBD에 `@sealed`가 있는 레이어만 조립할 수 있으므로
중간 상태에서 잘못된 내용이 실행되지는 않습니다.

## 개발자 delta의 배포

로컬 세션의 변경은 같은 형식의 레이어 한 개가 됩니다([산출물 4](04-developer-workflow.ko.md)).

```sh
sudo blkstack commit mywork --name ubuntu2204:torch-mywork     # 쓰기 계층만 봉인
sudo blkstack push ubuntu2204:torch-mywork REGISTRY/REPO:torch-mywork   # 새 블롭 1개만 전송
```

검증 실행에서 두 번째 push는 기존 레이어 2개를 "이미 있음"으로 건너뛰고 새 레이어만 올렸고, 데이터센터
측 `rbd-publish`도 기존 이미지 2개를 건너뛰고 새 이미지 1개만 기록했습니다.

이 블롭을 `lib-layers`에 올리면 컴퓨트 호스트의 root 데몬이 그것을 해석합니다. 따라서 이 경로는 **플랫폼이
신뢰하는 게시자**에게만 열어야 하고, 임의 사용자의 delta를 공용 레이어로 승격하는 절차는 따로 정해야
합니다([아키텍처](01-architecture.ko.md) 7절).

## 알려진 한계

- 빌드는 재현 가능하지 않습니다. 같은 레시피를 다시 실행해도 블롭 digest가 달라집니다(패키지 저장소
  상태, 타임스탬프, 파일시스템 배치, 임의 UUID). digest는 "그 빌드 결과"의 식별자입니다.
- 레이어 합치기(`overlaybd-merge`), 크기 변경(`overlaybd-resize`), 수거(쓰지 않는 레이어, 남은 `tmp-<uuid>`
  이미지)는 다루지 않았습니다.
- 매니페스트 서명이 없습니다.
- amd64 빌드(BIOS 부트 파티션 포함), `pytorch.sh`, 인증 있는 레지스트리, 파이프라인의 RBD 게시 경로는
  실행하지 않았습니다.
