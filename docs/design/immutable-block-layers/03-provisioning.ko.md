# 산출물 3 — 인프라 프로비저닝과 마운트 설정

상태: 참조 구성입니다. Ceph·OverlayBD·libvirt 훅 부분은 일회용 VM 한 대(단일 노드 MicroCeph)에서 실제로
실행해 확인했습니다. Nova와 Cinder는 실행하지 않았고, 운영 클라우드에는 아무것도 설치하거나 바꾸지
않았습니다. 운영 반영은 별도 승인이 필요한 변경(컴퓨트 노드에 호스트 데몬 추가, Cinder 드라이버 추가,
Ceph 풀·권한 추가)입니다. 파일은 모두 `examples/immutable-block-layers/` 아래에 있습니다. 근거 표기
([실행], [소스], [문서], [추론], [미검증])는 [아키텍처 문서](01-architecture.ko.md)와 같습니다.

## 1. Ceph: 풀과 권한

`ceph/setup-pools.sh [LAYER_POOL] [DELTA_POOL]` (기본값 `lib-layers`, `user-delta`. Cinder를 쓰면 delta 풀은
그 백엔드의 볼륨 풀입니다.)

| 풀 | 내용 | 쓸 수 있는 자격 | 읽는 주체 |
| --- | --- | --- | --- |
| `lib-layers` | 레이어 블롭 1개당 RBD 이미지 1개(`blk-<sha256>`), 보호된 스냅샷 `@sealed` | 게시 작업(`client.blkstack-publisher`) | 모든 컴퓨트 노드 |
| delta 풀 | 인스턴스 1개당 쓰기 가능한 RBD 이미지 1개 | 모든 노드 자격(`client.blkstack-node`)과 Cinder | 같음 |

```sh
ceph auth caps client.blkstack-publisher mon 'profile rbd' osd 'profile rbd pool=lib-layers'
ceph auth caps client.blkstack-node      mon 'profile rbd' osd 'profile rbd-read-only pool=lib-layers, profile rbd pool=user-delta'
```

- 노드 자격으로 레이어 풀에 대해 스냅샷 생성, 메타데이터 변경, 스냅샷 보호 해제, 이미지 삭제, 이미지 생성을
  시도했고 모두 거부되었습니다. [실행] `rbd-read-only` 프로필은 clone 자식 등록(헤더의 `child_attach` /
  `child_detach`)은 허용합니다. [소스] 이 설계는 clone을 쓰지 않습니다.
- delta 풀은 일반 Nova/Cinder 구성과 마찬가지로 **모든 노드 자격이 모든 이미지에 쓸 수 있습니다.** 한 노드가
  침해되면 다른 인스턴스의 delta를 바꿀 수 있습니다. 그래서 노드는 delta 안의 파일시스템을
  `nodev,nosuid,noexec,nosymfollow`로 마운트하고 `data`·`index`가 일반 파일인지 확인합니다.

### 요구사항과 달라진 점

- **`exclusive-lock`**: 요청대로 레이어 이미지에는 이 기능이 없습니다(`layering`만). 다만 그것이 다중 노드
  읽기 전용 매핑의 조건은 아닙니다. 레이어는 스냅샷(`image@sealed`)으로 매핑하고, 스냅샷 매핑은 항상 읽기
  전용이며 읽기 전용 krbd 매핑은 exclusive lock과 header watch를 잡지 않습니다. [소스: Linux 6.8
  `drivers/block/rbd.c`] 여러 호스트의 동시 매핑은 실행하지 않았습니다. [추론]
- **delta 이미지에는 `exclusive-lock`을 켭니다**(`object-map`, `fast-diff`도). 매핑은 `--exclusive`로 하며,
  노드는 이 기능이 없는 delta를 거부합니다. 두 번째 호스트의 매핑이 실패한다는 것은 옵션 설명에 근거한
  추론이고 여러 호스트로 실행하지 않았습니다. [추론]
- **`ceph-immutable-object-cache`는 krbd로 매핑한 레이어에는 적용되지 않습니다.** 이 캐시는 librbd가 clone의
  부모를 읽을 때 동작합니다. [문서·소스: `ParentCache.cc`] 이 구조에서 레이어 읽기의 로컬 캐시는 호스트
  페이지 캐시입니다. NVMe 캐시가 필요하면:
  (a) krbd 장치 위에 dm-cache나 OpenCAS — 실행하지 않음 [미검증];
  (b) 레이어를 레지스트리에서 읽고 OverlayBD 파일 캐시(`cacheConfig.cacheDir`)를 NVMe에 둠 — 검증 전 읽기라는
      한계가 따라옴;
  (c) 레이어 스냅샷마다 노드별 빈 clone을 만들어 librbd(`rbd-nbd`)로 매핑 — 이 경우 캐시가 적용될 수 있으나
      실행하지 않음 [추론];
  (d) [아키텍처 문서](01-architecture.ko.md)의 대안 B(RBD clone 체인 + librbd).

### 레이어 게시와 매핑

```sh
# 게시(레이어당 한 번). 최종 이름에 @sealed가 있으면 건너뜁니다.
rbd import --image-format 2 --image-feature layering BLOB lib-layers/tmp-<uuid>
rbd snap create lib-layers/tmp-<uuid>@sealed && rbd snap protect lib-layers/tmp-<uuid>@sealed
rbd export lib-layers/tmp-<uuid>@sealed - | sha256sum     # 노드가 읽게 될 스냅샷을 다시 읽어 digest 확인
rbd rename lib-layers/tmp-<uuid> lib-layers/blk-<sha256>  # 이름이 이미 있으면 실패 → 먼저 끝낸 쪽 것을 유지

# 컴퓨트 노드(노드당 한 번, 그 노드의 모든 인스턴스가 공유)
rbd --id blkstack-node device map --read-only lib-layers/blk-<sha256>@sealed      # /dev/rbdN
```

위 순서는 `blkstack rbd-publish`와 `blkstack node-attach`가 수행합니다. 임시 이름으로 올린 뒤 검증하고 이름을
바꾸므로, 노드는 검증된 스냅샷이 없는 `blk-<sha256>`를 볼 수 없습니다. 게시가 실패하거나 신호로 끝나면 임시
이미지를 지웁니다. 강제 종료(SIGKILL, 전원)된 경우에는 `tmp-<uuid>` 이미지가 남으며 이를 수거하는 작업은
없습니다.

레이어 블롭 형식에 대한 제약: 블롭 길이는 512의 배수가 아닐 수 있고 블록 장치는 섹터 단위로만 보이므로
끝이 잘립니다(krbd는 소스 판독, 로컬 실행은 loop 장치와 krbd 양쪽에서 열림 확인). `overlaybd-commit -z`로 만든
ZFile은 commit 때 헤더를 다시 써서 색인 위치를 헤더에서 읽으므로 그 상태로 열립니다. [소스·실행] 압축하지
않은 LSMT와 `-t`(tar) 형식은 대상이 아닙니다. `blkstack`은 항상 `-z`로 commit합니다.

## 2. 컴퓨트 노드

### 패키지와 커널

| 항목 | 값 | 비고 |
| --- | --- | --- |
| OverlayBD | 릴리스 v1.0.18 패키지(`overlaybd-tcmu`) | 이 릴리스 패키지에는 ublk 프런트엔드가 없습니다 [실행: 패키지 목록] |
| 커널 모듈 | `target_core_user`, `tcm_loop`, `rbd`, **`dm-ebs`** | Ubuntu 24.04 6.8 커널에서 확인 |
| 도구 | `ceph-common`(rbd), `jq`, `e2fsprogs`, `dmsetup`, `kpartx`, `parted`, `util-linux` | `kpartx`·`parted`는 빌더와 자기 세션용 |

### `/etc/overlaybd/overlaybd.json` (`compute/overlaybd.json`)

| 키 | 값 | 이유 |
| --- | --- | --- |
| `download.enable` | `false` | 데이터센터에서는 레이어를 RBD에서 읽으므로 레지스트리 전체 내려받기를 끕니다 |
| `cacheConfig.cacheDir`, `cacheSizeGB` | 경로, 용량 | 레지스트리 경로를 쓸 때만 의미가 있습니다. RBD(`file`) 하위 레이어에는 쓰이지 않습니다 |
| `ioEngine` | `0`(psync) | 로컬 실행에 쓴 값. `1`(libaio)은 읽기 전용 레이어를 `O_DIRECT`로 열어 페이지 캐시를 쓰지 않게 됩니다 [소스] — 캐시가 없어지므로 권하지 않습니다 |
| `credentialConfig` | `file`, `/etc/overlaybd/cred.json` | 레지스트리를 쓸 때만. 파일 권한 0600, 저장소에 넣지 않습니다 |
| `exporterConfig.enable` | `true` | 장치별 지표 수집 |
| `enableAudit` | `true` | I/O 감사 로그 |

### 인스턴스 디스크 조립 (`blkstack node-attach`)

```
lib-layers/blk-L0@sealed ─ krbd ro ─▶ /dev/rbd0 ┐
lib-layers/blk-L1@sealed ─ krbd ro ─▶ /dev/rbd1 ├─ lowers[].file
lib-layers/blk-L2@sealed ─ krbd ro ─▶ /dev/rbd2 ┘
user-delta/volume-<id> ─ krbd --exclusive ─▶ /dev/rbd3 ─ ext4 ─ {data, index}  = upper (sparse)
                                   │
   overlaybd-tcmu (config.v1.json) ─ TCMU ─ tcm_loop ─▶ /dev/sdX
                                   │   타임아웃 상향, write-back 캐시 표시(flush 전달)
                              dm-ebs (512B 섹터 → 4 KiB 블록 단위 쓰기)
                                   ▼
                    /dev/mapper/blk-<hash>  ◀─ /dev/blkstack/<id>  ◀─ QEMU
```

`config.v1.json`(생성되는 형태):

```json
{
  "lowers": [{"file": "/dev/rbd0"}, {"file": "/dev/rbd1"}, {"file": "/dev/rbd2"}],
  "upper": {"index": "/var/lib/blkstack/sessions/node-<id>/delta/index",
            "data":  "/var/lib/blkstack/sessions/node-<id>/delta/data"},
  "resultFile": "/var/lib/blkstack/sessions/node-<id>/result"
}
```

조립 순서와 그 이유:

1. 노드 전체 잠금을 잡습니다. 레이어 매핑은 인스턴스들이 공유하므로, 매핑·해제·"아직 쓰는 인스턴스가
   있는가" 판단이 한 잠금 아래에서 일어나야 합니다.
2. delta 이미지의 메타데이터 `blkstack.stack`에서 스택을 읽고 형식을 검사합니다(digest·UUID·크기·레이어 수
   상한·부모 UUID 사슬). 이 값은 다른 자격(Cinder)이 쓴 것이므로 그대로 믿지 않습니다. delta는 자신의 스택을
   스스로 기록하고 있으므로 다른 스택 위에 조립될 수 없습니다. CLI에서 스택 이름을 주고 붙일 때 기록과
   다르면 거부됩니다. [실행]
3. delta 이미지에 `exclusive-lock`이 있고 크기가 가상 디스크보다 큰지 확인합니다.
4. 각 레이어 이미지의 `blkstack.digest`가 요청한 digest와 같은지 확인하고 `@sealed`를 읽기 전용으로
   매핑합니다(이미 매핑되어 있으면 재사용).
5. delta를 `--exclusive`로 매핑합니다. 처음이면 파일시스템 UUID를 정해 이미지에 기록(`blkstack.fs_uuid`)한 뒤
   그 UUID로 ext4를 만들고, 끝나면 `blkstack.formatted=1`을 기록합니다. 이후에는 이 표시가 있으면 절대 다시
   포맷하지 않습니다(파일시스템 탐지가 실패했다고 사용자 데이터를 지우지 않기 위함). 포맷과 기록 사이에서
   중단된 경우는 UUID가 일치하면 이어서 진행하고 [실행], 이 도구가 만들지 않은 내용이 있으면 거부합니다. [실행]
   메타데이터는 매핑을 푼 상태에서 씁니다(exclusive 매핑 중에는 이미지 메타데이터를 바꿀 수 없었습니다 [실행]).
6. OverlayBD 장치를 만들고 아래 "쓰기 가능한 장치의 세 가지 설정"을 적용합니다.
7. 도중에 실패하거나 SIGTERM을 받으면 한 일을 역순으로 되돌립니다(장치, 마운트, 매핑, 세션 디렉터리). [실행]

- 쓰기 계층을 RBD 블록 장치에 직접 둘 수는 없습니다. OverlayBD의 쓰기 계층은 파일 두 개(`data`, `index`)이고
  sparse 방식은 파일의 hole로 매핑을 복원합니다. [소스] 그래서 delta 이미지 위에 ext4를 두고 그 안에 파일을
  둡니다.
- delta 이미지는 가상 디스크보다 크게 만듭니다(`가상 크기 + 가상 크기/16 + 1` GiB). 게스트가 디스크를
  전부 덮어써도 상위 파일이 들어갈 자리가 있어야 합니다. 그렇지 않으면 게스트의 파일시스템에는 여유가
  있는데 쓰기 오류가 납니다. 얇게 할당되므로 프로비저닝 크기는 실제 사용량과 무관합니다.

### 쓰기 가능한 장치의 세 가지 설정 (필수)

`overlaybd-tcmu`를 README의 절차대로만 붙이면 VM 디스크로 쓰기에 안전하지 않았습니다. `blkstack`은 아래를
적용하고, 적용할 수 없으면 장치를 내놓지 않습니다.

| 설정 | 기본 동작에서 관찰한 것 | 적용 | 확인 |
| --- | --- | --- | --- |
| **4 KiB 쓰기 정렬** (`dm-ebs`) | sparse 쓰기 계층은 기록 여부를 데이터 파일의 hole, 즉 4 KiB 단위로 기억합니다. 하위 레이어에 데이터가 있는 4 KiB 블록 안에 512바이트만 쓰면, 장치를 다시 열었을 때 그 블록의 나머지 3,584바이트가 0으로 읽혔고 그 상태가 commit한 레이어에도 들어갔습니다 | `dmsetup create … ebs /dev/sdX 0 1 8`: 소비자는 512바이트 섹터를 보되 OverlayBD에는 4 KiB 블록 전체만 쓰입니다(읽고-고쳐-쓰기) | 같은 시험이 통과 [실행] |
| **flush 전달** | 장치가 "휘발성 캐시 없음"으로 보여 커널이 flush를 보내지 않는데, 상위 파일 쓰기는 호스트 페이지 캐시에 머뭅니다. 64 MiB를 쓰고 `fsync`해도 delta 이미지에 반영되지 않았습니다 | SCSI 디스크의 `cache_type`을 `temporary write back`으로 | `fsync` 직후 delta 이미지 사용량 증가 [실행]. 이 표시는 장치 rescan, udev change, 파티션 다시 읽기 뒤에도 유지되었습니다 [실행] |
| **I/O 타임아웃** | TCMU 명령과 SCSI 장치의 타임아웃이 30초입니다. 레지스트리나 Ceph가 그보다 오래 멈추면 게스트는 I/O 오류를 받습니다 [소스: 커널 문서] | `attrib/cmd_time_out`과 `device/timeout`을 2,147,483초로(containerd 스냅샷터가 쓰는 값) | 설정값 확인 [실행]. 실제 정지 상황은 시험하지 않음 [미검증] |

flush 표시는 커널 안의 값입니다. `cache_type`에 `temporary`가 없는 값을 쓰면(장치가 거부하더라도) 표시가
풀리고 다음 rescan에서 write-through로 돌아갔습니다. [실행] 조립 뒤에 이 값을 다시 확인하는 장치는 없으므로,
호스트에서 이 파일을 건드리는 도구가 없어야 합니다.

`dm-ebs`를 거치면 write-zeroes가 실제 0 쓰기로 바뀌고 `dm-bufio` 계층이 하나 늘어납니다. 성능 영향은
측정하지 않았습니다. 쓰기 가능한 스택의 원시 `/dev/sdX`에는 어떤 것도 직접 쓰면 안 됩니다.
QEMU 디스크는 `cache=none`이어야 게스트의 flush가 그대로 내려옵니다(Nova는 블록 장치에 기본으로 그렇게
설정하지만 `disk_cachemodes` 설정에 따라 달라집니다 [소스]).

discard의 의미: 쓰기 계층에서 discard된 블록은 장치를 다시 연 뒤에는 0이 아니라 **하위 레이어의 내용**으로
읽힙니다. [실행] 파일시스템의 빈 블록에는 문제가 없지만, discard 후 0을 기대하는 사용은 맞지 않습니다.

### 호스트가 게스트 디스크를 건드리지 않게 하기

조립된 장치는 게스트의 디스크인데 호스트에는 일반 블록 장치로 보입니다. 호스트의 udev·udisks·LVM
자동 활성화가 그 안의 파티션이나 볼륨 그룹을 건드리지 않게 해야 합니다(게스트의 볼륨 그룹 이름이 호스트와
같으면 충돌할 수 있습니다 [추론]). `compute/69-blkstack-ignore.rules`가 예시이며, LVM을 쓰는 호스트는
`lvm.conf`의 `global_filter`로 `/dev/mapper/blk-*`와 TCMU 디스크를 제외해야 합니다. 이 규칙은 시험 호스트에서
속성이 붙는 것까지만 확인했습니다.

### 노드 에이전트와 libvirt 훅

| 파일 | 설치 위치 | 역할 |
| --- | --- | --- |
| `bin/blkstack` | `/usr/local/bin/blkstack` | 에이전트가 호출 |
| `compute/blkstack-agent.socket`, `blkstack-agent@.service` | `/etc/systemd/system/` | root 전용 유닉스 소켓 `/run/blkstack/agent.sock`, 요청 1건당 프로세스 1개 |
| `compute/blkstack-agent-handler` | `/usr/local/libexec/` | `attach <volume-id>` / `detach <volume-id>`만 받음. ID는 UUID 형식 검사 후 사용 |
| `compute/node.env.example` | `/etc/blkstack/node.env` | 풀 이름과 Ceph 자격 이름(비밀값 없음) |
| `compute/libvirt-hook-qemu` | `/etc/libvirt/hooks/qemu` | `prepare begin`에서 조립 요청, `release end`에서 해제 요청 |
| `compute/69-blkstack-ignore.rules` | `/etc/udev/rules.d/` | 위 절 |

훅이 할 수 있는 것과 없는 것(libvirt 10.0.0 문서 기준):

- `prepare begin`은 libvirt가 자원에 레이블을 붙이기 전, QEMU를 시작하기 전에 호출됩니다. 여기서 실패하면
  게스트가 시작되지 않습니다. 훅은 이 시점에 장치를 만들 수 있습니다.
- 일반 시작에서는 훅이 도메인 XML을 바꿀 수 없습니다. 따라서 XML이 처음부터 `/dev/blkstack/<volume-id>`를
  가리켜야 하고, 그것은 아래 Cinder 연결 정보가 담당합니다.
- 훅 안에서 libvirt를 다시 호출하면 안 됩니다. 이 훅은 에이전트 소켓에만 접속합니다.
- libvirt는 데몬 시작 시 훅 스크립트를 찾습니다. 설치 후 `libvirtd`를 재시작해야 합니다.
- 훅은 인스턴스가 시작될 때만 동작합니다. 실행 중인 인스턴스에 스택 볼륨을 새로 붙이는 것(hot-attach)은
  지원하지 않습니다.

시간 제한은 훅(270초) > 에이전트 유닛(240초) > 에이전트의 잠금 대기 순으로 맞췄습니다. 유닛이 시간 초과로
중단되면 systemd가 `blkstack`에도 SIGTERM을 보내고 그 종료 처리기가 조립을 되돌립니다(SIGTERM 되돌리기는
CLI로 확인, 유닛 시간 초과 자체는 시험하지 않음). 해제 요청은 `/dev/blkstack` 링크 유무가 아니라
`blkstack`의 세션 상태를 기준으로 하므로, 반쯤 만들어진 조립도 해제 때 정리됩니다.

로컬 실행(`poc/run-libvirt-hook-poc.sh`)에서 Nova 없이 `virsh start`만으로 다음을 확인했습니다: 시작 전에는
장치가 없고, 시작하면 훅이 장치를 만들고, 게스트가 그 디스크로 부팅해 레이어 내용을 보고하고, 종료하면
장치와 RBD 매핑이 모두 사라지며, delta 이미지가 없는 볼륨은 시작이 거부되고 아무 상태도 남지 않습니다.
[실행] 이 실행에서 delta 이미지는 Cinder 드라이버가 아니라 스크립트가 직접 만들었습니다.

Kolla 환경에서 추가로 필요한 것(미실행): `nova_libvirt` 컨테이너는 `/run`을 이미 바인드하지만
`/etc/libvirt/hooks`는 보존하지 않으므로 훅 파일을 추가 볼륨으로 넣어야 합니다. [소스] 에이전트와 OverlayBD는
컨테이너가 아니라 호스트에서 실행합니다(configfs 접근이 필요하고, 훅은 소켓만 쓰면 됩니다).
`overlaybd-tcmu`를 재시작하거나 올리려면 그 노드의 스택 인스턴스를 먼저 비워야 합니다.

### 라이브 마이그레이션

지원하지 않는 것으로 설계했습니다. 대상 호스트에서도 `prepare`가 먼저 실행되어 같은 delta를 붙이려 하지만,
원본 호스트가 `--exclusive`로 잡고 있으므로 실패하고 마이그레이션이 거부될 것으로 봅니다. 두 OverlayBD
프로세스가 한 쓰기 계층을 여는 일을 막는 쪽을 택했습니다(대상 프로세스는 자신이 연 시점의 매핑만 알고 있어,
원본의 마지막 쓰기를 보지 못합니다). 이동은 정지 → 해제 → 다른 호스트에서 조립 → 시작 순서로 합니다. 이
거부 동작은 여러 호스트로 실행해 보지 않았습니다. [추론] 실패한 조립이 흔적을 남기지 않는 것은 한
호스트에서 확인했습니다. [실행]

## 3. Nova/Cinder 연동 (설계, 미실행)

Nova는 수정하지 않습니다. Nova 2026.1의 libvirt 드라이버는 Cinder 연결 유형 `local`을 이미 지원하고
(`VOLUME_DRIVERS['local']`), 연결 정보의 `device_path`를 그대로 `<disk type='block'>`의 소스로 씁니다.
장치를 만들지는 않습니다. [소스] 따라서:

1. **Cinder 백엔드**(`openstack/cinder_blkstack_driver.py`, RBD 드라이버의 하위 클래스 스케치):
   - 스택은 속성 `blkstack_stack`을 가진 Glance 이미지로 표현합니다. 이 속성이 가리키는 것을 컴퓨트 호스트의
     root 데몬이 조립하므로, **게시 권한이 있는 역할만 설정할 수 있는 보호 속성**이어야 합니다.
   - `clone_image`: 그 이미지로 볼륨을 만들면 빈 RBD 이미지(delta)를 만들고 스택을 메타데이터에 기록합니다.
     복사는 없습니다. **볼륨 크기는 스택의 가상 크기와 같아야 하고**(게스트가 보는 크기), RBD 이미지 자체는
     위의 여유분만큼 더 크게 만듭니다.
   - `initialize_connection`: `{"driver_volume_type": "local", "data": {"device_path": "/dev/blkstack/<volume-id>", "discard": true}}`.
   - 확장·스냅샷·복제·유형 변경·이미지 업로드는 스택 볼륨에 대해 거부합니다. 스택 이미지를 암호화 볼륨
     유형으로 쓰려는 요청도 거부합니다. 다른 백엔드는 이 코드를 실행하지 않으므로, 스택 이미지에
     `cinder_img_volume_type`을 지정해 이 볼륨 유형으로만 쓰이게 해야 합니다.
   - 백업·복원은 스케치가 막지 않습니다. 복원은 사용자가 고른 바이트를 delta 이미지에 쓰는데, 그 안의
     ext4를 컴퓨트 호스트가 마운트하므로 이 볼륨 유형에서는 꺼야 합니다.
2. **컴퓨트 노드**: 위 훅과 에이전트가 그 경로의 장치를 QEMU 시작 전에 만듭니다.

이 방식의 한계: `local` 유형은 Nova가 연결·해제를 하지 않는 경로이므로 수명주기 전체(조립 실패, 정리,
호스트 장애 후 재조립)를 훅·에이전트가 책임져야 합니다. delta 이미지의 RBD 기능(`exclusive-lock`)은 클러스터
기본값에 달려 있고 스케치는 그것을 설정하지 않습니다(노드가 없으면 거부). Cinder 드라이버는 문법 검사만
했고 어떤 Cinder에도 올려 보지 않았습니다. [미검증]

### 사용량 과금

Cinder는 볼륨의 프로비저닝 크기만 보고합니다. 실제 기록량은 Ceph에서 읽어야 합니다.

```sh
blkstack usage --image user-delta/volume-<id>
# {"provisioned_bytes":…, "used_bytes_exact":…, "used_bytes_object_granular":…}
```

- 측정 대상은 "delta RBD 이미지 안의 ext4가 차지한 RBD 구간"입니다. 단위는 아래에서 위로 RBD object 안의
  구간, ext4 블록(4 KiB), OverlayBD 쓰기(4 KiB 정렬)입니다. 게스트 파일시스템의 사용량도, OSD의 물리
  사용량(복제·압축 포함)도 아닙니다.
- `rbd du`의 기본값은 object 단위(기본 4 MiB)로 올림한 값이고 object map으로 빠르게 계산됩니다. `--exact`는
  object 안의 기록 구간을 세지만 모든 object를 훑으므로 느립니다. [소스] 과금 주기와 비용의 균형이 필요합니다.
  두 값의 차이는 [README](README.ko.md)의 측정 표에 있습니다.
- 비어 있는 delta도 0이 아닙니다(파일시스템 메타데이터와 저널).
- 게스트가 파일을 지워도 `discard`가 내려오지 않으면 delta는 줄지 않습니다. 게스트의 `fstrim`
  (Ubuntu는 주 1회 타이머)이 있어야 반영됩니다. delta 이미지의 ext4는 `discard` 옵션으로 마운트합니다. krbd는
  기본 `alloc_size`(64 KiB)보다 작은 discard를 버리므로 [소스] 작은 삭제는 RBD 사용량을 줄이지 못할 수
  있습니다.

## 4. 로컬 환경에서 원격 레이어로 부팅

```sh
sudo blkstack init
sudo blkstack pull REGISTRY/REPO:TAG --name STACK          # 매니페스트만
sudo blkstack session-create STACK NAME
sudo blkstack attach NAME                                   # /dev/mapper/blk-<hash>
sudo blkstack boot NAME --user-data user-data --ssh-port 2222
```

`boot`가 실행하는 QEMU(aarch64 예):

```sh
qemu-system-aarch64 -machine virt,gic-version=max -accel kvm -cpu host -m 2048 -smp 2 \
  -display none -serial file:SESSION/console.log -daemonize -pidfile SESSION/qemu.pid \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.fd \
  -drive if=pflash,format=raw,file=SESSION/efivars.fd \
  -drive if=none,id=root,format=raw,file=/dev/mapper/blk-<hash>,cache=none,aio=native,discard=unmap \
  -device virtio-blk-pci,drive=root,serial=blkstack-root \
  -drive if=virtio,format=raw,readonly=on,file=SESSION/seed.img \
  -netdev user,id=n0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=n0
```

x86_64에서는 `-machine q35`의 기본 펌웨어(SeaBIOS)로 부팅하도록 되어 있습니다(jammy amd64 이미지는 BIOS 부트
파티션과 ESP를 모두 가집니다 [문서]). 이 경로는 실행하지 않았습니다. `/dev/kvm`이 없으면 에뮬레이션으로
떨어지며 매우 느립니다.

지연 로딩 설정은 `/etc/overlaybd/overlaybd.json`의 `cacheConfig`(읽은 범위를 보관하는 디렉터리와 상한)와
`download`(기동 후 전체 블롭을 배경에서 내려받을지)입니다. 지연 읽기는 ZFile의 CRC만 거칩니다. 블롭 전체의
SHA-256은 배경 내려받기가 끝난 뒤에야 확인되고, `download.enable=false`이면 한 번도 확인되지 않습니다.
[소스] 캐시 항목은 레지스트리 구분 없이 블롭 digest 이름으로 저장되므로 [소스], 신뢰하지 않는 레지스트리에서
지연 읽기를 한 호스트의 캐시는 다른 레지스트리의 같은 digest 읽기에도 쓰입니다. 검증이 필요한 실행은
`pull --blobs`를 쓰십시오. 사설 레지스트리를 지연 읽기로 쓰려면 `credentialConfig`가 가리키는 자격 파일이
있어야 하며 `blkstack`은 그것을 만들지 않습니다.
