# Palimpsest 목표 명세

## 문서 상태와 작성 원칙

- 상태: 제품 목표와 우선순위의 핵심 인터뷰 완료. 에이전트 실행 계획·완료 기준을 포함하며 기술 설계와 실행 권한·자원 범위는 별도로 확정한다. 아래 원문은 보존된 초기 초안으로, 상단의 최신 결정과 충돌하면 최신 결정을 따른다. 이 문서는 구현 완료나 운영 검증을 의미하지 않는다.
- 목적: 전체 목표를 구체화하고, 이를 에이전트의 loop/goal 실행에 사용할 수 있는 범위·단계·완료 기준으로 변환한다.
- 답변으로 확정된 결정과 미결정 사항을 구분한다. 미답변 항목을 에이전트가 임의로 확정하지 않는다.
- 2026-11-03 사용자가 이 체크리스트를 G0부터 실행하도록 지시했다. 앞선 인터뷰가 명세 편집만 승인했던 이력은 보존한다. Goal 실행 지시가 새 VM·유료 자원·배포 대상·Ceph/IAM 권한을 자동 확대하지 않으며 실제 허용 범위는 G0에 별도로 기록한다.
- 현재 작업트리의 기존 변경은 보존한다. 기존 구현·운영 계약과 목표가 충돌하면 차이를 기록하고 목표 변경 여부를 결정한다.

## Goal 실행 체크리스트

### 사용 규칙

- 이 절을 진행 상태의 정본으로 사용한다. 아래 목표·인터뷰 기록·완료 판정 표는 요구사항의 근거이며 체크리스트가 기존 승인 경계를 대체하지 않는다.
- 모든 항목은 미완료로 시작한다. 문서 작성, 명령 존재, 과거 다른 소스의 성공만으로 체크하지 않는다. 기존 구현도 해당 항목의 근거를 현재 소스에 연결하고 필요한 검증을 수행한 뒤 체크한다.
- 고정 ID는 이름을 바꾸거나 재번호화하지 않는다. 추가 작업은 새 ID로 추가하고 요구사항을 삭제·축소해 완료율을 높이지 않는다.
- `[ ]`는 미완료·진행 중·차단을 포함한다. `[x]`는 해당 항목의 완료 조건과 증거가 충족된 경우에만 사용한다. 진행 중과 차단은 아래 실행 기록으로 구분한다.
- 실행 순서는 G0 → M1 → M2 → M3 → M4 → M5.1 → M5.2 → M5.3 → M5.4 → M5.5 → FINAL이다. 공통 종료 게이트 GATE는 각 단계마다 적용한다. 의존 항목이 차단되면 무관한 조사·설계는 진행할 수 있으나 후속 단계 완료를 선언하지 않는다.
- 구현 항목은 관련 동작 검증까지 포함한다. 설계 항목은 결정·근거·영향 범위의 기록으로 완료할 수 있으나 구현 완료를 뜻하지 않는다. 실제 실행이 필요한 항목은 승인·환경이 없으면 체크하지 않고 차단 사유를 남긴다.
- 각 반복은 `미완료 항목 선택 → 전제·승인 확인 → 기존 코드 검토 → 구현/설계 → 영향 검사와 실제 경로 검증 → 증거 기록 → 해당 ID 체크 → 다음 항목` 순서로 진행한다.
- 관련 소스 변경으로 완료 증거가 더 이상 유효하지 않으면 이전 기록을 보존하고 해당 항목을 다시 미완료로 돌려 재검증한다.
- M1–M4 완료는 기본 제품 목표 완료다. 확장 순서가 확정된 M5 전체를 완료하지 않고 전체 roadmap/goal 완료라고 선언하지 않는다. 특정 단계만 실행하는 경우 시작 전에 그 목표 범위를 명시한다.
- 이 문서를 goal 입력으로 사용한다는 사실만으로 VM 생성·배포·과금·권한 발급을 승인한 것으로 해석하지 않는다. 실행 권한은 G0에서 별도로 확인한다.

### 재개용 실행 기록

**최신 상태 (2026-11-08 새벽, M2 진행 중): 47/108 완료(M1 완료 35 + M2-01~04·06·07·09~14), M2는 미완료.** 다음 세션은 `M2-whole-source-review-and-stamp539.json`, `M2-client-replay-redesign-result538.json`, `M2-12-owner-physical-storage-and-seed-decisions537.json`, `M2-implementation-result536.json`과 `docs/development-handoff.md`(주 저장소 및 병합 worktree)의 "M2" 절부터 읽는다. 병합 worktree `/Users/pieroot/code/palimpsest-m2-merge-20261107`(branch `goal/m2-merge-20261107`, base 7962b18, 미커밋·미스테이지, 주트리 WIP + M1 병합)는 독립 보안·병합 무결성·architecture 리뷰를 마쳤고 architecture 가드가 통과한다(exit0, `81d91b88…`/648files). 미체크 M2 항목: M2-05(실제 Afterglow OAuth), M2-08(OAuth 절반), M2-15(별도 상태 루트에서 새 VM 부팅), M2-16(두 번째 Apple Silicon 장비), M2-17(GATE) — 라이브 접근·native 승인·장비가 필요해 차단됐다. 모든 M2 검증은 loopback·합성 Keystone·SQLite 격리 수준(test-passed + 격리 종단 간)이며 live-verified 주장은 없다. 아래 첫 문단의 "35/108 … 다음 미완료 M2-01" 표기는 M1 종료 시점 기록이다.

현재 상태: **35/108 완료(M1-27·M1-28 포함, M1 완료 선언 — 범위·한계는 M1-28 증거), 다음 미완료 M2-01**. 이 문단의 나머지와 바로 아래 두 문단은 M1-27 진행 중의 시점별 기록으로 보존한다. 기존 기능·정리 증거는 유지한다. q381 실패(attempt21)는 QMP quit 1회418 등으로 정리했다. 갱신416 아래 b01은 정상 재개431됐고, deny/expiry 복원 SSH 요청의 미정 전송은 표준 mux 종료444/445로 host 측만 닫았으며 guest 상태는 읽기전용441/442로 확인했다. **attempt22(q424, 누적22/50)**는 실제 HVF QEMU에서 SSH·provisioner 준비 마커까지 진행했고 의도된 exit73 뒤 Packer가 1로 끝났다456. retained eval kernel 소실로 host Packer/QEMU custody는 재구성하지 않았다457. 이후 qcow2 root 파티션을 읽기전용 추출했으나 **추출 로그·journal 바이트는 cleanup459에서 삭제되어 복구 불가**다. 남은 것은 해시와 본 세션 도구 출력 발췌뿐이며 458 v2에 격하 기록했다. 발췌 기준으로 expiry 타이머 fallback이 게스트를 systemd poweroff로 종료시킨 것으로 보이나, 이는 게스트 측 관측이며 설계된 정상 경로(Packer shutdown_command 후 호스트가 QEMU 종료를 관측)의 증거가 아니다. 의존성 closure460은 완료됐다. **Packer bake/consumer/native 비교 harness 소스는 작성·독립 리뷰가 닫혔다(502): native501 3라운드(최종 open P1/P2 없음), fence500은 리뷰가 찾은 P0(새 case row 0개일 때 case-order가 모든 guard를 HOLD시킴)를 수정·CPU 검증, launch/consumer500 3라운드(최종 open P1/P2 없음), bake helper·generator는 첫 리뷰에서 open P1/P2 없음. CPU 검사(generator 공유표·CLOEXEC byte-identity·diff report, 5개 case sandbox 외부TCP/UDP EPERM, 소유자 ledger 편집 전송 scratch smoke)를 통과했다. 이 harness는 아직 한 번도 VM으로 실행하지 않았고 생성 22/50은 그대로다.** 내 CPU 테스트가 `plm run --help`를 띄워 SDK154 패키지 트리에 `.pyc` 109개를 만들었고 fence qualify_sdk154가 잡았다(VM 영향 없음). 기록504 후 정확히 그 파일만 제거하고 qualification 일치를 확인했다505. 현재 Mac은 배터리 전원(86%)이며 이전 효과 때(AC 100%)와 다르다. 사용자에게 AC 연결을 요청했고 전원 상태는 매 case receipt에 기록한다.

**실행 갱신 (2026-11-07, 누적 생성 30/50):** reviewed harness를 처음 실제 실행했다. **Packer success path 성공:** pb501 cold bake는 Packer exit 0(Popen→exit 58.925s), parent가 QEMU (pid,birth) 부재를 직접 관찰했고 실패 이유 없음; pc502/pc503 consumer는 정확한 stdout `count=3 sum=31`과 QEMU exit 0을 확인하고 골든 verdict GOLDEN-CONFIRMED(provision 완료·sudo poweroff·poweroff.target·expiry service 미시작); pb504 changed rebake(59.230s)+pc505는 `sum=131`로 같은 경로를 통과했다. **native lane:** `plm build` cold 4.57s(SDK total 3.712s, cache none)·changed 1.63s(SDK 0.819s, cache local), consumer n15/n16/n17 create→handoff 22.47/22.47/22.34s(workload는 이 시점 이전 완료=상한)·정확성 5개 검사 통과·정상 stop/rm/부재·owner Removed. 모든 VM row Removed, guard 통과. 증거: `M1-27-comparison-interim520.json`과 case별 receipt. 실행 중 발견·정정: native501 line942 UnboundLocalError(효과 전 실패)를 native503 한 줄 수정+516으로 기록, 내 CPU 테스트의 `.pyc` 109개 사건은 504/505. **M1-27은 아직 미체크:** spec 444의 같은 VM stop/start(restart) 시간이 양쪽 모두 미측정이라 새 reviewed 소스(native restart505, Packer table600+guest reboot tail)를 작성·리뷰한 뒤 실제 측정한다. n=1 표본이며 일반 성능 우위를 주장하지 않는다.

정정: 바로 위 "현재 상태" 문단의 "이 harness는 아직 한 번도 VM으로 실행하지 않았고 생성 22/50은 그대로다"와 "배터리 전원(86%) … AC 연결을 요청"은 소스 리뷰 종료 시점(502)의 기록이다. 이후 실행 결과·생성 30/50·배터리 86%→61%는 위 **실행 갱신**이 우선한다. 전원 어댑터는 연결되지 않은 채 캠페인이 진행됐고, 이는 타이밍 교란 요인으로 기록했다.

| 항목 | 현재 기록 |
|---|---|
| 실행 목표 범위 | 전체 로드맵: G0 → M1 → M2 → M3 → M4 → M5.1 → M5.2 → M5.3 → M5.4 → M5.5 → FINAL, 각 단계 GATE. M1만 또는 이전 SDK 하위 작업으로 축소하지 않음 |
| 현재 단계 / 작업 ID | M2 진행 중(47/108). 구현·격리 검증 완료 항목은 체크됨. **남은 M2: M2-05·08·15·16·17.** 막힌 이유: 실제 Afterglow OAuth(라이브), 새 VM 부팅(병합 트리 제품 빌드의 SDK 재적격화 + native 효과 승인), 두 번째 Apple Silicon 장비. M2-12는 소유자 결정 537(현재 구현 유지, raw 변환 제외)로 체크 |
| 실행 주체·리뷰어 | Sol 개발 + 독립 worker/no-force·asset·guest·native reviewer. 이전 provider429·보조 task audit 이력 보존, Astra 또는 미수신 승인·실동작 성공 주장 없음 |
| 적용 소스·작업트리 식별 | 주트리7962b18 혼합 WIP(주 저장소 architecture guard는 stale, 미 stamp)와 격리 트리 base6e4dff7·SDK154 불변·미커밋(격리 트리 guard exit0 `source_sha256=1f9cf5e4…` files634). M1-27 harness(증거 도구, 제품 소스 아님; 부모가 디스크로 계산한 pin): fence500 `c971350f…`·bake500 `2861bf9d…`·gen500 `519f17f5…`·launch500 `467d28b2…`·consumer500 `f4a3e165…`·native503 `3058a2a7…`·glue504 `e37e7ab6…`·fence600 `a25c1b4b…`·launch600 `ebcabd92…`·bake600 `b6baa633…`·consumer600 `87e9b1f8…`·gen600 `f831adbe…`·native restart505 `2a04a650…`. 보존 baseline cold422 `1834120e…`·helper424 `c2cb8697…` 등은 제한된 증거만 따른다 |
| 허용 동작·자원·만료·예산 | 244·400 불변. 갱신416 UTC2026-11-08T01:34:18.689437Z·same-kernel monotonic389311.159806125 고정, 하나라도 만료하면 새효과 차단. 생성33/50 비환불(Packer 23–27·31–32, native 28–30·33), 공개2019517440bytes/8GiB, Running≤2·resident≤3·각4CPU/8GiB/전체disk32GiB·physical100GiB·free300GiB 불변(직접 inventory: builder b01만 Running, physical 약38GB, free 약387GB). q381강제418·b01읽기전용441·표준mux종료444 예외는 소비/종료됐고 재사용 권한 아님. 새 native/Nova/GPU/Hub 배포·IAM·Ceph 효과는 별도 승인 필요 |
| 마지막 완료 ID / 증거 | 총47/108. M2: `M2-whole-source-review-and-stamp539.json`(병합 소스 독립 보안·병합 무결성·architecture 리뷰와 수정, architecture 가드 exit0 `source_sha256=81d91b88…`/648files, test_hub_publish 72건, 하네스 77/77·경계 11/11 최종 코드), `M2-client-replay-redesign-result538.json`, `M2-12-owner-physical-storage-and-seed-decisions537.json`, `M2-implementation-result536.json`, 소유자 결정 533·535, 병합 검증 534(정정: 539가 잃어버린 v0.3.11 수정 4건을 발견·복원), 증거 사본 `m2-e2e-evidence-copy/`(+`results-r538/`, `review-probes/client-review-r4-r6/`). M2-01 531·532. M1-27 `M1-27-comparison-final527.json`(+528, 529, 525, native restart summary), M1-28 `M1-28-gate-application530.json`. 모든 QEMU row·native consumer n15–n18 Removed, 잔여 listener/disk/hold 없음 |
| 차단 항목 | M2 라이브 증거: 실제 Afterglow OAuth(M2-05/08)와 새 VM 부팅(M2-15, 병합 트리 제품 빌드의 SDK154 적격화·fence 가드 재구성·새 native 효과 승인; renewal416 만료 UTC 2026-11-08T01:34:18Z, 생성 33/50)은 라이브 접근·승인이 필요하고 M2-16은 장비가 없다. 이 호스트에서 검증하지 못한 것: Kolla 실제 배포·MySQL/MariaDB·Manila/CephFS·Linux 전용 테스트·호스트 CI(Kolla 테스트는 venv의 `ansible-playbook`을 PATH에 두면 통과: 125 passed/1 skipped, 이전 "ansible-playbook 없음"은 PATH 문제였음). 릴리스 루프(소유자 2026-11-03 허용: 검토된 소스→patch 증가→새 immutable tag→Kolla→API 검증)는 승인 대기가 **아니라** 전제 대기: 커밋 범위 정리, Linux hosted CI·format drift, MariaDB 실행, Kolla 대상·롤백·API 검증 계획 기록, `palimpsest_api_forwarded_allow_ips` 결정. CI/소유권/데이터 보존 게이트·Ceph/IAM 발급·GPU/장치·새 유료 native·타 소유 자원 삭제는 허용 범위 밖. M1 한계(n=1, 상한 시간, 하이퍼바이저 차이, OpenStack shelve/unshelve는 M4 몫)는 유지 |
| 다음 행동 | (1) 릴리스 루프 전제 충족: 병합 worktree 커밋 범위(주 저장소 WIP와의 분리·핸드오프 정리) 확정, Linux hosted CI·format drift 정리, MariaDB 실행, Kolla 대상/롤백/검증 계획 기록. (2) M2-15: 제품 빌드 SDK154 적격화 + 새 native 효과 승인 후 별도 상태 루트에서 새 VM 부팅. (3) M2-16 장비 확보. (4) 실제 Afterglow OAuth 검증(라이브 접근 필요). 이후 M2-17 GATE. M2 GATE 전에는 M3로 진행하지 않는다 |
| 추가 실험 요구 | 제작 비용을 포함한 처음/기존산출물 재사용/소스변경 비교를 유지한다. officialUbuntu OCI base354와 그 archive keyring373→gpgv 검증된 noble/updates/security InRelease374→signed by-hash ARM64 indexes375→11개 실제.deb377(SHA 검증, 합7221720bytes)를 준비했다. 설치된 compiler/dpkg DB 복사나 guest 다운로드를 설치로 위장하지 않는다. prepared 입력·VZ 대HVF/cloudbase 차이·cold/bake/rebake/setup/readiness/전송·저장/상각·실패 비용을 구분하고 미측정은0으로 쓰지 않는다 |
| 사용자 추가 우선 지시 | reader=조회, member=생성·편집, manager=관리·제거와 선택274의 member 본인 VM·전용 boot·native 정리 및 reader/member 원본 토큰 본인 키 회수 예외를 반영했다. 독립 source 재검토·합성CPU1600검사와 등록된 main:app 실제loopback HTTP28검사331 통과: 정확한 action집합, reader쓰기거부, member게시/태그수정/실제bytes, manager삭제/키발급, reader본인회수·타인404·현재권한강등차단. SQLite/CAS·synthetic Keystone·lifespan off 범위다. 8GiB sparse tar 제외·Kolla nonroot 계약 미검증·dependency317 중복metadata 한계를 유지하며 실제IAM/Nova/GPU/배포/게시 없음 |

#### G0-03 승인·자원 행렬 — 2026-11-03

| 동작 | 현재 허용/차단 | 자원과 한도 |
|---|---|---|
| 저장소·문서·기존 상태 메타데이터 읽기 | 허용 | 명령별60초, 비밀값·개인키 내용 제외, 자원 생성0 |
| Goal 단계 내 소스·계약·테스트·문서 편집 | 설계·전제 확인 후 허용 | 기존 동시 작업과 분리한 검토 소스에서만 구현. G0에서는 현재 spec/증거 기록 편집. 공유 WIP reset/clean/stash/일괄 staging 금지 |
| 로컬 계약 검사 | 허용 | 명시된 portable/단일 경로와 격리 개발환경. 기본 명령 상한300초. `full`·native·guest-binary·실VM opt-in 자동 실행 금지 |
| 네트워크·다운로드 | 기존 공개 빌드 의존성 허용 이력과 읽기 전용 문서/명시 endpoint 조사 구분 | 새 Goal 대용량 입력의 출처·바이트·시간 한도 미확정. 현재 새 이미지 다운로드 시도0; G0-07에서 확인 |
| 기존 Lima VM·이미지·디스크·state | 읽기 전용만 허용 | start/stop/delete/resize/mount 변경0, 사용자 VM 재사용 금지 |
| 새 Lima/VZ proof VM·게스트 작업 | 미승인·차단 | 현재 허용수0. CPU/RAM/디스크/TTL/총시도·정리 범위는 G0-04 조사 후 확인 |
| 이전 SDK tag/release/Kolla/API 반복 | 해당 기존 구현·대상에 대한 standing 허용은 보존하되 현재 Goal 순서상 보류 | 정확한 검토 ref/이미지/롤백/백업 필요. 대상 확대·main 승격·CI 우회·기존tag 이동 아님. 현재 G0의 새tag/설치/서비스변경0 |
| 유료 Nova/GPU·새 스토리지·장치·Ceph/IAM 발급 | 별도 명시 승인 필요·차단 | 새 예산0. 기존 builder/share/station/peer/외부VM/키·장치 보존. 과거 spent/expired grant 유지; 환불·무조건 재시도·전역prune·강제종료·드라이버 변경 금지 |

이 행렬은 허용 범위를 기록한 것이지 미승인 자원의 승인을 획득했다는 뜻이 아니다. 부족한 권한·수량·기한·디스크 한도와 제품/보안 결정만 G0-07에서 근거를 붙여 묶어 확인한다.


항목 체크 시 바로 아래에 `증거: ID / source ref·작업트리 digest / 명령·시나리오 / 기대·관측 결과 / 증거 경로 / 검증 수준 / 자원 정리·잔여 상태`를 기록한다. 비밀값은 기록하지 않는다. 검증 수준은 `설계 확정`, `소스 검토`, `테스트 통과`, `로컬 실동작`, `배포 API 실동작`을 구분한다. 외부 증거 경로를 쓰면 재개하는 에이전트가 접근 가능한지도 기록한다.

### G0. 실행 준비·기존 구현 확인

- [x] G0-01 현재 branch/HEAD, staged/unstaged/untracked 상태와 최신 인계·아키텍처를 대조하고 보존할 기존 작업을 기록한다.
  - 증거: G0-01 / 주 트리 `7962b18e45cd87534ee1cf0c1d5c30af68e0a8a9`, 읽기 전용 working diff SHA256 `86f7f1911d510277b179aa8ebccce1313d4efcb419057ff567c603cf5f45574a`(전체 source guard 아님) / `git status/diff/log/rev-parse/worktree list`와 최신 인계·아키텍처 대조 / 스냅샷 staged0·unstaged84·untracked32. 직전28에서 증가해 동시 작업이 진행 중이며 정지된 소스로 간주하지 않음 / `~/.local/share/palimpsest-proofs/spec-goal-20261103/G0-01-baseline.json`, 본 기기에서 접근 가능 / 소스 검토 / reset·clean·stash·재배치·다른 작업 혼합 없이 세 작업트리와 기존 VM/GPU/issuer/서비스 상태 보존.
- [x] G0-02 현재 실행 목표가 M1만인지 기본 제품 M1–M4인지 전체 로드맵인지 기록하고, 문서 편집 승인과 구현 실행 승인을 구분한다.
  - 증거: G0-02 / 현재 `spec.md` 전체108개 ID와 2026-11-03 사용자 Goal 실행 지시 / 요구 범위를 전체 로드맵으로 고정하고 G0→M1→…→M5.5→FINAL 및 단계별 GATE 순서 기록 / `~/.local/share/palimpsest-proofs/spec-goal-20261103/G0-02-scope.json`, 본 기기에서 접근 가능 / 설계 확정 / 문서 편집·Goal 준비 지시를 새 VM·IAM·유료 자원 승인으로 확대하지 않았고 이전 SDK 릴리즈는 별도 이력으로 보존.
- [x] G0-03 소스 편집·검사·네트워크·로컬 VM·배포 각각의 허용 범위와 실제 자원·기한·시간/디스크/시도 한도를 기록한다. 미승인 외부 작업은 차단한다.
  - 증거: G0-03 / 현재 Goal 지시·기존2026-11-03 SDK 유지보수 허용·spec 승인 경계·AGENTS/contributor-workflow 대조 / 동작별 허용·차단·명령 한도와 미확정 자원의 허용 시도0을 기록 / `~/.local/share/palimpsest-proofs/spec-goal-20261103/G0-03-authority.json`, 본 기기에서 접근 가능 / 설계 확정 / 새 VM·유료자원·issuer/IAM·서비스 교체를 실행하지 않았으며 Goal 순서로 이전 유지보수와 분리.
- [x] G0-04 기존 Lima VM·이미지·디스크·state 루트를 확인하고, 새 proof/state 경로와 소유 자원 식별 규칙을 정해 기존 사용자 환경을 변경하지 않도록 한다.
  - 증거: G0-04 / Lima2.2.1의 `list --json`, `info`와 `df/sysctl/du/stat`, 두 기존 state 루트·선택 JSON 메타데이터 / 기본 홈에 타 작업용 중지 VZ VM1개(논리 디스크60GiB), 기존 arm64 cloud-image620224512bytes, host18논리CPU/128GiB RAM·약519.8GiB 가용 디스크 확인. 과거 ledger의 running을 현재 실행으로 세지 않음 / 위 `G0-04-{resources,lima-default}.json`, 본 기기에서 접근 가능 / 읽기 전용 로컬 도구 실동작·메타데이터 확인 / 기존 VM·이미지·캐시·state는 미변경·재사용 금지. 제안 root `~/.local/share/palimpsest-goal/sg3-b212fed8`는 미생성; 전용 HOME/LIMA_HOME/state/config/cache와 생성 전 intent·정확한 UID/ID/path/inode 대조 규칙 기록. macOS Lima 캐시는 HOME 아래 고정이므로 XDG_CACHE_HOME만으로 격리를 주장하지 않으며 실제 도구 경로 검증 전 생성 차단.
- [x] G0-05 Astra/Sol과 독립 리뷰 역할의 가용성을 확인하고, 불가한 역할과 대체 제안은 명시한다. 리뷰를 운영 승인으로 취급하지 않는다.
  - 증거: G0-05 / 현재 실행 컨텍스트 Sol·제공된 reviewer/security-reviewer 및 실제 완료한 독립 읽기 전용 scout2개 / Astra 전용 route/session은 미제공이라 확인 불가. Sol의 단계 계획+별도 독립 리뷰를 대안으로 제안하되 승인·Astra signoff로 표시하지 않음 / `~/.local/share/palimpsest-proofs/spec-goal-20261103/G0-05-roles.json`, 본 기기에서 접근 가능 / 역할 가용성 확인 / 이전 SDK 리뷰·새 소스 조사 모두 VM·배포·권한 발급 승인이 아님. 대체 결정은 G0-07에서 확인.
- [x] G0-06 CLI·build·state/inventory·OCI 변환·stage-1·Lima·Hub·OpenStack 경로를 조사해 기존/부분/누락과 재사용할 실제 심볼·테스트를 기록한다.
  - 증거: G0-06 / `7962b18+혼합 WIP` 읽기 전용 조사, 검토된 릴리즈 source guard 아님 / scout2개 소스 조사와 부모의 `StatePaths/resolve_roots`, Lima `_attach_layers`, `normalize_manifest`, `commit`, `visible_images/image_readable`, native `projection`, `CloudEnv._authenticate` 직접 대조 / `~/.local/share/palimpsest-proofs/spec-goal-20261103/G0-06-source-map.json`, `G0-06-MapLocalGoal.md`, `G0-06-MapHubRemoteGoal.md`, 본 기기에서 접근 가능 / 소스 검토, 테스트는 정의만 확인 / 기존 CAS·graph·worker·Kolla·state/inventory를 재사용하되 `/opt/layers/merged`≠실제 `/`, amd64 제한, 단일 rootfs pack≠ordered chain, live upper 정리≠안전한 사본 commit, 인증 public≠익명 public, 유한 native run≠dev/prod 서비스임을 기록. 새 테스트·빌드·VM·클라우드 작업 없음.
- [x] G0-07 아래 미결정 기술 항목을 담당 단계에 연결하고, 저장소에서 답할 수 없는 제품/보안/승인 결정만 묶어서 확인한다. 전체 구현을 설계 미결정 상태로 시작하지 않는다.
  - 증거: G0-07 / 단계별 결정 맵과 사용자 응답 `제안한 M1 실행 허용`, `Sol 계획 + 독립 리뷰` / M1 전용 제안 revision·범위·고정 만료·누적 한도를 `~/.local/share/palimpsest-proofs/spec-goal-20261103/G0-07-{decisions,approval}.json`에 기록, 본 기기에서 접근 가능 / 설계 확정·명시 승인 기록 / 생성0·다운로드0이며 기존/클라우드/배포 권한 확대 없음. 기술 설계는 담당 M1 항목에서 확정하고 후속 단계 미결정을 임의 완료로 세지 않음.
  - 후속 준비 기록: 승인 후 전용0700 runtime root와0600 owner intent를 만들었으며 `M1-private-path-preflight.json`이 실제 `limactl info/list`의 전용 home/identity 및 instance0을 기록한다. 이는 도구 경로 검증이지 VM·루트·워크로드 성공이 아니다. 생성 시도·게스트 실행·artifact 다운로드는0.

#### G0-07 미결정의 담당 단계

| 항목 | 담당 단계 / 처리 |
|---|---|
| FROM/cloud-base·ordered manifest·OS/arch·VZ 실제 루트·부팅/SSH/supervisor 경계 | M1-01~04·09·14~17·20·23. 기존 primitives를 재사용해 설계·독립 리뷰; 저장소에서 답할 기술을 사용자에게 재질문하지 않음 |
| 로컬 DB·원본 권위·기존 JSON/CAS/lock 전환 | M1-11~12·M2-03. 기존 state/inventory 안의 단일 권위를 유지하는 SQLite안을 검토. 아직 구현·최종 설계 완료 아님 |
| .plmignore·컨텍스트·안전한 capture 사본·필수 식별정보 분리 | M1-07~08·M3-01~06. 기술 계약으로 확정하며 원본 upper 정리나 재포함으로 필수 분리를 우회하지 않음 |
| dev/prod·마지막 SSH/console·재시작·5분 유예 | M1-19~24에서 로컬 설계. 이미 확정된 목표를 재질문하지 않음. 클라우드 위임·upper/shelve는 M4-09~12에서 별도 보안/운영 승인 |
| 새 로컬 실행 예산·Astra 불가 시 대안 | G0-07에서 M1 전용 예산과 Sol 계획+독립 리뷰를 사용자가 선택함. 생성 전 격리 검증·설계 리뷰는 여전히 필수 |
| 익명 public·OAuth·일반 API 키·게시 쿼터 | M2. 익명 public은 확정된 목표이고 현재 인증 public은 격차. issuer/키 준비는 실제 환경 확인 후 처리. 단위·압축·중복·버전/태그 개수·정책 하향은 M2-09~14 구현 전에 보수적 제안을 묶어 결정 |
| 두 번째 Apple Silicon·원격 배포·Git·지연 위임·유료 자원 | M2-16 및 M4의 해당 단계에서 장비/자격증명/보안/운영 전제 확인. 이번 M1 제안으로 확대하지 않음 |
| 기반 마이그레이션·devcontainer·amd64 교차 빌드·자체 Git·자유 조합 | M5.1→M5.5의 정해진 순서대로 설계·해당 제품/보안 결정. 전체 목표에서 제외하거나 미리 완료로 세지 않음 |

#### M1 전용 실행 승인 — G0-07-M1-budget-v1

| 항목 | 사용자 선택으로 승인된 한도와 보호 조건 |
|---|---|
| 적용 범위 | M1 전용 로컬 proof. 검토된 별도 소스 트리에서 구현·계약 검사·게스트 작업·직접 SSH/console·stop/start·정확히 소유한 시험 자원 정리 |
| VM | builder 포함 동시 실행2개, 정지 VM 포함 보유3개. VM당4vCPU·8GiB RAM·논리 디스크32GiB |
| 디스크 | 이미지·캐시·디스크·임시 산출물 합계 실제100GiB 이내. 새 작업 전 호스트 가용300GiB 이상. 기존 사용자 자원으로 초과를 해소하지 않음 |
| 시도·기한 | 승인 시점부터24시간, 누적 생성 시도12회. 실패·삭제 후 시도를 환불하지 않으며 한도/기한에 도달하면 새 작업 차단 |
| 공개 다운로드 | 누적8GiB. 공식 Ubuntu cloud-image/APT·공식 Ubuntu Docker Hub·공식 BuildKit/필요 빌드 의존성 및 검증된 배포 endpoint. 사설 자격증명이나 호스트 키 상속 없음 |
| 격리 | 미생성 root `~/.local/share/palimpsest-goal/sg3-b212fed8` 안의 HOME·LIMA_HOME·state·config·cache. 실제 도구가 해당 경로를 따르고 기존 SSH 키/캐시를 쓰지 않음을 생성 전에 확인 |
| 정리 | 사례가 끝나면 필요한 한정 증거를 보존하고 정확한 campaign/source/UID/runtimeID/path/inode를 대조한 시험 consumer·전용 디스크만 정상 종료·제거. 불확실한 생성/삭제·정리 실패는 새 사례 차단 |
| 제외 | 기존 사용자 VM/state/이미지/캐시 변경·재사용, Docker Desktop daemon/cache 변경, 호스트 패키지·LaunchAgent/영구 서비스 설치, Nova/GPU·Ceph/IAM·새 배포/공개·다른 장비 작업 |

사용자가 **Sol이 단계 계획과 개발을 담당하고 별도 독립 reviewer/security-reviewer가 검토**하는 대안을 선택했다. Astra의 참여·서명으로 표시하지 않는다. 운영 범위는 위 M1 승인과 고정 만료에 한정한다.


### M1. 로컬 Dockerfile → 실제 VM 개발환경

의존성: G0. 첫 대상: macOS Apple Silicon / Lima VZ arm64. 완료 근거: L0, L1, R1, S1 및 아래 실동작 항목.

#### 기반·빌드·로컬 메타데이터

- [x] M1-01 cloud image와 OCI rootfs를 연결할 계약을 설계하고, 원본 OCI 기반을 생략해 cloud image에 상위 변경분만 붙이는 잘못된 접합을 방지한다.
  - 증거: M1-01 / 격리 트리 `6e4dff7+설계 문서`, 당시 계약 snapshot SHA256 `b4ba8d227bb511563e677e8257c4390f049723715bb6e7768eeb3c4307962975`를 `M1-01-vz-native-root-spec.snapshot.md`로 보존 / 기존 mode·graph·root 전환 및 Lima2.2.1 boot/share source 대조, 독립 base-binding 리뷰와 upper 신원 결함 수정 후 boot-boundary closure / `~/.local/share/palimpsest-proofs/spec-goal-20261103/M1-01-design-verification.json`·고정 snapshot·리뷰2개, 본 기기에서 접근 가능 / 설계 확정 / application은 정확한 cloud-root projection의 명시적 FROM·검증된 suffix, OCI-root는 원본 전체 체인과 boot-only cloud 역할로 구분. 후속 M1-02 계약 추가는 별도 SHA로 기록하며 구현·실제 `/`·SSH 성공으로 세지 않음.
- [x] M1-02 cloud image·부모 레이어·순서·digest·아키텍처·실행 설정·출처를 표현할 기존 manifest 재사용/확장안을 확정한다.
  - 증거: M1-02 / 격리 트리의 canonical contract SHA256 `86b63d059ee53bd7b2120cb3c0ce046bba1ac827378baabeac9e437ced4298cb`, 고정 `M1-02-manifest-contract.md` SHA256 `8a6d2b38049d947387f025a8dc6eab31774d3f89872f560854285994192a4fe8` / 독립 projection·caller/cutover 리뷰9건 수정·closure / `~/.local/share/palimpsest-proofs/spec-goal-20261103/M1-02-design-verification.json`·고정계약·리뷰2개, 본 기기에서 접근 가능 / 설계 확정 / v1 typed base_root는 absent-only, 실제 cloud-base node+순서 suffix, explicit validation intent·독립 source qualification·조건부 호환성 권위를 확정. 기존 OCI/read identity 보존, M1 publication은 amd64 OCI-only 유지. schema/caller/변환/guest 구현·배포는 아직 완료 아님.
- [x] M1-03 관리자가 허용할 registry/namespace/repository와 확인 가능한 게시자 정책을 설정하고, 정규화된 ubuntu·nvidia/cuda 같은 예시의 매칭·거부 동작을 검증한다.
  - 증거: M1-03 / 격리 트리 base6e4dff7, source07 archive SHA256 `e9f1cadf32833c647e2dc0f70755a5a6653a1f6793f8797e75434c7b810c4c32`, 실제 working source guard `12b3a2aac24b9d3a66faf1605a4ce1e662ca2b7b717f693aebd099c8c496c216`/609files / exact normalized ubuntu·nvidia/cuda 허용, lookalike/empty/revoked 정책 거부, warm/retained/Docker/run·BuildKit engine fence·최종 publication/HOLD를 구현·독립 source closure. 최종4lane7350pass/22skip/142warnings,380.66s; root1301/2skip, Hub1670/2skip+동일 sparse node 별도1pass와06→07 byte 동일 근거를 보존. 실제 CLI·BuildKit 이름/번호 stage COPY의 정확한 OCI 파일·현재 정책 pipe/HOLD·loopback HTTP/SQL 검증 통과 / 위 `M1-03-verification.json`과 연결 receipt들, 본 기기에서 접근 가능 / 소스 검토·테스트 통과·로컬 실동작 / helper 실행 입력은 증거 보존 뒤 제거했고 필요한 새 builder1개만 승인 안에서 유지. 예약량≠총network실측, reviewer metadata 사고·초기 실패·미수령 출력 이력 보존. container/Kolla·Keystone·이미지 획득·실제 VZ user root·클라우드/GPU 성공으로 세지 않음.
- [x] M1-04 가져온 이미지의 digest·아키텍처를 고정하고 os-release로 지원 OS·버전과 부팅 방식을 선택한다. 미확인/미지원 OS는 실행 전에 차단하며 위험 승인으로 우회하지 않는다.
  - 증거: frozen source100·guardc90a4544…/634files와 실제 n06/sourcebinding6f2bdbba…·manifest74ba65eb…·linux/arm64. 원본 layer0의 유효 os-release digest87458b3b…가 실제 게스트 Ubuntu24.04.5의 파일 바이트와 일치하며 ubuntu24.04/lima-vz/direct-linux 정확 profile에 결합됐다. 현재 qualifier/platform/request/CLI 파일이 frozen100과 바이트 동일함을 확인하고 관련182검사/1.23s로 unknown/unsupported OS·버전·arch·digest 변조·risk/force 우회 거부를 검증했다. native 소비자가 원본 CAS를 재검증해 materialization·run 예약·VM launch 전에 바인딩한다(CAS/config 파일 생성까지 무효과라는 주장은 아님). `M1-04-n06-actual-OS-profile-binding119.json`, `M1-04-current-source-qualification-checks124.json`, actual-root106/107. 본 기기 공통 proof 경로에서 접근 가능 / 소스·검사·로컬 실제 루트. 별도 cleanup HOLD와 후속 재시작 오류는 아래 기록대로 미해결 유지.
- [x] M1-05 Dockerfile+COPY 소스+컴파일+패키지 설치를 포함하는 최소 결정적 검증 입력과 예상 출력·파일 내용을 정한다.
  - 증거: M1-05 / 격리 트리 `tests/fixtures/vz-native-root/` 5개 입력의 SHA256·바이트와 source12 product/source11 runtime을 `M1-05-verification.json`에 고정 / 실제 local CLI→소유 guest Buildx0.30.1 remote→BuildKit0.34.1에서 pinned Ubuntu ARM 원본 child·TCC exact version·libc6-dev 설치·COPY·실제 C compile/RUN 성공. stdout41bytes `palimpsest-vz-native-root\ncount=3\nsum=31\n`, report15bytes `count=3\nsum=31\n`, 원본 source/input/Dockerfile·ARM ELF183·첫 base descriptor/DiffID·CMD/workdir 일치 / 같은 proof 디렉터리 `M1-05-fixture-{build-source12b,observation-source12}.json`, 본 기기에서 접근 가능 / 입력·기대 계약 확정 및 로컬 실제 빌드 / APT dependency closure·최종 image byte 재현성이나 실제 VZ 사용자 `/`·SSH 성공은 아님. daemon 격리 `/tmp`·`/run/runc` 초기 실패는 보존하고 정상 stop/PID0 뒤 범위 한정 수정; 새 VM 생성0, 필요 builder1개 유지.
- [x] M1-06 로컬 전체 빌드 컨텍스트와 Dockerfile 경로를 전달하고, Dockerfile만으로 충분한 빌드도 지원한다. 소스 누락 실패를 보여주며 다른 소스로 조용히 대체하지 않는다.
  - 증거: M1-06 / source13 실제 product Buildx remote와 source14 동일 runtime AST·guard `4d0d9207…/619` / 외부 Dockerfile+원본 전체 context의 정확한 COPY, config-only Dockerfile 성공, 필수 소스 누락과 부모 디렉터리 대체 금지 실패를 실제 관측. engine의 정당한 빈 cache export로 발생하던 제품 오류는 optional local만 null digest/zero bytes/no promotion으로 수정하고 partial/missing/native mandatory cache 거부 유지. focused553pass, 4lane7598pass/20skip 후 offline hatchling cache 전제 실패 node 별도1pass, 독립 cache audit findings[] / 위 `M1-06-07-verification.json`·`M1-06-cacheless-{before-source12,export-shape}.json`, 본 기기 접근 가능 / 소스·테스트·로컬 실제 빌드 / source writeback 없음, bounded daemon 정상 stop/PID0. VZ root·배포·한 번의 full green run 주장은 하지 않음.
- [x] M1-07 .plmignore 문법·기준 경로·.dockerignore와의 적용 순서를 문서화하고, 제외·재포함·경로 경계의 실제 빌드 결과를 검증한다.
  - 증거: M1-07 / 같은 source13/14·guard619 및 local Dockerfile 계약 / source12에서 제외 파일 포함 결함을 실제 재현한 뒤 BuildKit matcher의 baseline→root `.plmignore` 순서, `!` 재포함·ancestor/sibling 경계·외부 Dockerfile 전용 ignore 우선·제외된 필수 COPY의 실패를 실제 산출물로 검증. 원본 context/Dockerfile은 미변경, 임시 정의는 solve/export 이후 성공·예외에 정리하며 외부 sidecar도 key에 결합. focused553pass·실제6case 기대 결과·독립 ignore audit findings[] / 위 `M1-06-07-real-product-cases13.json`, `M1-07-ignore-before-source12c.json` 및 verification receipt, 본 기기 접근 가능 / 소스·테스트·로컬 실제 빌드 / Docker ignore 문법 부분집합으로 명시, remote Git/Hub worker/commit·필수 식별정보 분리까지 구현했다고 주장하지 않음.
- [x] M1-08 기본 소스 제공은 레이어에 포함하는 방식으로 구현한다. 호스트 소스 직접 마운트는 명시적으로 선택했을 때만 적용되도록 검증한다.
  - 증거: source151 guard2cdc2e0e…/634files·installed wheelbf203328…·source/guest workerf7f25843…의 실제 n07(4320e45b…/upper78c6c8d0…). 설치된 `plm build`의 동일 state45 라벨을 `plm run`이 production resolver로 선택했다. 실제 `/` OverlayFS와 PID1 root가29:2로 같고 원본5readonly lower004→000 위의 embedded probe.c/input bytes가 명시적 `/work/host-source`와 일치한다. 공유는 CWD 프로젝트 아래 `tests/fixtures/vz-native-root` 하나를 `--mount-source …:ro`로 지정했으며 실제 mountinfo의virtiofs `ro`와 guest write EROFS를 관측했다. 원래 C프로그램 stdout41/report15와 upper 작업 파일은 두 설치 CLI exec에서 성공했다. 이전 default-no-share n06 실제 mount/접근106/107·source100→151의 이 경로 소스 검토를 함께 근거로 삼는다. 최초174는 공유 디렉터리=CWD라 parser가 실행 전 거부했고 provider/record 부재175·수정 parser176 후 새 intent177만 실행했다. 시도8은 환불하지 않아 현재9/12, builder+n07 Running2이며 완료 정리 전 새 사례를 무단 추가하지 않는다. `M1-08-installed151-n07-explicit-source-share179.json`, `M1-installed151-n07-corrected-create-result177.json`, second-alias-exec179. 실제 로컬 실동작이며 공유 kernel/hostile-guest 보안 동등성이나 cross-VM upper 독립성까지 완료한 것은 아니다.
- [x] M1-09 기본·부모·순서를 보존하는 읽기 전용 레이어 체인을 생성하고, 전체 rootfs 단일 블록 포장을 체인 재사용 완료로 오인하지 않는다.
  - 증거: source27 `f4f5cf45…`·source28 문서·guard `475b0b10…/619`; 원본 ARM manifest `7f62247f…`의5 compressed descriptor/DiffID·ordinal·process를 보존한 public CLI cold/warm, 새 parent-bound recipe와 동일 per-layer receipt, 역순 거부·verified artifact readback. 새 VM/root 실행 증거가 아님. `M1-09-10-completion28.json`.
- [x] M1-10 SquashFS 저장·압축과 별도 manifest의 역할을 고정하고 파일 소유자·권한·심볼릭 링크·삭제/whiteout·덮어쓰기 의미가 변환 후 유지됨을 검증한다.
  - 증거: 같은 reviewed source; v3 canonical lower-only markers·삭제/종류 장벽·생략 parent/root metadata 계승·일반 xattr 보존·parent-aware cache. 실제 canonical SquashFS와 mounted OverlayFS의2 marker순서 exact tree/UID/GID/mode/mtime/links/overwrite/delete/EROFS 쓰기 거부, 동일 leaf·다른 parent의 실제 cold/warm 분리1case 통과·loop 없음. focused1178·4lane7746pass/20skip·문서143·Ruff/guard·독립 source closure. SquashFS payload와 별도 graph/process manifest 역할을 계약에 명시했으며 cross-layer hardlink는 명시적 거부, 실제 VZ `/` 주장은 없음.
- [x] M1-11 현재 JSON 기록과 로컬 DB의 통합/전환 방식을 설계한다. 기존 state/inventory 진입점을 검토해 별도 우회 저장소를 만들지 않고 DB 기술·스키마·원본 권위를 문서화한다.
  - 증거: source28 코드 기반 source/caller map과 architecture/VZ contract의 SQLite selected-root catalog 설계. DB=durable discovery/read model, JSON/CAS/owner/lease/Hub 권위 보존, known ancestry/ordinal/부분commit·상태이동·불완전 관측·lock순서·fail-visible DB 경계 확정. 별도 readonly process SQLite3.45.1이 실제 원본5layer rows를 영속 조회했고 중복ordinal/FK 거부·반복bytes 허용·rollback 보존을 관측, 임시DB 정상 제거. 독립 권위/출처 source review 미해결 차단 없음. `M1-11-design-completion28.json`. M1-12 구현/실제VM 연결 성공 아님.
- [x] M1-12 빌드·기반·레이어 체인·태그/이미지·실행 연결 정보를 로컬 DB에 기록하고, CLI 프로세스 재시작 후 같은 결과를 조회할 수 있음을 검증한다.
  - 증거: installed SDK151의 같은 worker-state45에서 실제 build bk-00c8cdbc684c/bk-7de8f6451fb4·각 output label·서로 다른 archive hash와 같은 manifest4f0d99…를 기록했다. 실제 n07 run4320e45b…는 source imagec285e901…/snapshot2d5f3441…·5ordered lower에 연결되고 실제 `/`/원본C 동작179로 보강됐다. fresh `plm store catalog --refresh` 뒤 별도 `palimpsest store catalog`의 JSON이 complete/rev6·7runs/2builds/2images로 완전히 같다181. 실제readonly SQLite SELECT203은 build/label2개×source/derived/run ordinal0~4의10행 모두 일치함을 확인했다. 독립 source/schema 검토 ReviewCatalogEdges192: OCI-root는 원본 layer0부터 전체 체인으로 기반을 표현하며 cloud-image base_digest NULL/unknown parent를 임의로 채우지 않는 설계에 부합한다. build→image는 exact manifest join(다대일 가능), run은 imageID+snapshot binding 일치이며 유일한 build호출의 launch 증명은 label resolver173·실제argv177로 별도 유지한다. output labels를 native registry tags로 위장하지 않는다. `M1-12-installed151-same-root-fresh-process-catalog181.json`, `M1-12-installed151-catalog-join-verification203.json` / 실제로컬 DB·새CLI프로세스·빌드/실행 검증, DB를 lifecycle/payload 권위로 사용하지 않음.
- [x] M1-13 plm과 palimpsest가 동일한 구현을 호출하도록 설치 진입점을 제공하고, 설치된 두 명령으로 build/run/exec/상태 조회 경로를 실행한다.
  - 증거: independently-reviewed SDK152 guard92f672c3…/wheel2fa5aa48…을 승인162의 새 sdk-host152 prefix에 offline/no-deps/copy 설치하고 실제173package파일/165Python모듈을 frozen source와대조했다213/214. 두 console_scripts가같은 cli:main을가리키며 PYTHONPATH/editable/source주입없이동일state45에서 현재설치된 `plm build`와 `palimpsest build`가실제성공해같은manifest4f0d99…를반환234, privateBuildx/SSH/임시unit정리235도완료했다. 같은prefix의 `palimpsest run`은실제n08 생성225, `plm run`은현재buildlabel에서실제n09 생성236에성공했다. 양쪽실제exec/ps는n08의자기upper226과n09의UID/GID1000·cwd·원본C결과238을확인했고, publicinspect/logs도실행했다. `M1-13-sdk152-current-prefix-dual-build234.json`, `M1-13-25-sdk152-n08-create-result225.json`, `M1-13-19-20-sdk152-n09-create-result236.json`, `M1-13-20-sdk152-n09-genuine-main-and-dual-entrypoint238.json` / 현재설치본의실제build/run/exec/status; helper나test stub로대체하지않음. source152 live동시조회216/219 결함·prodUnlessStopped/OOM/전체mode수명주기는별도미완료다.

#### 실제 루트·접속·실행 모드

- [x] M1-14 호스트 저장/마운트→게스트 장치 또는 공유 경로→읽기 전용 lower 전달 설계를 확정한다. 호스트 경로가 게스트에서 동일하게 보인다고 가정하지 않는다.
  - 증거: source36 `201eed47…`·codeguard `f1793104…/621`; exactLima2.2.1 commit27bc4c4b의boot/share/tag/default source와installedvalidate--fill에서 distincthost/guestpath·RO·tag`lima-4f213fc473c0c7df`·plainmount제거·explicitRosettaoff를 확인. Canonicalstore/orderedlease→allowlisted sharedprojection→guestordinallower·지속custody와 run/root-volume→preformattedprivateext4/formatfalse→uniqueguestUUID+nonce first-use/resume 계약을 확정. 독립설계리뷰2개미해결차단없음·143문서계약pass. `M1-14-design-verification36.json`, 본기기접근가능. 검증수준=설계확정/소스·실제구성; VM생성0·nativeearlymount/쓰기거부/실제root/접속/upperresume 미검증. 검증용공개5lowercopy35.5MB만보존·기존VM/권한미확대.
  - 후속 재개: source36 초기 체크 뒤 `ReviewVZCustody`가 missing/recreated projection의 pre-start/resume gate 누락을 P2로 보고했다. 위 초기 증거는 보존하되 항목을 다시 미완료로 전환했다. Source-backed Lima auto-MkdirAll를 실패로 기대하지 않고 retained identity/payload/lease/완료binding 확인을 매create/start/resume 전에 요구하도록 정정·재검토 중이다.
  - 정정 후 완료 증거: source37 `ef9c1bfa…`/runtime35동일·guardf1793104/621, spec8943의매create/firststart/resume retaineddirectory/완료publication/정확한payloadallowlist·identity·hash/size/lease/runref사전검사와 unknowncleanup resume차단·effective/persisted uppername/formatfalse검사를 명시했다. 두 최종 source-only리뷰가P2닫힘/남은P1P2없음을 보고했고 문서143pass/6.76s·workingguard확인. `M1-14-design-verification37.json`이 초기완료를 대체하며 재개이력은보존. 설계확정만완료; nativeboot/root/접속미검증·VM생성0.
- [x] M1-15 부팅·init/supervisor·SSH·console을 제공하는 플랫폼 구성요소와 사용자 이미지의 경계를 정한다. 주입 정보의 commit 제외와 용량 집계를 문서화한다.
  - 증거: source38 `8ee66463…`/code35동일·guardf1793104/621, spec1A65의 staticARM first-partyplatform·privatecontroljail/actualPID1root/원본process·authenticatedtrustedclasses/prestartkey/genuineLima·AF_INETadapter/strictvsockhandoff·Ubuntu24정확주입/placeholder·copy-only필수제외·peak/RAM/physical계약. Access P2(직접vsockFD→Dropbear불가)는 연결별AF_INETpair로정정했고 두최종source-only리뷰남은차단없음. 문서143pass/7.00s; 실제busybox/agent staticARM/ash, guest35APT/offlinegcc/musl/qemu, exactSSHtar e098…→staticARM7ecd…/506576bytes/version만관측. `M1-15-design-verification38.json`, 본기기commonproofdir접근가능. 설계/컴포넌트 수준; firstpartyhelper/initrd/SSHauth/nativeboot/root/upperresume 미검증·VM생성0·호스트설치/공개0.
  - 재개/정정: 독립 native 리뷰와 부모의 pinned Lima2.2.1 `pkg/cidata/template.go` 실제 읽기에서 `ValidateTemplateArgs`의 root·UID0 거부를 확인했다. YAML validate 성공만으로 Create 가능성을 확정한 이전 설계 결론을 철회하고 체크를 재개한다. 실제 private nonroot bootstrap·진짜 sudo·별도 root control/admin·원본 OCI User 보존 및 stock guestagent/CIDATA pre-effect 집계를 정정 중이다. 아직 provider Create/새 native VM 시도는 하지 않았다.
  - 후속 관측49/정정50: 실제 CLI create는 private plmctl/1000 설정으로 provider를 Stopped까지 생성했지만 시작하지 않았다. CLI create의 내부 Prepare가 이미 만든 boot inputs를 재생성하려던 가정을 철회했다. Frozen50은 새 성공 create의 정확한 입력 admission만 허용하며 기존49는 삭제 전용이다. C44/asset44/worker45와 custody50의8개 filesystem/flock 경계 검증은 컴포넌트 증거이고 native root/SUID/SSH 성공이 아니다. 현재 항목은 미완료 유지.
  - 최신61 재개: actual ABI2 C44/asset44/worker45·source61 affected894pass/15skip·current632file guard·private UI 정상종료까지 확인했다. 실제49의 정상 RM은 삭제 전 설명되지 않은 carrier ctime에서 정지했으며 failed/HOLD 유지, 삭제시도0·nativeboot0·누적3/12이다. Runtime full-stamp/actor guard는 유지하고 실제 게스트 권한/루트/접속 검증 전 M1-15/16을 완료로 체크하지 않는다. `M1-16-current-checkpoint61.json`과 source61 인계가 재개 정본이다.
  - 완료 증거124: 위 설계 철회·실패 이력을 보존하고 source100의 실제 platform142ed75e…/assets103에서 private plmctl1000·진짜 sudo의 real/effective/saved/fs IDs1000→0, 잘못된 키 거부·정상 재핑을 Linux namespace component102로 확인했다. n06은 실제 Lima Create/부팅과 사용자 이미지 원본 User0·cwd·CMD, CLI/콘솔/SSH 접속에 성공했다. exact Dropbear7ecd073d…/506576bytes pubkey-only pin과 loader99의 old-accept/new-reject를 유지한다. ABI2 platform/control/access와 사용자 `/`의 경계, mandatory copy-only capture 제외, CIDATA/boot copies/캐리어/전용 upper/공유 projection/물리·RAM 집계는 현재 VZ 계약에 명시했고 actual n06 accounting을 completion124에 보존했다. M3 commit 구현·다른 OS 지원을 주장하지 않는다. `M1-15-18-n06-root-access-boundary-completion124.json`; 현재 정상 제거 HOLD는 별도 미해결.
- [x] M1-16 Lima VZ arm64에서 기반+레이어들이 실제 게스트 `/`의 OverlayFS lower가 되고 각 VM에 전용 upper/work가 생기도록 구현한다.
  - 증거: 같은 source100/assets103/profile104의 n06 원본5layer digest/ordinal을 실제 loop readonly mount와 결합했다. authenticated root29:2/inode와 PID1 root가 같고 사용자 `/`도 overlay이며 lowerdir004→000, 전용 upper/work는 root volume4f0fa194…에 있다. create105·actual-user-root106·public-CLI107·completion124. VZ actual-root 실동작이며 `/opt/layers/merged`나 chroot 대체가 아니다. 다른 VM과의 upper 독립성 실증은 M1-25로 남고, 최종 cleanup HOLD를 숨기지 않는다.
- [x] M1-17 mountinfo·파일시스템 식별·lower 순서·쓰기 관측으로 실제 루트를 검증한다. df 화면, /opt/layers/merged, chroot 실행만으로 대체하지 않는다.
  - 증거: actual-user-root106의 mountinfo·overlay magic794c7630·root/PID1 identity, authenticated5lower의 ordinal/digest/readonly 관측, public-CLI107·console115·direct-SSH116의 실제 사용자 `/` 재확인 및 readonly lower000 쓰기 EROFS. 전용 upper에 쓴 서로 다른 접속 경로의 파일은 정상 stop/start 후 재조회했다. completion124·persistence-summary124. 다른 VM 격리는 아직 별도이며 카탈로그만으로 실제 mount를 추론하지 않는다.
- [x] M1-18 CLI console과 직접 SSH 각각에서 빌드된 소스 결과를 실행하고 파일을 수정한다. VM 가동과 실제 접속 가능 상태를 구분한다.
  - 증거: 실제 공개 CLI `shell` PTY console115와 pinned 생성 config를 쓴 별도 OpenSSH PTY116에서 original embedded `./probe input.txt report.txt`를 실행해 stdout41bytes·report15bytes를 얻고, 각 경로에서 별도 작업 파일을 썼다. 처음 console114는 기본 cwd `/`에서 실행 실패했으므로 해당 과대성공 필드는 correction115로 철회하고 `set -eu; cd /opt/vz-native-root` 뒤의 실제 성공만 사용한다. 원본 Dockerfile/probe.c/input digest도 별도 관측했으며 실제 소스 편집·재컴파일과 두 접속 파일의 재시작 후 보존은118/124가 확인한다. 설치된 `plm`/`palimpsest` 양쪽 검증은 M1-13으로 남는다.
- [x] M1-19 dev/prod와 restart 설정·기본값을 제공한다. dev/no, prod/unless-stopped 및 명시적 사용자 중단의 우선순위를 검증한다.
  - 증거: flags를 생략한 n08의 actual dev/no225/226, 명시 prod/no n09의 genuine main exit7 뒤132초 무재시작241/242, 새 profile246 n10의 `--mode prod`·`--restart` 생략 → actual canonical plan prod/unless-stopped를 결합했다. n10은 정상 stop 후 explicit_stop_fenced=true·PID0·provider Stopped가437초 뒤에도 유지됐고 정상 rm/전용 disk·6 lease 부재 및 공유 보존까지 확인했다. completion246·explicit-stop-priority-complete246·final-absence246. 후속 조회 소스153의 검증이나 kernel/host 무고장 보장은 아니다.
- [x] M1-20 Entrypoint/CMD의 인자·환경·사용자·작업 디렉터리와 사용자 재정의 계약을 유지하고 주 작업의 실행 상태·종료 코드·로그를 조회한다.
  - 증거: `M1-20-default-and-override-acceptance312.json`. SDK154/profile246 n12는 nonempty Entrypoint의 fixed/empty prefix와 CMD의 space/semicolon/dollar-paren 경로를 그대로 받고 UID/GID1000·image env/cwd·sum131·main exit0와 공개 logs/inspect를 증명했다. n13은 명시 CMD가 EntryPrefix를 보존하면서 argv0·`$HOME`·`semi;colon`·빈 인자를 literal로 유지하고 `--user0:0`·원래 env/cwd·main exit23/logs를 증명했다. CLI에 없는 env 재정의 옵션은 주장하지 않는다. n12 정상 제거/전용 disk·6lease 부재309; n13은 dev lifecycle 검증을 위해 필요한 상태로만 보유한다.
- [x] M1-21 prod의 주 작업 종료·크래시·통제된 애플리케이션 OOM에서 VM/upper를 유지하고 재실행한다. 반복 실패 지연·상태 보고와 명시적 stop 뒤 재실행 금지를 검증한다.
  - 증거: SDK152/profile246 n10(3176537b…/upper5232f472…)은 main PID 자신의 정상7, self-SIGABRT134/6, main-cgroup memory.max268435456/swap0의 OOM137/9 뒤 같은 boot483c155e…와 작업 카운터를 보존하며7회 launch했다. PID1은 root cgroup, SDK 사용자 세션은 application/sessions로 분리했고 allocator의 최대 touch는512MiB였다. 실제 memory.events는 oom1/oom_kill2/oom_group_kill1이며 두 번의 독립 OOM으로 과장하지 않는다. 335 authenticated SDK status 표본과 공개 ps/inspect/logs, 실측 약2.03/3.85/8.00/16.16/31.90초 후속 지연을 보존했다. 60초 cap의 실측이나 모든 순간을 공개 CLI로 polling했다는 주장은 없다. 정상 stop의 signal15/exit143·fence true와437초 Stopped 우선순위 뒤 정상 rm/전용 disk·rootrecord·6 lease·export·actor/HOLD 부재, 5 lower/원본 보존을 완료했다. `M1-19-21-prod-default-normal-crash-bounded-oom-completion246.json` 및 raw246 증인. Source153 동시 조회/Packer 증거가 아니다.
- [x] M1-22 prod는 SSH/console 연결이 모두 종료돼도 계속 실행한다. dev는 마지막 세션·재접속·5분 유예를 기준으로 보존 정지하도록 세션 관측과 경합 처리를 검증한다.
  - 과거 부분 실증: source134 n06의 자연 idle stop136과 동일 upper 재개138은 보존하되 이것만으로 전체 세션 계약을 완료로 세지 않았고 당시 metrics 결함139가 있었다. 후속 실제 전체 증거는 아래329다.
  - 완료 증거: `M1-22-dev-and-prod-full-lifecycle-completion329.json`. SDK154/profile246 n13의 실제 PTY 단절 후272.648초에 재접속했고 같은 bootd5604a7b…에서 다른 PTY를 함께 열었다. 하나가 닫힌 뒤 남은 PTY만315.560초 동안 살아 있어 원래·새300초 deadline을 넘겨 실제명령/marker를 반환했다. 마지막 정상단절 뒤 sessions count0/knowntrue를 확인하고 guest probes 없이 기다려324.773초 시점 provider Stopped·upper 보존을 관측322. 앞선 자연정지319→공개 start320→marker/새boot321 보존도 실제다. prod n11은 실제 PTY 정상단절·count0/knowntrue 뒤331.614초 동안 guest 요청 없이도 provider Running/mainExited7을 유지329. n13과n11 모두 역할 종료 후 정상 rm·전용disk/root/6lease 부재323/330. 최초0×0 PTY harness 거부313 및 부모가 재접속창을 놓친319는 성공으로 재라벨하지 않는다.
- [x] M1-23 주 명령이 없는 dev와 명령이 없는 prod의 동작을 명시하고 검증한다. 게스트 커널/물리 호스트 무고장을 보장한다고 표시하지 않는다.
  - 증거: `M1-23-commandless-dev-prod-actual-acceptance336.json`. 실제 zero Entrypoint/CMD OCI manifest37ca0bd5…는 원래 다섯 레이어 bytes를 유지한다. prod attempt18은2.141초 exit1로 VM/canonical run/native worker journal 생성 전에 거부333. dev attempt19 n14는 main absent/PID0/null exit인 실제 root VM에서 root PTY와 image UID1000 structured exec가 각각 TCC컴파일·sum131·파일작업을 수행했다. PTY root cwd `/`와 exec image cwd는 다르며 root0600 marker의 UID1000 접근 거부를 permission weakening으로 바꾸지 않았다. 정상 stop/rm·전용disk/root/actor 및6lease 부재337; 생성19회 비환불.
- [x] M1-24 stop/start 후 소스 수정·작업 파일·패키지 환경이 유지되고 RAM/프로세스 연속 복원은 요구하지 않음을 확인한다.
  - 증거: n06에서 probe.c에 고유 comment를 쓰고 tcc로 probe-persist를 실제 컴파일·실행했다. 오프라인 실제 dpkg package `palimpsest-n06-persistence-proof=0.0.1`를 설치하고 공개 CLI의 Running→stop→start118 뒤 source/binary/input/report/package payload5개 SHA256와 libc6/libc6-dev/TCC/proof package4개 설치 버전·상태가 정확히 같음을 재조회했다. 원래 exec·console·직접 SSH 작업 파일3개도 보존됐다. `M1-24-n06-source-package-{setup,after-restart}118.json`, `M1-24-n06-live-explicit-{stop,start}118.json`, `M1-24-n06-source-package-persistence-summary124.json`. 로컬 실제 stop/start, 새 생성0·카운터7 유지·RAM/process snapshot 주장은 없음. 별도 자연 idle→start109 실패와 나중 cleanup120 HOLD는 이 정상 명시 stop/start 성공으로 덮지 않는다.
  - 강화된 현재 source 실증135/138/142: guardf9e5334a…의 host source134가 정확한 같은 n06/upper를 cold stop/start했다. 실제 before/after boot ID b47544d3…/e330f021…는 다르고, 중간 provider Stopped·PID/actor/socket 부재와5개 SHA256·4개 package 설치 기록 일치를 직접 보존했다. 자연 idle 뒤 새boot7e98e075…도 같은 파일/패키지다. `M1-24-n06-source134-cold-restart-persistence135.json` 및 raw witnesses, `M1-16-n06-final-absence-and-preservation142.json`. 정상 최종 정리까지 완료했으며118 stdout만으로 cold boot를 추론하던 증거 한계를 보강했다.
- [x] M1-25 같은 lower를 쓰는 별도 VM의 upper가 독립적이고 lower를 변경하지 못함을 실제 쓰기 결과로 검증한다.
  - 증거: SDK151→reviewed152의 같은 manifest4f0d99…/source imagec285e901…를 실제 n07(A4320e45b…/upper78c6c8d0…)와 n08(B29652ffb…/upper6cf505f6…)에 결합했다. B는A의n07/GUI작업marker가 없고, probe.c를339560…→a087e0…로수정/TCC재컴파일·실행한뒤에도 원본lower003 source339560…가같고 lower쓰기EROFS였다226. B정상Stopped확인뒤A를다시시작해 자기marker4개와원본source339560…가유지되고 B의수정source/binary/report/marker가없음을실제검증227. 두plan의5orderedSquashFS digests는동일하고전용ext4UUID는서로다르다. 비교는정해진2running/3resident한도에서순차접속했으며동시runtime-race보안실험을위장하지않는다. 두consumer와전용disk를정상stop/rm231한뒤freshprovider부재·rootrecord없음·각group+member총12lease권위부재·exportrefs없음, 공유5SquashFS fullbyte/structure와SDKbuiltarchive2개/원본archive/hostfixture보존232를확인했다. `M1-08-25-sdk152-n08-default-share-private-write226.json`, `M1-25-sdk152-n07-reciprocal-upper-read227.json`, `M1-25-sdk152-paired-final-absence-and-preservation232.json` / 실제로컬VM쓰기·재기동·정상삭제까지완료. 생성10/12유지·필요builder만Running·force/수동native정리0.
- [x] M1-26 소스 변경 후 새 빌드→새 VM의 결과가 달라지고 기존 이미지·VM은 유지됨을 확인한다. 로컬 DB·실행 결과·원본 digest를 증거로 연결한다.
  - 증거: `M1-26-changed-source-new-vm-original-preservation326.json`. 공개 build263의 C sum 초기값 변경은 새 manifest e81b5280…와 n12/n13에서 sum131을 만들었다. 같은 최초3 lower를 재사용하고 끝2 lower가 달라졌다. 원본 n11은 변경 전297과 변경 소비자 제거 뒤 재개325에서 원래 manifest4f0d99…·source339560…/binarya1b306…·sum31·자기 private marker를 유지했고 새 consumer marker는 없었다. 각 lower/upper identity·원본 manifest content hash·변경 archive fullhash와 실제 CLI 결과를 연결했다. 원본은 필요한 prod 검증329까지 끝난 뒤 정상 제거/부재330, 변경소비자도309/323에서 제거했다. build402.24초는 parent scheduling 포함 상한으로 benchmark 표본으로 세지 않는다.
- [x] M1-27 기능 검증에서 빌드·기동 시간·캐시 상태·전송량·저장량과 환경을 기록하고, 같은 워크로드에 대해 HashiCorp Packer 골든 이미지 방식과 이미지 제작 시간을 포함한 end-to-end 비교를 수행한다. 아래 골든 이미지 비교 기준을 적용하며 임의 성능 합격선이나 미승인 장시간 실험은 만들지 않는다.
  - 추가 범위: 2026-11-05 사용자 제안으로 Packer 같은 사전 제작 이미지 시스템을 비교 대상으로 포함한다. 최초 제작·기존 산출물 재사용·소스 변경 후 재빌드를 구분하고, 사전 제작 비용을 숨긴 부팅 시간만으로 우열을 결론내리지 않는다. 최초 요구사항 확정은 실행 증거가 아니며, 이후 private plugin 준비·실패한 실제 HVF attempt21·복구와 현재 미완료 비교 상태는 상단 재개 기록의 개별 증거를 따른다.
  - 증거(2026-11-07, n=표본 수를 항상 병기): `M1-27-comparison-final527.json`(+보완 `…supplement528.json` clone 시간, `M1-27-failure-and-retry-cost529.json` 실패 비용, 중간 `…interim520.json`, `M1-27-packer-restart-measurement525.json`, `M1-27-native501-consumer-restart-m505-summary.json`). 같은 호스트·같은 noble 계열·같은 11 offline deb·probe 소스·성공판정(`count=3 sum=31`/변경 `sum=131`)으로 **Packer 1.15.1+qemu plugin 1.1.8(QEMU 11.1.3 HVF)** 골든 이미지와 **native SDK154(Lima VZ)**를 실제 실행했다. 모든 13개 측정 표본(Packer bake 3·consumer 4, native build 2·consumer 4)이 정확한 출력으로 통과했고 비교 표본 중 실패는 0이다. **새 산출물 비용:** Packer cold bake Popen→Packer exit 58.925/58.888s(n=2, exit 0·QEMU 부재 parent 직접 관찰), 변경 rebake 59.230s(n=1); native `plm build` 4.57s cold(SDK 3.712s, cache none)·1.625s 변경(SDK 0.819s, cache local, 3개 lower warm·2개 cold_miss) — 단 native는 이미 실행 중인 builder와 준비된 BuildKit daemon/forward 위에서 측정한 setup 제외 값이다. **기존 산출물에서 새 VM:** Packer clone 약 0.01s(APFS clonefile)+기동~workload 결과 11.565–13.044s(n=4, 상한: 모든 consumer에서 첫 SSH가 10s banner timeout 후 재시도), native `plm run` create→handoff 22.344–22.812s(n=4, 정확한 CLI wall; workload는 이 이전 완료=상한). **같은 VM 재시작:** native `plm stop` 4.703s·`plm start` 8.057s(n=1, 같은 run/instance/lease, 새 boot id, main 재실행 로그 2회, 정확한 출력); Packer는 VM 관리자가 없어 poweroff→QEMU exit 30.1–30.6s(n=4), 새 clone 기동→SSH(위), 그리고 같은 QEMU 프로세스의 guest reboot 41.9s(n=1, 상한; 옛 guest 종료 ~30s 포함, 같은 pid/birth, 새 boot id, workload 재실행 통과, expiry timer 재무장)를 기록했다. **QEMU를 같은 사용 디스크로 다시 띄우는 측정은 하지 않았다.** 전송량·저장량·cache 상태·상각 추정(손익분기 약 6VM, 단일 표본 추정이며 관측값 아님)과 환경 교란(하이퍼바이저/부팅 경로 차이, 일부 배터리 전원, native 준비구간의 custody guard)은 527에 있다. 실패 비용 529: Packer 선행 실패/중단 3회(attempt20 q338 15분 HOLD·attempt21 q381 3.5시간 retained custody와 일회성 예외 2건·attempt22 q424 27분)와 builder 사고 약2시간은 성공 경로와 분리해 기록했다. 생성 23–27·31–32 Packer, 28–30·33 native(누적 33/50). 일반 성능 우위·p90·보안 attestation을 주장하지 않는다.
- [x] M1-28 GATE를 적용하고 M1 결과·한계·차단·자원 상태를 기록한 후 M1 완료를 선언한다.
  - 증거: `M1-28-gate-application530.json`. GATE-01 충족(독립 리뷰 open P1/P2 없음; native503 한 줄 수정과 restart505 P2 수정은 부모 검증만 거쳤고 독립 재리뷰는 없다) · GATE-02 충족(Source264 694 focused/1861 expanded 통과, M1-20~26 실동작, M1-27 실제 실행; 이번 단계에서 테스트 재실행은 없음) · GATE-03 제품 소스 충족(격리 트리 `scripts/check_architecture.py` exit0 `source_sha256=1f9cf5e4…` files634, ARCHITECTURE/CHANGELOG/handoff 갱신; M1-27 동안 제품 소스 변경 없음) **단 주 저장소의 guard는 stale이며(혼합 WIP, 이 작업과 무관, 미 stamp) 아무것도 커밋·게시·배포하지 않았다** · GATE-04 충족(직접 inventory: QEMU row 10개·native consumer n15–n18 모두 Removed, 잔여 listener·disk·hold 없음; 의도 보존은 builder b01(Running, renewal416 승인·용도·만료 timer 기록)과 증거 파일뿐) · GATE-05 M1 범위 충족. **한계:** L0·L1·R1과 S1의 **로컬 재시작 절반**만 M1 근거이며 S1의 OpenStack shelve/unshelve는 M4 몫으로 M1에서 주장하지 않는다. 단일 호스트·n=1·상한 시간·서로 다른 하이퍼바이저, 보안 attestation 없음. **M1 완료 선언 범위는 이 한계 안의 로컬 Lima VZ arm64 개발환경이다.** 전역 GATE-01~05 체크박스는 모든 실행 단계 충족 시에만 표시하므로 그대로 둔다.

### M2. Hub 게시·인증·쿼터·다른 환경 재사용

의존성: M1. 완료 근거: L3, R2, H1–H4. Hub 개발·격리 검증과 실제 배포의 권한을 구분한다.

- [x] M2-01 기존 Hub API·저장소·DB·Kolla 설정·키 도메인을 검토하고 사용할 격리/배포 대상과 필요한 준비를 확정한다.
  - 증거(2026-11-07): `M2-01-hub-review-and-integration-analysis531.json`(읽기 전용 scout 2건 기반 검토), 정정 `M2-01-merge-cost-correction532.json`(잘못된 base 49b57d3 기준 "199 conflict/23 files"는 무효, 올바른 base 6e4dff7 기준 4 docs 파일 6 hunk), 소유자 결정 `M2-09-owner-quota-and-key-decisions533.json`, 병합 검증 `M2-01-merge-verification534.json`. **검토 결론:** Hub(SQLAlchemy async SQLite/MySQL, Alembic 없음)는 published-images·레이어 그래프·Manila/CephFS 바인딩·package key(ppk_v1, package/cache 전용)를 갖지만 **익명 읽기 경로 없음(현재 public=인증된 모든 사용자), 쿼터·등급·관리자 한도·물리 저장 보고·blob GC 없음, 이미지 라우트용 키 없음, CLI 게시 호출 없음(`push_image`/`publish_image` 호출처 0), 로컬 게시 상태 기록 없음, manifest platform이 linux/amd64만 허용(M1 결과는 arm64)**. 경로 간 가시성 공백 후보(legacy `is_published` 조상 가시성 우회, 동일 바이트 업로드의 private 레이어 읽기 권한)는 M2-04에서 실패 테스트 먼저로 확인한다. 이전 메모의 "기본 프로젝트 소유" 가설은 두 트리에서 재현되지 않았고 실제 Hub 확인은 `[UNVERIFIED-LIVE]`.
  - **격리 대상(확정):** 병합 worktree `/Users/pieroot/code/palimpsest-m2-merge-20261107`(branch `goal/m2-merge-20261107`, base 7962b18, 미커밋·미스테이지; 주트리 WIP + M1 병합, 소유자가 옵션 B 선택). 로컬 uvicorn/ASGI + SQLite + LocalPath CAS + 합성 Keystone(`hub/tests/test_auth.py` keystone_http), 스크러빙 env(`env -i`, 빈 HOME, `OS_*` 없음). 주트리·격리 M1 트리는 수정하지 않는다(실수로 주트리 테스트 2개를 수정했다가 정확한 역패치로 복구, 해시가 HEAD와 일치함을 확인; 534에 공개).
  - **병합 검증:** Hub lane 1825 passed/7 skipped. 루트 197 파일 per-file 비교(병합 11046 노드): Kolla 96 실패는 `ansible-playbook` 부재로 주트리·M1·병합 동일(환경 한계). M1이 물려준 테스트 결함 3건을 병합 트리에서만 수정(arm64 native 선택 기대값, `lock_deadline` stub, Darwin에서 SIGSYS로 인터프리터를 죽이는 `sys.platform` 스푸핑 테스트에 linux skipif). M1이 base 대비 스스로 제거·개명한 테스트 함수 18개 외 병합이 떨어뜨린 테스트 없음. **미검증:** Linux 전용 테스트(스푸핑 29곳, tests/kvm·integration·e2e)와 Kolla는 이 macOS 호스트에서 실행하지 못했다(hosted Linux CI 필요); `ruff format --check` 20 파일 상속 drift; architecture guard exit1(stale, 병합 소스 미검토라 stamp 안 함).
  - **배포 대상·전제(확정, 미실행):** 정규 Kolla-Ansible 롤아웃(`deploy/kolla/ansible/roles/palimpsest`)과 소유자 승인 2026-11-03의 reviewed-source → 패치 버전 → 새 불변 태그 → 정규 Kolla 배포 → 실제 API 검증 루프. 현재 전제 미충족: 검토된 소스, 병합 소스 architecture 검토·stamp, Linux CI 통과, format drift 정리, M2 구현. 해당 승인은 CI/소유권 게이트와 Ceph/IAM 자격 발급·GPU 변경·신규 유료 native 캠페인·타 소유자 자원 삭제를 제외한다. **이번 단계에서 커밋·푸시·태그·배포·실제 Hub/Keystone/Afterglow 접속은 하지 않았다.**
- [x] M2-02 로컬 DB 파일 복사 대신 이미지 manifest·레이어 산출물을 게시하는 API 계약을 구현하고 기반·부모·순서·digest·출처를 보존한다.
  - 증거(2026-11-07, 병합 worktree·격리 검증, `M2-implementation-result536.json`): `plm published push|pull|status`가 로컬 카탈로그의 manifest와 SquashFS 레이어, 선택적 원본 OCI 소스 묶음을 게시한다. manifest 영수증은 내용 기반 필드만 포함해 어느 상태 루트에서든 같은 digest를 만든다(machine identity 제거 후 테스트). 종단 간 하네스 77/77 ×2: 레이어 순서·부모 사슬·digest·크기와 복원한 OCI 레이아웃의 바이트 동일성 확인. oci-root에 한해 arm64 manifest 허용(application은 amd64), 실행 소비자(native/legacy instance/image job)는 arm64를 fail-closed. **한계:** 실제 Hub·Kolla 배포 없음, 로컬 loopback·합성 Keystone·SQLite.
- [x] M2-03 upload/push 후 Hub의 권위 있는 이미지 식별자와 결과를 확인하고, 로컬 DB의 게시 상태를 해당 결과와 연결한다.
  - 증거: request id를 첫 네트워크 호출 전에 영속화하고, Hub 응답의 id·manifest digest·이름·가시성·레이어 사슬·소유 프로젝트가 로컬 manifest와 일치할 때만 `published`로 기록한다. 4xx는 `failed`, 5xx/타임아웃/불일치는 `unknown`. 하네스: 응답 유실(프록시)→`unknown`→같은 request id 재시도로 Hub 이미지 1개, 응답 digest 변조→`unknown/response_mismatch`, 다른 자격증명/프로젝트는 낡은 `published`를 받지 않음. **미해결(P2):** 게시 전용 키 + 다른 프로젝트 행 재생 + 불확실한 Hub 결과가 겹치면 중복 Hub 이미지와 재시도되지 않는 request id가 생길 수 있다(536 `client-P2-1`, 수정 방법과 프로브 D1/D2 기록).
- [x] M2-04 private 기본값·본인 범위, internal 프로젝트 범위, public 익명 범위를 조회·획득·게시 전 경로에 적용한다. 비공개 부모의 재배포 제한도 유지한다.
  - 증거: 기본 private. 하네스 H1/H2: 익명은 public만 목록·조회·다운로드, private/internal은 manifest·blob·직접 digest URL(공개 이미지 id에 비공개 digest 조합 포함) 모두 구분 불가능한 404/401. 다른 프로젝트·다른 사용자 읽기 거부, 게시 경로의 비공개 부모 재배포 제한 유지(기존 Hub 테스트 + 신규 회귀). 후보 공백 두 건(legacy `is_published` 조상 가시성 우회, 동일 바이트 업로드의 private 레이어 읽기)은 실패 테스트 우선으로 확인했으나 **재현되지 않았고** 회귀 테스트로 남겼다. **알려진 한계:** 다른 프로젝트의 인증 사용자는 public 이미지에서 `project_id`·`storage_binding`을 볼 수 있다(익명 뷰만 숨김).
- [ ] M2-05 Afterglow OAuth 로그인 결과를 Hub 사용자·프로젝트·작업 권한으로 연결하고 정상 게시·권한 밖 거부를 실동작으로 검증한다.
  - 부분 증거만: Hub 인증은 `openid`/`oidc`/`saml2`/`mapped` 인간 인증 방식을 받아들이고 프로젝트는 토큰 스코프에서 가져온다. 합성 Keystone으로 `["openid"]` 토큰의 정상 게시·스코프 없는 토큰 거부를 확인했다. **실제 Afterglow OAuth 로그인은 실행하지 않았다(라이브 필요).**
- [x] M2-06 Palimpsest API 키와 기존 package 전용 키의 범위를 설계하고 발급·보관·만료·회수·작업 한도·사용자/프로젝트 귀속을 구현·검증한다.
  - 증거: 소유자 결정 533에 따라 별도 키 종류 `ppi_v1_`(package `ppk_v1`은 package/cache 전용으로 유지, 이미지 권한 없음). 동작 `images:read`/`images:publish`(publish는 read를 함의하지 않음), SHA-256 해시만 저장·1회 표시·최대 90일·사용자당 활성 키 상한(기본 50)·키는 키를 관리할 수 없음·사용 시점과 쓰기 직전에 현재 프로젝트 멤버십 재검증·발급자는 현재 보유한 동작만 위임(reader는 읽기 키만). 하네스: 읽기/게시 키 범위, 프로젝트 귀속, 회수·만료 키 401, 키 발급 거부, ppk_v1 키 401, 비밀 노출 스캔 0건(약 650 파일, 카나리 검출 확인). 독립 보안 리뷰 P0/P1 없음.
- [x] M2-07 익명 public manifest/blob pull과 해시 검증을 실행한다. private/internal manifest·blob·직접 digest URL의 익명 접근을 거부한다.
  - 증거: 하네스 H1: 익명 `published pull`이 SquashFS와 소스 묶음 blob 전부를 SHA-256·크기로 검증하고 OCI 레이아웃이 게시자 원본과 바이트 동일. 저장된 blob 한 바이트 변조 후 pull은 부분 파일·레이아웃·카탈로그 행 없이 거부(Hub는 저장 blob을 다운로드 시 재검증하지 않으며 검증은 클라이언트 몫). 익명 속도 제한(주소 기준, IPv6 /64 집계). **배포 주의:** HAProxy 뒤에서는 `FORWARDED_ALLOW_IPS`가 없으면 익명 호출자가 프록시당 한 버킷을 공유한다(Kolla 변수 `palimpsest_api_forwarded_allow_ips` 기본 `127.0.0.1`, 동작 보존, Kolla 테스트는 이 호스트에서 실행 불가).
- [ ] M2-08 익명 push·게시·변경을 거부한다. OAuth/API 키 각각의 허용 작업과 타 사용자·타 프로젝트·키 범위 밖 작업 거부, 회수/만료 키 거부를 검증한다.
  - 부분 증거: 익명 push/stage/publish/extend/키/관리 라우트 401, API 키·토큰 경로별 허용 작업, 타 사용자·타 프로젝트·키 범위 밖 거부, 회수·만료 키 401은 하네스로 검증했다. **OAuth 절반은 합성 `openid` 토큰뿐이라 실제 Afterglow OAuth로는 미검증이다.**
- [x] M2-09 이미지/버전/태그 개수 집계와 GB/GiB·압축/비압축·체인 중복 집계를 설계·기록한다. 사용자 확정값과 제안값을 구분하고 필요한 정책 결정을 완료한다.
  - 증거: 소유자 결정 `M2-09-owner-quota-and-key-decisions533.json`(개수 단위=고유 이름, 바이트=저장된 압축 blob, 단위 GiB, 체인 내 중복 digest는 1회, 별도 키 종류)와 `M2-15-owner-pull-run-and-source-cap-decisions535.json`(원본 OCI 소스 묶음 게시, 소스 전용 별도 관리자 한도). 제안값(유료 시드 20/50 개·50 GiB 등)과 가정(oci-root는 OCI 기반 레이어까지 모든 manifest 레이어를 센다)은 소유자 결정이 아니라고 코드·문서에 표시했다. **결정의 결과로 남는 공백:** 고유 이름 단위이므로 한 이름 아래 버전 수와 범위별 총 저장량은 제한되지 않는다.
- [x] M2-10 무료 private 사용자별 2개, internal 프로젝트별 5개, public 개수 무제한을 적용한다. 허용 마지막 값·다음 게시 거부와 다른 사용자/프로젝트의 독립 집계를 검증한다.
  - 증거: 하네스 H3(시드 정책 무변경): private 2개째 허용·3번째 새 이름 403 `image_count_limit {limit:2, used:2}`, 같은 이름의 새 digest는 허용, internal 5개 허용·6번째 403(`scope: project`), 다른 프로젝트 독립 집계, public 6개 이상 무제한, 같은 request id 재생은 쿼터 검사보다 먼저 반환. 워커 완료 경로의 쿼터 거부는 워커를 중단시키지 않고 작업을 `error`로 종료한다(리뷰가 발견한 공유 워커 중단 결함을 수정·테스트).
- [x] M2-11 원본 cloud image를 제외한 전체 레이어 체인에 무료 private/internal/public 1/5/10 GB 한도를 적용하고 경계·초과·부모 체인 포함을 검증한다.
  - 증거: 시드 정책 정확값 1073741824 / 5368709120 / 10737418240 바이트(GiB). `run_literal_boundaries.py` 11/11 ×2(+ 최종 소스 재실행 11/11, 영수증 538): 실제 크기로 한도−1과 정확히 한도는 201, 한도+1은 413 `image_chain_size_limit`(본문의 `limit_bytes`/`chain_bytes`가 기대값과 일치). 10 GiB 스테이징 31초, 피크 디스크 21.49 GB 후 정리. 축소 한도 경계에서 application-mode 기반 이미지는 제외·부모 레이어는 포함·중복 레이어는 1회 확인. **참고:** 소스 묶음 한도 경계는 축소 한도로만 검증(실제 GiB는 체인 한도에 대해서만). oci-root 이미지는 원본 OCI 기반 레이어까지 매니페스트의 모든 레이어를 센다(소유자 결정 537).
- [x] M2-12 실제 물리 저장량에는 원본 이미지·raw 변환·공개 범위 간 복사도 반영하고, 논리 한도와 구분해 관리자에게 조회시킨다.
  - 증거(2026-11-08, 소유자 결정 `M2-12-owner-physical-storage-and-seed-decisions537.json`): 소유자가 "현재 구현 유지"를 선택했다 — 물리 저장량 = Hub가 아는 저장 blob 바이트(SquashFS 레이어 + 원본 closure blob), 저장 네임스페이스별(로컬은 Hub 전체 1회, Manila는 share·scope별이라 공개 범위 간 복사는 네임스페이스별로 따로 집계) 고유 digest 합계. 관리자 물리 보고(`GET /v1/admin/image-quota/physical`)가 논리 바이트·이미지 수·고유 이름 수와 분리해 보여주고, 원본 기반 이미지 digest는 Hub가 blob을 알 때만 크기를 표시한다(아니면 null). 하네스가 독립 계산과 일치함을 확인(50 blob, 217193 바이트; `H4-physical-*` 통과, 최종 소스에서 77/77). **범위 좁힘(명시):** 항목 문구의 "cloud image raw 변환"(Glance/Cinder 쪽 용량)은 소유자가 제외했으므로 추적하지 않는다. 이후 소유자가 추가를 원하면 별도 구현·라이브 OpenStack 읽기 승인이 필요하다. 같은 결정에서 oci-root 전체 레이어(원본 OCI 기반 레이어 포함) 체인 집계와 유료 시드(private 20/50, internal 50/50, public 무제한/100 GiB)도 확정했다.
- [x] M2-13 유료는 모든 범위에서 관리자 설정으로 약 50–100 GB와 확대된 개수 한도를 적용한다. public 개수 무제한을 유지하며 결제 시스템을 임의로 추가하지 않는다.
  - 증거: 사용자 등급과 프로젝트 등급을 관리자 API로 지정하면 더 큰 개수·용량 한도가 즉시 적용된다(하네스 H4-paid-user-tier / paid-project-tier). 유료 시드(private 20/50 GiB, internal 50/50 GiB, public 무제한/100 GiB)는 소유자가 제안값 그대로 확정했고(결정 537) 관리자가 변경할 수 있다. 결제 시스템 없음. **한계:** 50–100 GB 실제 크기 업로드는 하지 않았고 정책값 반영만 검증했다.
- [x] M2-14 관리자의 한도 변경·유료 정책 적용·하향 시 기존 게시물과 작업 환경 보존/새 게시 판정 정책을 구현·검증한다. 자동 삭제로 초과를 해소하지 않는다.
  - 증거: 하네스 H4: 한도 상향은 다음 게시에 즉시 반영, 비관리자는 모든 관리 라우트 403, 사용량 아래로 하향해도 기존 이미지는 목록·다운로드 유지, 새 고유 이름과 초과 체인은 거부되고 사용량 출력이 `over_limit`을 표시, 자동 삭제 없음(작업 환경 VM을 건드리는 경로 없음). **소유자에게 보고할 결과:** 고유 이름 규칙 때문에 이미 초과한 범위도 기존 이름 아래 새 버전은 게시할 수 있다.
- [ ] M2-15 게시한 결과를 별도 상태 루트의 클라이언트로 pull하고 로컬 DB에 등록한 뒤 새 VM에서 M1 개발 작업을 재현한다.
  - 부분 증거(VM 없음): 소유자 결정 535(A)로 원본 OCI 소스 묶음을 게시한다. 별도 상태 루트로 pull해 전체 OCI 체인을 검증하고 표준 OCI 레이아웃(디렉터리 0700, 파일 0400)을 복원, `LocalLayoutSource` + `inventory.record_oci_source`로 로컬 카탈로그에 등록했다. 복원 레이아웃의 native qualification(lima-vz, ubuntu 24.04)이 게시자와 같은 플랫폼·레이어 occurrence·부팅 프로파일을 낸다. `plm run <layout>` 명령을 출력만 하고 실행하지 않았다. **미충족:** 별도 상태 루트에서 실제로 새 VM을 부팅해 M1 작업을 재현하는 단계. 병합 트리의 제품 빌드를 검증된 SDK로 다시 빌드·적격화하고 새 native 효과 승인(생성 33/50, 만료 UTC 2026-11-08T01:34:18Z)을 받아야 하며, 가드·SDK 적격화 등 사전 조건은 소실된 eval 커널과 함께 다시 세워야 한다.
- [ ] M2-16 다른 Apple Silicon 장비에서 같은 아키텍처 결과를 재현한다. 장비가 없으면 이 항목은 미완료로 두고 같은 장비 격리 증거와 구분한다.
- [ ] M2-17 GATE와 H1–H4를 모두 충족하고 실제 배포/로컬 검증 수준을 구분해 M2 완료를 기록한다.

### M3. 작업 환경 commit·ignore·재사용·삭제

의존성: M2. 완료 근거: L2, S2 및 인스턴스 식별정보 분리 계약.

- [ ] M3-01 실제 `/`의 upper 캡처와 일관성 확보 방식을 설계하고, 실행 중 캡처 허용 조건·필요한 정지와 실패 시 원본 보존을 명시한다.
- [ ] M3-02 수정 소스·설치 패키지·사용자 설정을 새 레이어로 저장하고 부모·기반·순서·실행 설정을 유지한다. RAM/프로세스 상태를 저장 대상으로 삼지 않는다.
- [ ] M3-03 .plmignore의 commit 기준 경로·재포함·build와의 차이를 명시하고, 제외 파일·외부 마운트/볼륨·임시 파일시스템의 캡처 범위를 검증한다.
- [ ] M3-04 machine-id·SSH 호스트 개인키·cloud-init 상태·주입 authorized_keys/토큰·생성 호스트명/네트워크/DHCP 상태를 캡처 사본에서 제외/초기화한다. 원본 VM은 변경하지 않는다.
- [ ] M3-05 lower에 있던 식별정보가 다시 나타나지 않도록 가림/재생성 의미를 검증한다. 사용자 ignore의 재포함이 필수 분리를 우회하지 않도록 한다.
- [ ] M3-06 로그·캐시·패키지 DB 포함 정책을 고정한다. 이미 부모 레이어에 들어간 파일을 .plmignore만으로 제거했다고 주장하지 않는다.
- [ ] M3-07 같은 commit으로 만든 두 새 VM의 수정 소스·패키지 동작을 확인하고 서로 다른 machine-id·SSH 호스트 키 및 원본 주입 자격증명 비상속을 검증한다.
- [ ] M3-08 commit을 로컬 DB·Hub에 게시하고 허용된 다른 사용자가 새 VM에서 재사용할 수 있음을 검증한다.
- [ ] M3-09 연결 종료·stop/start에서 미커밋 upper를 보존하고, 명시적 인스턴스 삭제 시 해당 upper만 제거한다. 별도 볼륨·게시 레이어·타 VM 자원은 보존한다.
- [ ] M3-10 GATE를 적용하고 commit·보존·삭제의 실제 결과를 근거로 M3 완료를 기록한다.

### M4. OpenStack 상주 서비스·대시보드·원격 SDK

의존성: M3. 완료 근거: C1–C4, S1–S3, P1. 새 유료 VM/GPU·스토리지/자격증명 준비는 별도 승인 대상이다.

- [ ] M4-01 서버 상주 서비스·worker·Hub·Afterglow의 책임과 정본 Kolla 배포 대상을 확정한다. 외부 클라이언트가 내부 관리망/SSH에 접근해야만 하는 구조로 만들지 않는다.
- [ ] M4-02 clouds.yaml 기반 원격 인증을 서비스 API 호출에 연결하고, 클라이언트 직접 OpenStack 조작 경로와 구분해 검증한다.
- [ ] M4-03 필요한 서비스·endpoint·인증·권한·네트워크 접근 검사 목록과 검사 주체를 정하고, 누락 시 구체적 실패를 반환한다. 접근 실패 상태에서 자원 생성을 진행하지 않는다.
- [ ] M4-04 대시보드에서 이미지+레이어 조합인 플레이북의 기반·순서·아키텍처·출처·공개 범위를 확인하고 선택해 VM을 생성한다.
- [ ] M4-05 Git URL·고정 리비전·Dockerfile 경로·전체 빌드 컨텍스트를 입력받아 서버에서 빌드한다. 사설 Git 인증과 source ignore·임의 내부망 URL 접근 경계도 설계한다.
- [ ] M4-06 Git 빌드의 원본 커밋·빌드 결과·게시 이미지·인스턴스 연결을 조회하고 실패·로그·종료 상태를 확인한다.
- [ ] M4-07 허용된 Docker Hub/registry 이미지 획득과 직접 Dockerfile 빌드 두 경로로 실제 루트 VM을 생성한다. 미지원 출처/OS와 아키텍처 불일치는 실행 전에 거부한다.
- [ ] M4-08 SDK와 별도 CLI에서 생성·상태·로그·SSH 정보·보존 종료·재개·명시적 삭제의 같은 서비스 계약을 실행한다.
- [ ] M4-09 OpenStack의 upper 저장 위치와 shelve/unshelve/offload 보존을 설계·검증하고, 인스턴스 완전 삭제 때 해당 upper의 제거와 게시 lower 보존을 확인한다.
- [ ] M4-10 마지막 SSH/console 세션 관측·5분 타이머·재접속 취소·다중 세션·동시 shelve 경합을 서비스 생명주기에 맞게 구현한다. 일반 관측 실패는 단절로 취급하지 않는다.
- [ ] M4-11 지연 shelve에 필요한 제한된 위임을 현재 인증 계약과 대조해 설계·승인받고, 원래 사용자 권한·만료·회수를 적용한다. 무단 서비스 자격증명 대체를 하지 않는다.
- [ ] M4-12 선택한 dev 자동 shelve·upper 보존·unshelve와 prod의 연결 해제 후 상시 실행·OOM/크래시 재시작을 배포된 API 경로에서 검증한다.
- [ ] M4-13 배포 승인이 적용되는 정확한 소스를 리뷰하고 patch 증가→새 immutable release→canonical Kolla 배포→실제 API 확인을 수행한다. 기존 태그 덮어쓰기·CI 우회·범위 밖 운영을 하지 않는다.
- [ ] M4-14 승인된 시험 자원은 한 사례 종료 후 전용 자원 부재를 확인하고 다음 사례로 넘어간다. GPU는 별도 승인·전역 한 번에 하나·예산을 지키며 불확실한 삭제를 재전송하지 않는다.
- [ ] M4-15 대시보드·외부 SDK에서 빌드→게시→생성→개발/서비스 실행→보존 재개→명시적 삭제까지 검증하고, 권한/정리 결과와 GATE를 충족해 M4 완료를 기록한다.

### M5.1. 기반 이미지 마이그레이션·구조 검사·위험 승인

의존성: M4. 자동 실행 호환성 증명이나 패키지 ABI 해결기로 확대하지 않는다.

- [ ] M51-01 기존/새 기반의 파일·종류·속성·덮어쓰기·삭제/whiteout과 레이어 접촉 경로를 비교하는 구조 검사 계약·보고 형식을 확정한다.
- [ ] M51-02 동일 OS 릴리스의 이미지 리비전 변경과 OS 릴리스 변경 모두 검사하고 원래 조합은 유지한 새 조합으로 등록한다.
- [ ] M51-03 충돌 없음이 확인된 경우만 일반 실행을 허용하고, 충돌·검사 미완료/불가 등 호환성 위험은 --yes-i-understand-the-risk 없이는 차단한다.
- [ ] M51-04 CLI·Hub API·대시보드의 명시적 승인 의미를 맞추고, 일반 --force/확인 옵션이 대신하거나 인증·소유권·무결성·쿼터/지원 OS를 우회하지 못함을 검증한다.
- [ ] M51-05 구조 검사의 한계·패키지 DB 충돌·실행 검증의 사용자 책임을 표시하고, 승인된 새 조합 생성과 원래 조합 재사용을 확인한 뒤 GATE를 적용한다.

### M5.2. devcontainer.json 설정 지원

의존성: M5.1. Docker CLI/API 호환이나 브라우저 IDE를 암묵적으로 추가하지 않는다.

- [ ] M52-01 VM에 대응 가능한 필드·경로 기준·명령 실행 시점·Palimpsest 설정 우선순위를 조사해 지원 부분집합과 미지원 처리 계약을 정한다.
- [ ] M52-02 Dockerfile/context·사용자·작업 디렉터리·환경·포트·선택 볼륨 등 확정된 필드만 VM 설정으로 변환한다. 지원하지 않는 필드를 조용히 성공 처리하지 않는다.
- [ ] M52-03 실제 설정 파일로 build→새 VM→SSH/console 개발 작업을 수행하고 .plmignore·dev/prod·데이터 보존과 함께 동작함을 검증한다.
- [ ] M52-04 지원/비지원 예시와 실제 오류·한계를 문서화하고 GATE를 적용한다.

### M5.3. Apple Silicon에서 linux/amd64 교차 빌드

의존성: M5.2. 다른 CPU에서 같은 바이너리를 네이티브 실행한다고 주장하지 않는다.

- [ ] M53-01 에뮬레이션 또는 허용된 원격 builder 등 실행 방식을 비교해 선택하고 추가 자원·다운로드·아키텍처 제약과 승인을 기록한다.
- [ ] M53-02 Apple Silicon에서 amd64 대상을 명시해 소스 컴파일을 포함한 빌드를 수행하고 결과의 아키텍처·digest·기반·체인을 기록한다.
- [ ] M53-03 amd64 산출물을 Hub에 게시하고 ARM 산출물과 잘못 바인딩되지 않도록 선택·거부를 검증한다.
- [ ] M53-04 승인된 amd64 VM에서 결과를 실행한다. Mac 에뮬레이션·원격 amd64·네이티브 arm64 결과를 구분하고 증거가 없는 환경의 성공을 주장하지 않은 채 GATE를 적용한다.

### M5.4. Hub 자체 Git 저장소

의존성: M5.3. 외부 Git 링크 빌드와 별도 기능이다.

- [ ] M54-01 기존 Git 서비스 통합/직접 제공 방식을 조사하고 소유권·가시성·인증·한도·보관/삭제·백업·빌드 권한의 계약을 확정한다.
- [ ] M54-02 사용자가 소스를 push하고 허용된 사용자/프로젝트가 조회·clone하도록 구현한다. 이미지 public과 Git 소스 공개를 자동으로 동일시하지 않는다.
- [ ] M54-03 저장된 특정 커밋에서 Dockerfile+소스를 빌드하고 commit→빌드→이미지 출처를 보존한다. 접근 권한 없는 소스의 빌드를 거부한다.
- [ ] M54-04 사용자 source push→Hub build→새 VM 재현과 허용된 삭제/복구 범위를 검증하고 GATE를 적용한다.

### M5.5. 자유로운 패키지 레이어 조합

의존성: M5.4. 기본 부모 체인을 깨뜨리는 임의 혼합을 무조건 허용하지 않는다.

- [ ] M55-01 OS/아키텍처·부모·패키지/파일 충돌·레이어 순서·공개 범위와 조합 허용 조건을 설계하고, 해결 불가능한 조합의 거부/위험 승인 범위를 구분한다.
- [ ] M55-02 사용자가 레이어 후보와 순서를 선택하고 검증 결과를 확인해 새 불변 조합을 생성하도록 제공한다.
- [ ] M55-03 허용된 조합의 실제 VM 실행과 파일·패키지/순서 충돌 및 지원 범위 밖 조합의 거부를 검증한다. 위험 승인을 보안 우회로 사용하지 않는다.
- [ ] M55-04 기본 Ubuntu→Python→Torch→vLLM 같은 부모 체인 경로가 유지되고 새 조합의 출처·쿼터·공유 권한이 적용됨을 검증한 뒤 GATE를 적용한다.

### GATE. 각 단계의 공통 종료 조건

이 목록은 각 단계에서 반복 적용한다. 단계별 완료 ID의 증거에 적용 결과를 기록하고, 아래 체크는 모든 실행 대상 단계에서 충족됐을 때만 표시한다.

- [ ] GATE-01 영향 범위 소스·호출자·계약을 검토하고 기존 경로를 재사용했으며 독립 리뷰의 미해결 차단 사항이 없다.
- [ ] GATE-02 관련 테스트/검사와 실제 변경 경로 smoke를 실행하고 정확한 소스·명령·결과·실패·한계를 기록했다. 테스트만으로 실동작을 대신하지 않았다.
- [ ] GATE-03 source 변경 후 아키텍처·계약·해당 문서·변경 기록·인계를 갱신하고 검토한 소스에만 필요한 marker/guard를 적용했다.
- [ ] GATE-04 실제 생성 자원의 소유권·삭제/의도된 보존과 잔여 상태를 확인했다. 보존이 필요한 자원은 현재 승인과 용도를 기록했으며 미확인 정리 실패를 숨기지 않았다.
- [ ] GATE-05 단계의 모든 필수 항목·완료 판정 ID를 충족하고, 실행 기록의 다음 작업·차단·증거를 갱신했다.

### FINAL. 최종 goal 완료

- [ ] FINAL-01 실행 목표 범위의 모든 체크 항목에 유효한 근거가 있고 차단·부분 구현·다른 소스의 과거 증거를 완료로 세지 않았음을 확인한다.
- [ ] FINAL-02 기본 제품 M1–M4의 실제 사용자 여정과 L0–L3, C1–C4, H1–H4, R1–R2, S1–S3, P1의 증거를 연결한다.
- [ ] FINAL-03 전체 로드맵 실행 목표라면 M5.1–M5.5를 승인된 순서로 완료하고 확장 기능의 지원 범위·한계를 기록한다.
- [ ] FINAL-04 현재 소스·릴리스·배포된 버전과 검증 환경을 구분하고 문서·인계·운영/개발 사용법을 맞춘다. 실행하지 않은 배포·교차 장비·GPU 성공을 주장하지 않는다.
- [ ] FINAL-05 자원·권한·잔여 이슈·증거 경로와 최종 결과를 보고한다. 승인 없이 목표를 축소하거나 체크박스를 채우기 위해 미완료 항목을 제거하지 않는다.

## 인터뷰 결정 기록

### 확정된 제품 목표

#### 로컬 개발자 클라이언트

- Docker 사용자에게 익숙한 별도 Palimpsest CLI를 제공한다. 실제 docker CLI나 Docker API에서 직접 조작할 필요는 없다.
- 짧은 명령 plm과 긴 명령 palimpsest를 모두 제공하며 같은 기능을 실행한다. 현재 pyproject.toml에는 palimpsest 진입점만 있으므로 plm 추가는 구현 격차다.
- 컨테이너 대신 VM 기반 실행환경·개발환경을 제공한다. VS Code Dev Containers, Ona, GitHub Codespaces와 같은 개발환경 경험이 목표의 예시다.
- CLI 터미널과 SSH 접속을 필수로 제공한다. devcontainer.json을 통한 설정 지원은 원하는 확장 기능이며, 지원 필드·호환 범위와 완료 단계는 미정이다. 브라우저 IDE는 필수로 확정하지 않았다.
- VM에서 수행한 작업을 다시 레이어로 저장하고, 나중에 자신이 재사용하거나 다른 사용자에게 공유할 수 있어야 한다.
- 저장·복원 대상은 개발·실행 환경이다. 실행 중 프로세스와 RAM을 복원하는 기능은 목표가 아니다.
- 첫 완료 목표는 작업한 VM의 저장이 아니라, 로컬 Dockerfile과 필요한 소스로 빌드한 환경을 새 VM에서 재현하는 것이다.
- 로컬 작업의 레이어 메타데이터를 SQLite 같은 로컬 DB에 기록한다. SQLite 자체가 필수인지 다른 로컬 DB도 허용하는지는 미정이다.
- 로컬 DB 파일 자체가 아니라 메타데이터와 레이어 산출물을 Hub에 게시하고, 다른 환경에서 사용할 수 있어야 한다. 레이어 압축·저장에는 SquashFS를 우선 후보로 검토한다. 메타데이터의 전송 형식과 묶음 형식은 후속 설계로 정한다.
- 로컬 개발자 사용은 명시적인 제품 목표다. 원문 초안 2-2도 이 결정에 맞춰 수정했다.

#### 클라우드와 OpenStack 서비스

- 클라우드 VM 배포는 별도의 명령 체계 또는 SDK를 통해 수행할 수 있어야 한다.
- OpenStack에는 해당 기능을 제공하는 상주 서비스가 있어야 한다.
- OpenStack 대시보드에서 기존 “플레이북”을 선택해 VM을 생성할 수 있어야 한다. 여기서 플레이북은 이미지와 레이어의 조합을 뜻하며, Ansible 플레이북과 동일한 형식이라는 의미는 아니다.
- 사용자가 GitHub 또는 Git 저장소 링크를 제공하면 해당 저장소를 기반으로 빌드하고 실행할 수 있어야 한다.
- Docker Hub의 기존 이미지를 가져오는 경로와 직접 빌드하는 경로를 모두 지원해야 한다.
- 원격 클라이언트는 clouds.yaml 같은 인증 설정을 사용해 해당 서비스에 실행을 요청할 수 있어야 한다.
- 원격 실행에 필요한 서비스에 접근 가능한지 확인하는 로직이 필요하다. 검사 주체·필수 서비스 목록·인증/권한 검사 범위·실패 처리 방식은 후속 인터뷰로 확정한다.

#### 소스 입력과 빌드

- Dockerfile과 빌드에 필요한 소스를 빌드 컨텍스트로 제공할 수 있어야 한다. 소스 컴파일이 필요한 이미지에 Dockerfile만 전달하는 것으로 완료를 판정하지 않는다.
- 로컬 소스베이스와 Git 저장소 링크를 지원 대상으로 한다. Dockerfile만으로 빌드가 가능한 경우는 소스 파일 없이도 허용한다.
- Hub가 제공하는 Git 저장소에 소스를 올리고 그 소스로 빌드하는 기능은 검토 대상이다. 필수 완료 조건으로 확정하지 않았다.
- Dockerfile 없는 저장소의 자동 감지·빌드는 요구사항으로 확정하지 않았다.
- 소스는 기본적으로 빌드 컨텍스트에서 레이어에 포함해 VM에 제공한다. 호스트 소스를 VM에 직접 마운트하는 것은 Docker volume처럼 사용자가 선택하는 기능이며 기본 전제는 아니다.

#### 이미지·레이어와 VM 루트 구성

- 이미지 중심으로 실행한다. Palimpsest의 원본 기반 이미지는 cloud image로 관리하고, 그 위에 레이어를 적용한다.
- 실행 호스트에 레이어를 미리 저장하거나 마운트해 재사용하고, VM 내부에서는 레이어들을 OverlayFS lowerdir로 구성해 실제 `/`로 사용해야 한다. 단순히 일반 VM의 특정 디렉터리에 레이어를 붙이는 것으로 완료를 판정하지 않는다.
- 호스트 저장·마운트에서 게스트 lower까지 전달하는 구체적인 장치·공유 방식은 환경별로 설계한다. 호스트 마운트 경로가 게스트에서 그대로 접근 가능하다고 가정하지 않는다.
- 재사용할 레이어는 읽기 전용으로 유지하고 VM별 upperdir을 분리한다. 보존 종료·재시작에서는 upperdir을 유지하고, 인스턴스를 완전히 삭제하면 해당 인스턴스의 미커밋 upperdir도 삭제한다.
- 같은 CPU 아키텍처 사이의 재사용이면 필수 요구를 충족한다. Apple Silicon에서 만든 결과는 다른 Apple Silicon에서 재사용할 수 있어야 한다. CPU 일치만으로 OS·커널·장치 호환을 보장하지 않는다.
- Apple Silicon에서 linux/amd64 대상으로 교차 빌드하고 실행하거나 Hub에 게시하는 기능은 희망 확장이다. 필수 교차 아키텍처 바이너리 실행으로 간주하지 않는다.
- 기본 레이어 모델은 기반·부모·순서에 종속된 체인이다. 예: Ubuntu 24.04 → Python 3.11 → Torch → vLLM. 사용자가 패키지 레이어를 자유롭게 골라 조합하는 기능은 충돌 위험이 큰 희망 확장이며 기본 완료 조건이 아니다.
- 같은 OS의 새로운 기반 이미지로 기존 레이어를 옮기는 기능은 마이그레이션 지원이며, 동작하지 않을 가능성을 전제로 한다. 우선 구조 검사를 제공하고 실행 호환성을 보장하지 않는다.
- 동일 OS 릴리스의 이미지 리비전 갱신(사용자 예: 22.04(10.2) → 22.04(10.3))에는 구조 검사로 충분하다. OS 릴리스 변경(예: 22.04 → 24.04)도 금지하지 않고 마이그레이션을 지원한다. 예시 괄호 숫자의 공급자별 의미는 가정하지 않는다.
- 실행 검증은 사용자·개발자의 책임이며 제품이 자동 실행 검증을 강제하지 않는다. 구조 검사에서 충돌 없음이 확인된 경우에만 위험 승인 없이 실행할 수 있다. 그 외 호환성 위험은 기본 차단하고 --yes-i-understand-the-risk로 명시적으로 수락해야 진행한다. 이 규칙은 CLI와 Hub에 동일하게 적용하며 단순 --force나 일반 확인 옵션으로 대체하지 않는다. 검사 불가·미완료를 무충돌로 취급하지 않는다.
- 위험 승인 옵션은 호환성 위험 수락에만 사용한다. 인증·소유권·무결성·자원 한도 검사를 우회하는 수단으로 사용하지 않는다. Hub/API에서도 같은 승인 의도를 검증해야 하며 CLI 표시만으로 대체하지 않는다.
- SquashFS 레이어와 별도로 기반 이미지 식별자, 아키텍처, 레이어 식별자·순서·의존성 등을 나타내는 manifest가 필요하다. 필수 필드와 기존 형식 재사용 범위는 소스 검토 후 정한다.

#### 외부 이미지 출처와 지원 OS 판정

- 관리자가 지원 가능한 이미지를 Docker Hub 또는 레지스트리의 정규화된 이미지 이름으로 등록할 수 있다. 예: ubuntu, nvidia/cuda. 레지스트리 호스트·namespace·repository를 구분해 동명이미지를 자동 신뢰하지 않는다.
- 식별 가능한 게시자 정보를 정책 조건으로 사용할 수 있다. 레지스트리에서 확인한 출처·게시자와 이미지 내부 자체 선언을 구분한다. 게시자 검증을 지원하지 않는 레지스트리의 동작은 별도 명시한다.
- 이미지 이름·허용 출처 정책으로 지원 대상을 판단하고, 실제 획득한 결과는 digest와 아키텍처로 고정·기록한다. 모든 외부 이미지에서 원래 FROM의 완전한 계보 증명을 필수로 요구하는 앞선 초안은 이 정책으로 대체한다.
- os-release는 OS 계열·버전 분류와 부팅 시스템 선택에 사용한다. 같은 계열·버전이라는 사실을 cloud image와 바이트가 같거나 실행 호환성이 보장된다는 뜻으로 해석하지 않는다.
- 허용 출처가 아니거나 OS 계열·버전을 확인할 수 없거나 지원하는 부팅 시스템이 없으면 실행을 차단한다. 이 검사는 호환성 위험 승인 옵션으로 우회하지 않는다.
- Docker 이미지를 가져오기 전에 manifest/config를 검사할 수 있으나 OS 확인에는 rootfs 획득이 필요할 수 있다. 다운로드 성공을 실행 허용으로 간주하지 않는다.
- Palimpsest 빌드·게시·commit은 확인 가능한 기반·부모·순서·출처를 유지한다. 확인하지 못한 외부 FROM 계보를 추측해서 생성하지 않는다.
- 같은 Ubuntu 버전이라도 OCI 기반 rootfs와 cloud image의 바이트·패키지 구성은 같지 않다. OCI 기반을 생략하고 그 위 레이어를 cloud image에 바로 붙이면 패키지 DB 같은 전체 파일 교체로 기반 상태가 불일치할 수 있다.
- 따라서 Docker 이미지의 기반→cloud image 연결은 미해결 설계 항목이다. 원래 OCI rootfs를 유지하는 방식과 cloud image에서 빌드를 재수행하는 방식 등을 검토하고, 지원 OS 판정만으로 서로 다른 기반을 안전하게 접합했다고 판정하지 않는다. 기존 전체 rootfs 포장도 이 문제의 해결 증거로 삼지 않는다.
- 지원 OS 목록은 Docker Hub 입력 범위를 직접 제한한다. Ubuntu만 승인했다면 Debian·Alpine 등 다른 기반은 이름이 익숙한 언어 이미지라도 자동 허용하지 않는다. 개별 태그의 실제 기반·아키텍처는 digest별로 확인한다.

#### 실행 모드와 주 프로세스

- dev와 prod 실행 모드를 제공하며 기본값은 dev다. 작업 환경의 데이터 보존과 VM이 계속 실행되는 상태를 구분한다.
- dev는 개발할 때 시작하고 SSH 또는 console 연결 종료를 기준으로 사용 종료를 판단한다. 꺼져도 upper와 작업 환경은 명시적 삭제 전까지 유지한다. 재접속 유예와 다중 세션은 아래 보존 정책을 따른다.
- prod는 연결 해제와 관계없이 상시 동작한다. Entrypoint/CMD 또는 명시한 실행 명령을 주 작업으로 실행하고 실제 프로세스 실행 상태를 확인한다. VM ACTIVE 상태만으로 주 작업 정상 동작을 판정하지 않는다.
- prod는 내부 애플리케이션 OOM·크래시·주 프로세스 종료 때문에 VM을 down/shelve/delete하지 않고 작업 상태를 보존한다. restart 옵션으로 주 프로세스 자동 재실행을 제공하며 기본값은 unless-stopped다. dev 기본값은 no다.
- unless-stopped는 사용자 명시적 중단 전까지 정상·오류 종료 후 주 작업을 재실행하는 의미다. 반복 실패의 지연·상태·종료 코드·로그를 제공하도록 설계한다. 재실행 시도를 보장하는 것이며 프로그램의 정상 실행이나 물리 호스트·게스트 커널의 무고장을 보장한다고 표시하지 않는다.
- prod의 VM 유지와 주 작업 restart는 별도 계약이다. restart=no로 주 작업이 정지돼도 prod VM을 임의로 끄지 않는다. 명시적 stop/shelve/delete 뒤에는 restart 정책으로 자동 기동하지 않는다.
- dev에서 Entrypoint/CMD가 없거나 실행을 비활성화한 경우 상시 주 작업은 필요하지 않다. prod에서 명령이 없는 경우 VM만 유지할지 admission을 거부할지는 후속 설계로 명시한다.


#### VM과 작업 데이터 수명주기

- 작업 환경은 사용자가 명시적으로 제거하지 않는 한 유지한다. 로컬과 클라우드 모두 터미널 종료·SSH 연결 해제·stop만으로 VM 작업 데이터나 upperdir을 삭제하지 않는다.
- 보존 종료 후 다시 시작하면 같은 upperdir의 작업 환경을 사용할 수 있어야 한다. OpenStack에서는 이 의미를 shelve/unshelve로 제공한다. RAM·프로세스 복원은 포함하지 않는다.
- 사용자가 인스턴스를 명시적으로 완전히 삭제하면 미커밋 upperdir도 함께 사라진다. commit으로 별도 저장한 레이어와 공유된 lower는 해당 인스턴스의 임시 쓰기 영역과 구분한다.
- SSH/console 단절 자동 보존 종료는 dev에만 적용하고 prod에는 적용하지 않는다. 마지막 세션 단절 후 5분간 재접속이 없으면 로컬에서는 보존 정지, OpenStack에서는 upperdir을 보존하는 shelve를 수행한다. 유예 중 재접속하면 취소하며, 다른 세션이 남아 있으면 타이머를 시작하지 않는다. 클라우드의 자동 shelve 활성화는 앞서 정한 사용자 선택을 유지한다.
- 자동 처리는 완전 삭제가 아니다. 일반 인증·관측 장애를 세션 단절로 임의 판정하지 않는다. 세션 관측과 재접속·shelve 경합의 구체적인 구현은 아래 설계 조건을 따른다.

자동 처리 구현에 앞서 다음 설계 조건을 해결해야 한다.

- 지연 작업 권한: `ARCHITECTURE.md:66-69`의 현재 native 작업은 원래 사용자의 새로 검증된 인증을 요구하며 토큰을 저장하거나 서비스 자격증명으로 실행을 대체하지 않는다. SSH 단절 5분 뒤의 작업에도 유효한 권한이 필요하다. 제한된 위임 등의 설계와 명시적 승인 없이 서비스 권한으로 대신 실행하지 않는다.
- 세션 관측: `cloud_env.py:13-15`의 기존 소비자 경로는 게스트에서 Palimpsest 코드를 실행하지 않는다. 직접 SSH까지 관측하려면 게스트 측 관측 구성 또는 통제되는 SSH 프록시 등 별도 설계가 필요하다. CLI 연결 상태만으로 모든 SSH 세션이 끊겼다고 판정하지 않는다.
- upper 저장 위치: shelve/unshelve와 shelve-offload에서 upper가 유지되는 저장 장치를 명시해야 한다. 부팅·별도 Cinder 볼륨 또는 다른 저장 방식을 선택하고, 실제 보존 동작과 인스턴스 삭제 시 전용 upper 저장소의 제거를 검증한다. 이미지 기반 루트와 볼륨 기반 루트의 shelve 동작이 같다고 가정하지 않는다. 공유 레이어 저장소는 삭제하지 않는다.

#### commit의 인스턴스 식별정보 분리

- 실제 `/`의 쓰기 영역을 공유 레이어로 저장할 때, 원래 VM의 머신 식별정보와 접속 자격증명을 복제하지 않아야 한다. 이는 사용자 소스 포함 범위와 별도의 안전 조건이다.
- 기본 제외·초기화 대상에는 machine-id, SSH 호스트 개인키, cloud-init의 인스턴스별 상태, 주입한 authorized_keys·토큰, 생성된 호스트명·네트워크 설정·DHCP 임대 상태를 포함한다. 새 VM에서는 새 식별정보와 해당 VM의 접속·네트워크 설정을 생성한다.
- 영속 사용자 설정과 런타임이 생성한 설정을 구별하고, 운영 중인 원본 VM의 식별정보를 삭제하지 않는다. 캡처 사본에서 처리하며 구체적인 파일 목록은 지원 OS별로 검토한다.
- lower에 이미 들어 있는 식별정보도 새 VM에 노출되지 않도록 초기화·가림 의미를 설계한다. upper의 파일을 생략하는 것만으로 충분하다고 가정하지 않는다.
- 로그·임시 파일·재생성 가능한 캐시의 기본 포함 정책은 별도 설계한다. 패키지 DB를 캐시로 오인해 삭제하지 않는다. 사용자가 직접 저장한 모든 비밀정보를 자동 탐지·제거한다는 보장은 하지 않는다.
- 완료 검증에는 같은 commit으로 만든 두 새 VM의 machine-id·SSH 호스트 키가 서로 다르고, 원래 VM의 주입 자격증명·인스턴스별 네트워크 상태를 상속하지 않는지 확인하는 시나리오를 포함한다.

#### 사용자 지정 제외 규칙

- gitignore와 같은 별도 ignore 메커니즘을 제공해 사용자가 저장·게시에서 제외할 파일을 지정할 수 있어야 한다.
- 파일명은 .plmignore로 확정한다. build 컨텍스트와 commit에서의 적용 범위, gitignore 문법 지원 수준, 기존 .dockerignore와의 우선순위는 후속 설계로 확정한다.
- 사용자 ignore 규칙은 머신 식별정보·주입 자격증명 분리와 별개다. 사용자 재포함 규칙으로 필수 식별정보 분리를 무효화하지 않는 것을 설계 기본안으로 삼는다.
- ignore가 최상위 변경분에서 파일을 생략하더라도 이전 lower에 포함된 파일·비밀정보가 제거되는 것은 아니다. 게시 대상 체인 전체와 제외 규칙의 한계를 사용자에게 표시해야 한다.

#### Hub 공개 범위와 개수 제한

- 기본 공개 범위는 private이다. 한도 정책은 언제든 변경 가능한 설정으로 관리한다.

| 공개 범위 | 무료 개수 제한 | 무료 이미지별 레이어 총용량 한도 — 원본 cloud image 제외 |
|---|---|---|
| private | 사용자별 2개 | 약 1 GB |
| internal | 프로젝트별 5개 | 약 5 GB |
| public | 개수 제한 없음 | 약 10 GB |

- 용량은 개별 레이어가 아니라 이미지가 사용하는 레이어 전체의 합계에 적용하며, 원본 cloud image 용량은 제외한다. private 무료 이미지 한도는 약 1 GB, internal은 약 5 GB, public은 약 10 GB다. 부모 애플리케이션 레이어도 체인 합계 대상이며 새로 추가한 변경분만 집계하지 않는다.
- 유료는 private/internal/public 모두 용량 제한을 크게 완화해 약 50–100 GB 수준을 지원하고, private/internal 개수 제한도 무료보다 확대한다. public 개수 무제한 정책은 유지한다. 유료의 정확한 값은 관리자가 실제 스토리지 용량을 고려해 설정한다.
- 무료·유료의 용량·개수 한도는 운영자가 언제든 변경할 수 있는 정책으로 관리하고 소스 상수로 고정하지 않는다. 실제 과금·결제 시스템 구현을 이 요구로부터 추정하지 않는다.
- 원본 cloud image의 물리 용량·raw 변환 후 용량은 위 이미지별 레이어 한도에서 제외하되 실제 스토리지 사용량에서는 계속 추적한다. 용량 한도에서 제외된다고 기반 이미지 복사·배포 비용이 사라지는 것은 아니다.
- 현재 `ARCHITECTURE.md:55-59`는 공개 범위별 namespace에 완전한 이미지를 두고 범위 간 게시 시 바이트를 복사한다고 설명한다. 논리적인 이미지별 레이어 한도와 범위별 실제 물리 사용량을 구분한다. 한도 정책을 맞추려고 공개 범위 간 저장 격리를 임의로 제거하지 않는다.
- 같은 물리 레이어를 이미지 체인에서 중복 참조할 때의 합산 규칙, 개수의 이미지/버전/태그 단위, 압축/비압축 바이트 및 GB/GiB 단위는 후속 설계로 고정한다. 관리자는 실제 저장소 사용량을 조회하고 정책을 조정할 수 있어야 한다.
- public 개수 무제한은 저장 바이트·업로드 크기·빌드 자원까지 무제한이라는 뜻이 아니다. public 이미지의 pull은 익명으로 허용한다. private/internal은 익명으로 조회·획득할 수 없으며 해당 권한을 요구한다.
- 한도 변경은 기존 작업 환경의 자동 삭제 승인이 아니다. 한도 하향 시 기존 게시물 보존과 새 게시 차단·초과 해소 정책은 후속 확정한다.

#### 로그인·API 키와 익명 배포

- push와 게시·변경 작업은 인증된 사용자만 수행한다. 인증 성공과 프로젝트·공개 범위·작업 권한 확인을 구분한다.
- Afterglow에서는 OAuth 로그인 또는 사용자가 발급한 Palimpsest API 키로 인증할 수 있어야 한다. OAuth 로그인 결과가 Hub의 사용자·프로젝트 권한으로 연결되는 경로를 명시한다.
- API 키는 사용자·프로젝트 및 허용 작업 범위를 식별하고 발급·회수·만료·비밀값 보관 정책을 제공하도록 설계한다. 키의 유출을 막기 위해 비밀값을 이미지·레이어·일반 로그에 포함하지 않는다.
- 기존 README의 ppk_v1 패키지 키는 package/cache 전용 권한 도메인이다. 이 키가 전체 이미지·빌드·VM 실행을 이미 지원한다고 판정하거나 사람의 관리 권한으로 승격하지 않는다. 기존 키의 재사용 가능 범위와 필요한 키 종류는 소스 검토 후 결정한다.
- 현재 아키텍처의 public은 인증된 사용자 대상으로 설명돼 있으므로 익명 pull은 권한 계약 변경이다. 공개 이미지의 manifest·필요한 blob 다운로드 경로만 허용하고, 비공개 데이터·스토리지 자격증명·관리 API는 노출하지 않는다. private/internal 부모의 공개 재배포 권한도 별도로 유지한다.

#### 성능 목표와 계측

- 반드시 달성해야 하는 기동 시간·빌드 시간·저장량 절감 수치 목표는 없다. 위 용량 쿼터는 성능 목표가 아니라 제품 정책이다.
- 초기 완료는 기능 정확성·실제 사용자 경로·데이터 보존·격리·권한으로 판정한다. 빌드 시간, 최초/캐시 재사용 기동 시간, 실제 저장량과 전송량은 조건과 함께 측정·기록하되 합의하지 않은 임계값으로 실패시키지 않는다.
- 측정을 위해 별도 장시간 벤치마크나 유료 자원 캠페인을 자동 시작하지 않는다. 승인된 기능 검증에서 얻은 관측과 별도 성능 실험을 구분한다.

##### 골든 이미지 비교 — 2026-11-05 추가 결정 / M1-27

- 비교 대상은 Palimpsest의 OCI 빌드·레이어 제작/재사용 경로와 **HashiCorp Packer로 골든 디스크 이미지를 제작하고 그 이미지로 VM을 만드는 경로**다. 여기서 Packer는 현재 SquashFS packing 도구(`mksquashfs`)와 다르다. Packer가 해당 ARM64/하이퍼바이저에서 실제 지원되는 builder/plugin을 조사·고정하며, 다른 도구로 대체하면 실제 도구와 이유를 명시하고 Packer를 실행한 것처럼 보고하지 않는다.
- 동일한 호스트·CPU/RAM·대상 아키텍처·OS 계열/릴리스·패키지 버전·소스 커밋·워크로드와 성공 판정을 맞춘다. 최종 VM의 하이퍼바이저·디스크 형식·커널·네트워크·스토리지 조건도 가능하면 맞추며, 맞출 수 없는 차이는 표에 남긴다. 다른 조건의 결과를 레이어 방식 자체의 인과적 우위로 해석하지 않는다.
- **최초 cold 경로:** 필요한 입력 획득·builder 준비 → 애플리케이션/이미지 빌드 → 레이어 packing 또는 골든 이미지 provisioning·일반화/sealing → 결과 변환·저장/등록·전송 → consumer VM 생성 → 최초 SSH 준비 → 같은 워크로드의 실제 준비 완료를 측정한다. 사전에 구운 골든 이미지를 시작점으로 놓고 제작 시간을 누락하지 않는다.
- **기존 결과 재사용 경로:** 이미 제작한 같은 산출물로 새 consumer를 만들 때의 복제/전송·새 upper/차분 디스크 생성·부팅/준비 비용을 비교한다. 같은 VM의 stop/start 시간은 새 VM 생성과 별도로 기록한다. 기존 이미지 빌드 시간을 매번 다시 더하거나, 반대로 최초 총비용에서 빼지 않는다.
- **소스 변경 경로:** 동일한 변경을 적용한 뒤 Palimpsest의 증분/캐시 빌드와 Packer의 새 골든 이미지 제작을 각각 끝내고 새 VM에서 변경 결과를 확인한다. 재사용 가능한 기반·패키지/다운로드 캐시는 허용하되 각 방식의 실제 cache hit/miss와 재제작 범위를 기록한다.
- 최초 준비에 필요한 도구/plugin 설치·검증, 기반 이미지와 Palimpsest 플랫폼/kernel/bootstrap 자산 준비 비용을 양쪽 모두 별도 항목으로 표시한다. 해당 실행에 필수인 경우 cold 총시간에 포함하며, 이미 준비된 환경의 관측은 setup 제외 결과로 분명히 구분한다. 개발 중 디버깅·재시도 시간은 정상 성공 경로와 분리하되 실패 시간/비용을 버리지 않는다.
- 각 단계의 UTC 시작/종료 시각과 monotonic 경과시간, 최종 end-to-end wall time을 함께 남긴다. 병렬 단계의 시간을 단순 합산해 총시간을 부풀리지 않는다. SSH 준비와 애플리케이션 준비를 따로 기록하고 VM `Running`만으로 준비 완료로 보지 않는다.
- 원시 증거에는 source ref, Packer/plugin·빌드 도구 버전과 템플릿, 실제 명령·종료 코드, 기반/산출물 digest, cache 상태, 빌드·등록/전송·VM 생성·준비 단계 시간, 전송 bytes, 논리/실제 할당 저장량, 자원 ID와 정리 결과를 연결한다. 공개 입력 전송 포함/제외 조건을 양쪽에서 맞추거나 차이를 명시하고, 사용자/타 작업 캐시를 지워 cold 조건을 만들지 않는다.
- 실제 관측 표본 수 `n`과 실패·미측정 항목을 그대로 표시한다. 관측하지 않은 시간을 `0`으로 채우거나 한 번의 관측으로 p90·일반적 성능 우위를 주장하지 않는다. 공통 비교 구간의 관측값으로 `N`개 VM에 대한 **최초 제작+재사용 총비용 및 VM당 상각 비용**을 함께 제시한다. 상각/손익분기 계산은 추정이며 실제 `N`회 반복 결과와 구분한다.
- 실행 순서는 현재 native 정리·기능 정확성 확보 → 비교 가능한 recipe/지원 환경·자원 계획 확인 → 승인 범위 안의 한정 기능 검증·측정이다. Packer가 내부적으로 만드는 임시 builder VM도 생성 시도·동시/보유 VM·CPU/RAM·디스크·TTL·다운로드 예산과 정리 증명에 포함한다. 이 비교 제안은 기존 G0 한도, 새 하이퍼바이저/호스트 설치·유료 자원 승인, 불확실한 정리 차단을 자동 확대하지 않는다.
- 비교 경로가 미지원이거나 남은 승인/예산으로 실행할 수 없으면 필요한 정확한 전제와 차단 사유를 기록하고 **M1-27은 미완료**로 둔다. 방법론 문서 작성이나 기존 다른 환경의 벤치마크로 실제 비교를 대체하지 않는다.





#### 확정된 우선순위와 단계 의존성

1. 로컬 Dockerfile과 필요한 소스를 빌드하고, 그 결과로 새 VM에서 개발환경을 재현한다. 레이어 메타데이터를 로컬 DB에 기록한다.
2. Hub 게시에 앞서 Hub의 필요한 설정과 게시·조회 경로를 준비한다. 기존 Hub를 처음부터 다시 구현한다는 의미는 아니다.
3. 로컬 산출물과 메타데이터를 Hub에 게시하고, 다른 환경에서 받아 VM 개발환경을 재현할 수 있는지 검증한다.
4. 작업 중 VM의 환경을 레이어로 저장하는 기능과 OpenStack 대시보드·Git 기반 클라우드 흐름도 전체 목표에 포함된다. 이들 사이의 세부 실행 순서는 아직 확정하지 않았다.

### 에이전트 loop/goal 실행 계약 초안

상태: M1을 Apple Silicon/Lima VZ arm64부터 검증하는 순서는 사용자 확인을 받았다. 아래는 전체 목표를 단계별로 수행하기 위한 계약 초안이며, 계획 순서 확인을 구현 루프 시작·자원 생성 승인으로 확대하지 않는다.

#### 단계별 목표와 종료 증거

| 단계 | 범위 | 통과 증거 |
|---|---|---|
| M1 로컬 빌드·실제 루트 재현 | 첫 검증 대상은 macOS Apple Silicon/Lima VZ arm64다. Dockerfile과 COPY 또는 컴파일이 필요한 소스를 사용해 cloud image 기반 레이어 체인을 빌드하고 로컬 DB에 기록한다. | 빌드에 사용한 소스 변경이 새 VM 결과에 반영됨을 확인한다. 게스트 mountinfo로 실제 `/`의 OverlayFS·의도한 lower 순서·개별 upper를 확인하고, SSH에서 소스 빌드 결과와 파일 쓰기를 실행한다. stop/start 후 파일 보존과 로컬 DB에서 기반·레이어·빌드 결과 조회를 확인한다. `/opt/layers/merged`만 구성하거나 chroot 안에서 실행한 결과로 대체하지 않는다. |
| M2 Hub 준비·게시·동일 아키텍처 재사용 | 기존 Hub를 검토·설정하고 로컬 메타데이터와 레이어를 게시한다. 다른 격리된 클라이언트 환경에서 조회·획득한다. | private 기본·산출물 해시·순서를 유지하고 새 환경에서 M1 작업을 재현한다. 아래 H1–H4의 익명/인증 경계·키/OAuth·쿼터 검증도 모두 통과해야 한다. 다른 Apple Silicon 장비 증거가 없으면 같은 장비의 격리 결과와 구분하며 교차 장비 완료를 주장하지 않는다. |
| M3 VM 작업 commit·재사용 | 사용자 ignore와 필수 식별정보 분리를 적용해 쓰기 영역을 레이어로 저장한다. | 수정 소스·설치 환경을 새 VM에서 재현하고, 제외 파일·외부 볼륨 포함 여부와 새 VM별 식별정보 분리를 검증한다. 명시적 삭제 전에는 작업 환경이 유지되고 삭제 후 미커밋 upper만 제거됨을 확인한다. |
| M4 OpenStack 서비스·대시보드·원격 SDK | 기존 조합 선택, Git 컨텍스트 빌드, Docker Hub 입력, clouds.yaml 인증을 통한 서비스 API 실행과 접근 검사를 제공한다. | 배포된 API와 대시보드에서 빌드 결과→실제 루트 VM→SSH/개발 작업→shelve/unshelve→명시적 삭제를 검증한다. 선택적 5분 자동 shelve는 위임·세션 관측 설계와 승인 후 검증한다. 개발자 직접 SDK/SSH 우회 실험으로 서비스 경로 완료를 대신하지 않는다. |
| M5 확장 기능 순차 구현 | M5.1 기반 이미지 마이그레이션·구조 검사·위험 승인 → M5.2 devcontainer.json → M5.3 Apple Silicon에서 amd64 교차 빌드 → M5.4 Hub 자체 Git 저장소 → M5.5 자유로운 패키지 레이어 조합 순서로 진행한다. | 사용자 확인된 확장 로드맵이다. 각 기능의 세부 범위·기술 설계·검증 전제를 고정하고 순서대로 증거를 기록한다. 구조 검사 결과를 실행 호환성 보장으로 표시하지 않는다. 기본 제품 단계 완료와 확장 로드맵 전체 완료를 구분한다. |

#### 반복 절차

1. 현재 단계의 기존 구현·테스트·실행 증거를 검토하고 기존/부분/누락을 갱신한다. 문서만으로 재구현 또는 완료를 결정하지 않는다.
2. 현재 단계의 실패하는 사용자 경로와 통과 조건을 고정한다. 저장소에서 답할 수 있는 사항은 조사하고, 제품 의도·권한·자원 선택만 사용자에게 질문한다.
3. 필요한 소스·계약을 수정하고 독립 리뷰, 영향 범위 검사, 실제 변경 경로 smoke를 수행한다. 요청된 Astra/Sol 역할의 가용성을 확인하고 불가하면 알린다.
4. 소스 검토 후 아키텍처·문서·변경 기록과 해당 guard를 갱신한다. 실제 source SHA/작업트리 스냅샷, 명령·결과·실패·한계를 기록한다.
5. 현재 승인 범위 내 실패는 원인을 수정해 반복한다. 해당 단계의 증거가 모두 충족돼야 다음 단계로 진행한다. 테스트 통과·로컬 실행·배포된 API 검증을 구분한다.

#### 자율 실행 범위와 보호 조건

- 인터뷰 당시 승인된 실행은 명세 편집뿐이었다. 이후 사용자가 이 체크리스트의 G0부터 실행하도록 지시했으므로 현재 허용/차단은 상단 G0-03과 G0-07의 기록을 따른다. 구현·로컬 VM·디스크·네트워크의 미확정 자원 한도는 자동 승인으로 보지 않는다.
- 로컬 실험은 별도 proof/state 경로와 명확히 소유한 VM을 사용한다. 사용자의 기존 Lima VM·state 루트·이미지·디스크를 변경하거나 재사용하지 않는다. 실행 전에 실제 도구가 격리 경로를 따르는지 확인한다.
- contributor-workflow의 2026-11-03 승인은 해당 Palimpsest 구현의 검토→patch 증가→새 immutable release→정본 Kolla 배포→API 검증 반복에만 적용한다. 이 인터뷰만으로 새 운영 범위나 배포 타깃을 확장하지 않는다.
- Nova/GPU·새 유료 VM, Ceph/IAM 권한 발급, 장치 변경·비소유 자원 제거는 별도 범위와 승인이 필요하다. 제품에 위험 승인 옵션이 있어도 에이전트 운영 승인과 CI·인증·소유권 제한을 우회하지 않는다.
- 단계별 자원·시간·시도 한도는 실행 전에 정한다. 사용한 예산을 실패나 삭제 후 환불 처리하거나 불확실한 생성·삭제를 무조건 재시도하지 않는다.

#### 중단·보고·완료

- 승인·필수 자원·안전한 격리·자격증명·외부 연결이 없으면 해당 작업을 보류하고 정확한 전제를 보고한다.
- 정리 실패, 소유권 불명, 생성·삭제의 불확실성, 데이터 유실 위험이 있으면 새 자원 작업을 중단한다. 소유 자원의 허용된 안전 조치와 증거 보존 외에 진행하지 않는다.
- 같은 원인과 같은 접근의 실패를 반복하지 않는다. 새 근거·변경된 해결책 없이 재실행하지 않고, 대안이 승인·제품 결정을 요구하면 질문한다.
- 미결정 사항은 관련 단계만 막는다. 예를 들어 private/internal 한도값은 해당 게시 정책 완료를 막지만 M1의 실제 루트 구현을 막지 않는다. 제품 의도를 임의로 정하지 않는다.
- 각 단계는 통과 증거·잔여 격차·정리 상태를 기록한다. 전체 필수 요구와 선택 기능의 포함 여부가 확정되고 해당 증거가 갖춰져야 전체 goal 완료로 판정한다. M1만으로 전체 완료를 선언하지 않는다.


### 사용자 여정에 따른 완료 판정 초안

다음은 목표에서 도출한 검증 시나리오이며, 구현 완료나 검증 성공 기록이 아니다. 지원 환경과 정확한 입력·출력은 후속 결정 후 고정한다.

| ID | 사용자 여정 | 완료 시 관찰해야 할 결과 |
|---|---|---|
| L1 | 로컬 Dockerfile과 필요한 소스로 빌드 | 결과로 새 VM을 만들고 소스 빌드 결과 및 합의된 개발 작업을 확인한다. 기존 컨테이너 실행이나 원래 빌드 VM 재사용으로 대체하지 않는다. |
| L0 | 로컬 레이어 메타데이터 기록 | 로컬 DB에 빌드 결과의 레이어 메타데이터가 기록되고, 이후 조회·게시에서 그 결과를 식별할 수 있다. 필수 필드는 후속 설계로 확정한다. |
| L2 | VM 안에서 작업한 뒤 레이어로 저장 | 저장한 결과로 새 VM을 생성했을 때 합의된 작업 결과가 재현된다. 원래 VM을 계속 사용하는 것으로 대체하지 않는다. |
| L3 | 저장한 레이어를 공유 | private는 본인, internal은 허용된 프로젝트 사용자, public은 익명 사용자가 획득해 새 VM에서 합의된 작업을 재현한다. 공개 범위별 재배포 권한과 비공개 부모의 보호도 유지한다. |
| C1 | 대시보드에서 플레이북 선택 | 선택한 이미지·레이어 조합으로 VM을 생성하고 워크로드 결과를 확인한다. |
| C2 | Git 링크로 빌드·실행 | 지정한 저장소와 리비전에서 빌드한 결과로 VM을 생성하고 워크로드 결과를 확인한다. 리비전 선택과 빌드 규칙은 미정이다. |
| C3 | Docker Hub 이미지 가져오기·직접 빌드 | 두 입력 경로 각각의 결과를 VM 실행에 사용할 수 있다. |
| C4 | 원격 인증 설정으로 서비스 이용 | 원격 클라이언트에서 인증하고 서비스 접근 검사 결과를 확인한 뒤, 서비스 API로 실행을 요청하고 결과를 확인한다. |
| H1 | 익명 public pull과 비공개 보호 | 인증정보 없이 public manifest와 허용된 blob을 획득하고 해시를 검증한다. private/internal의 manifest·blob·직접 digest URL은 익명으로 획득할 수 없고, public 참조로 비공개 전용 데이터를 우회 노출하지 않는다. |
| H2 | push 인증·권한 | 익명 push·게시·변경을 거부한다. Afterglow OAuth와 Palimpsest API 키 각각으로 허용 범위의 게시를 수행하고, 다른 사용자/프로젝트 및 키 범위 밖 쓰기를 거부한다. 회수·만료된 키는 거부한다. |
| H3 | 무료 쿼터 경계 | 확정된 집계 단위로 private 사용자별 2개와 internal 프로젝트별 5개까지 허용하고 다음 신규 게시를 거부한다. public은 개수 때문에 거부하지 않는다. 이미지별 비기반 레이어 합계의 private/internal/public 설정 한도 1/5/10 GB 각각에서 경계값과 초과를 검증하며 원본 cloud image 크기는 제외한다. 바이트 단위·집계 규칙 확정 전에는 통과를 선언하지 않는다. |
| H4 | 관리자 한도·유료 정책 | 운영자가 범위별 개수·용량 한도를 변경하면 설정된 정책이 게시 판정에 반영된다. 유료는 관리자 설정으로 무료보다 큰 개수와 약 50–100 GB 용량을 적용할 수 있다. 원본 이미지 포함 실제 저장 사용량과 정책 집계량을 구분해 조회한다. 하향 변경의 보존·초과 처리 정책은 설계 확정 후 검증한다. |
| R1 | VM 루트 구성 확인 | VM 내부의 실제 `/`가 의도한 OverlayFS이고, 기반 이미지와 순서가 지정된 읽기 전용 lower들 및 VM별 upper를 사용함을 확인한다. df 화면만으로 완료를 판정하지 않는다. |
| R2 | 동일 아키텍처의 다른 환경에서 재사용 | 게시한 메타데이터·레이어로 별도 환경에 새 VM을 만들고 합의된 개발 작업을 수행한다. Apple Silicon 간 재사용을 포함한다. |
| S1 | 보존 종료·재시작 | VM 쓰기 영역에 만든 작업 파일이 로컬 재시작 및 OpenStack shelve/unshelve 후 유지된다. 프로세스 지속은 요구하지 않는다. |
| S2 | 인스턴스 완전 삭제 | 해당 인스턴스의 미커밋 upperdir은 제거되며, 이미 저장한 레이어와 다른 VM의 lower·upper는 영향을 받지 않는다. |
| S3 | dev SSH/console 단절 자동 보존 종료 | 자동 처리를 선택한 dev에서 마지막 세션 단절 후 5분간 재접속이 없을 때만 보존 종료한다. 로컬 stop·클라우드 shelve 후 upper를 다시 사용할 수 있다. 유예 중 재접속은 취소, 남은 세션은 타이머 시작을 막는다. prod는 모든 연결이 끊겨도 종료하지 않는다. |
| P1 | prod 주 작업 실패·재실행 | 통제된 애플리케이션 OOM과 주 작업 크래시에서 VM과 작업 파일을 유지하고 unless-stopped 정책으로 재실행한다. 종료 코드·실패·재시작 상태를 표시한다. 명시적 stop 이후에는 재실행하지 않는다. 게스트 커널·호스트 장애를 이 증거로 검증했다고 주장하지 않는다. |

### 현재 소스 기준 재사용 대상과 격차

아래는 소스 선언과 제한된 검색으로 확인한 상태다. 실행 검증이나 완전한 기능 감사가 아니며, 기존 기능을 처음부터 재구현하라는 의미가 아니다.

| 목표 | 현재 근거 | 계획상 상태와 남은 확인 |
|---|---|---|
| Docker 사용자에게 익숙한 별도 CLI | `src/palimpsest_local/cli.py`에 별도 Palimpsest CLI와 exec, commit, pull, push 정의가 있다. | 기존 진입점 재사용. 실제 Docker CLI/API 호환은 목표가 아니다. pull/push 경로 모두가 VM 네이티브 경로라고 가정하지 않는다. |
| VM 작업을 레이어로 저장 | `cli.py:1335`의 commit은 지원되는 cloud-image VM 변경을 레이어로 저장한다고 명시한다. | 기존 진입점 재사용 후보. 환경 저장·복원·공유 경로를 확인한다. RAM/프로세스 복원은 목표가 아니며 첫 완료 목표는 Dockerfile 기반 재현이다. |
| 여러 VM 서비스 선언 | `cli.py:1246`의 compose는 palimpsest.yml용이며 Docker Compose가 아니라고 명시한다. | 기존 기능 있음. 실제 Docker CLI 호환은 불필요하다. Compose 파일이나 devcontainer.json 지원 여부는 별도 결정한다. |
| clouds.yaml 기반 원격 사용 | `src/palimpsest_local/cloud_env.py:1-7`은 named clouds.yaml과 openstacksdk로 OpenStack을 직접 조작하는 경로를 설명한다. | 부분 대응. 상주 서비스 API 경유 실행과 필요한 서비스 접근 검사까지 충족하는지는 별도 확인한다. |
| Git 링크 입력 빌드 | Hub api/services에서 git_url, repository_url, repo_url 이름의 입력을 검색했으나 찾지 못했다. | 미확인 격차. 다른 입력 모델·클라이언트·대시보드 경로까지 확인한 뒤 누락 여부를 확정한다. |
| 로컬 Dockerfile 빌드 | `buildkit.py:9-10,947-951`은 BuildKit의 합쳐진 rootfs tar를 내보내고 하나의 SquashFS로 포장한다. `buildkit.py:1723-1724,2205-2206`은 parent_digest=None과 지정된 cloud-image base digest를 기록한다. | 부분 구현. 이 runtime-pack 경로는 cloud image 대비 변경분 추출이나 Dockerfile 명령별 레이어 체인이 아니다. 기존 OCI FROM 결과 전체를 한 블록으로 포장하는 경로와 사용자가 요구한 cloud image→Python→Torch→vLLM 체인을 구분해야 한다. 기존 OCI 변환 등 다른 경로는 별도 검토하고, M1에서 FROM 해석·기반 매핑·체인 생성 계약을 확정한다. |
| 로컬 메타데이터 DB | `src/palimpsest_local/build.py`에 record.json 기록 경로가 있다. | 일부 기록 존재. 전체 저장소의 DB 사용 여부는 아직 조사하지 않았으며, 로컬 DB 요구에 맞는 통합·전환 범위는 미확인이다. |
| cloud image + 레이어를 실제 VM `/`로 구성하는 공통 경로 | `lima.py:1393-1394`, `cloud_env.py:13-14,1569-1575`, `docs/vm-workflow.md:15-17`의 일반 cloud-image 경로는 `/opt/layers/merged`를 구성하며 기본 `/`와 분리된다. `guest/stage1/README.md:8-15`의 실제 루트 전환은 별도 경로이고 README는 OCI-root를 Linux amd64 KVM으로 제한한다. | 목표 통합 기능 누락. 기존 마운트·검증 코드는 재사용 후보이나 Apple Silicon과 amd64에서 cloud image 기반 레이어를 실제 `/`로 사용하는 경로가 있다고 판정하지 않는다. 첫 구현 단계에서 기반 이미지 표현·부팅·게스트 루트 전환 설계를 확정해야 한다. |
| 외부 이미지의 개발용 부팅·접속 | 기존 stage-1 문서는 이미지 프로세스 실행 경로를 설명한다. 외부 OCI 이미지에 init·sshd·부팅 모듈이 모두 있다고 가정할 수 없다. | 미확인 설계 격차. 원본 사용자 공간을 유지하면서 CLI console·SSH와 prod supervisor를 제공할 부팅 구성요소를 설계해야 한다. 플랫폼 구성요소와 사용자 레이어를 구분하고 commit·과금 용량에의 포함 정책을 명시한다. Apple Silicon 경로의 지원은 별도 구현·검증한다. |

후속 실행 계획은 각 단계를 기존/부분 구현/누락/미확인으로 나누고, 기존 구현 검증 → 필요한 확장 → 종단 간 검증 순서로 작성한다. 현재 구현이 제품 목표를 제한하거나 구현 존재가 완료 증거를 대신하지 않는다.

기반 이미지 갱신 검사의 한계: 패키지 설치 레이어가 패키지 DB·캐시 같은 공통 파일을 변경하면 구조 검사에서 충돌로 나타날 수 있다. 경로 겹침이나 무충돌 결과를 실행 호환성 증명으로 표시하지 않는다. 우선 구조 검사 기반 마이그레이션을 지원하고, ABI·의존성·실행 검증은 제품의 필수 자동 검사로 요구하지 않는다. 이는 소유자의 목표 결정이며 현재 특정 레이어에서 관측한 충돌 결과는 아니다.


### 미결정 사항과 다음 인터뷰

1. 제품의 핵심 목표, 성능 수치 목표 없음, public 익명 pull·인증 push, 확장 순서는 사용자 확인을 받았다.
2. 구현 전 기술 결정: 부팅·SSH 구성, FROM과 기반 이미지 연결, 로컬 DB 통합, .plmignore 문법·적용 순서, 키 권한·OAuth 연결, 지연 작업 위임과 세션 관측을 기존 소스 검토로 구체화한다. 제품 계약이나 보안 경계를 바꾸는 대안만 사용자에게 올린다.
3. 용량의 압축/비압축·단위·중복 집계, 게시 개수의 버전/태그 단위와 정책 하향 시 동작은 보수적인 설계안을 문서화한다. 미확정 값을 사용자 확정사항으로 표시하지 않는다. 해당 정책 구현 전 필요한 결정만 묶어서 확인한다.
4. M1의 Apple Silicon/Lima VZ 우선 검증과 확장 로드맵 순서는 확정했다. 인터뷰 자체가 구현 루프를 승인했던 것은 아니며, 이후의 Goal 실행 지시와 실제 자원·권한·예산 확인은 상단 G0 실행 기록에서 구분해 관리한다.

## 원문 초안

1. palimpsest 의 개념 구체화.
1-1. palimpsest 는 이 프로젝트의 전체 이름으로 docker 처럼 hub 와 클라이언트, registry 로 이루어져있음.
1-2. palimpsest 는 여러 레이어로 구성된 시스템으로, 각 레이어는 독립적으로 동작하며 서로 다른 기능을 수행함.
1-3. palimpsest 의 레이어들은 docker 처럼 각 라이브러리나 패키지, 실행 파일등 무엇이든 될 수 있음.
1-4. palimpsest 의 레이러들은 서로 다른 환경에서 실행될 수 있고 이때 말하는 환경은 OS, 하드웨어, 네트워크 등 다양한 요소를 포함함.
2. palimpsest 의 구현 목적은 다음과 같아.
2-1. palimpsest 는 다양한 환경에서 실행될 수 있는 독립적인 레이어들을 제공함으로써, 개발자들이 다양한 환경에서 애플리케이션을 쉽게 배포하고 실행할 수 있도록 지원함. 이는 docker 와 동일한 목적을 가지고 있으며, 개발자들이 애플리케이션을 배포하고 실행하는데 필요한 환경 설정과 의존성 문제를 최소화함.
2-2. palimpsest 는 로컬 개발자 클라이언트와 격리된 sandbox·클라우드 환경을 모두 지원하며, 레이어 재사용을 통한 효율적인 배포를 위해 설계됨.
2-3. docker 는 컨테이너라는 기술을 사용하여 애플리케이션을 격리된 환경에서 실행할 수 있도록 지원하지만, palimpsest 는 그 중 레이어들을 중심으로 하여, 각 레이어가 독립적으로 동작할 수 있도록 설계됨. 이를 통해 개발자들은 각 레이어를 개별적으로 관리하고 업데이트할 수 있으며, 필요에 따라 특정 레이어만 교체하거나 수정할 수 있음.
2-4. 이 레이어를 기반한 애플리케이션 관리가 필요한 이유는 ansible 이나 terraform 같은 도구들로 인프라를 배포할때 동일한 리소스를 여러 머신에서 재사용할 수 있도록 하기 위함임. palimpsest 는 이러한 요구사항을 충족시키기 위해 설계되었으며, 각 레이어는 독립적으로 동작하면서도 서로 상호작용할 수 있도록 구성됨.
2-5. 여러 머신에서 동일한 레이어를 재사용해야하는 이유는 배포의 효율성에 기반함. 동일한 레이어를 여러 머신에서 재사용함으로써, 개발자들은 배포 시간을 단축하고, 리소스 사용을 최적화할 수 있음. 또한, 동일한 레이어를 사용함으로써, 애플리케이션의 일관성을 유지할 수 있으며, 버그나 문제 발생 시 빠르게 대응할 수 있음.
2-6. 동일한 레이어를 재사용하면 설치과정을 단순화할 수 있으며, 각 레이어의 설치 및 설정 과정을 표준화하여 배포 과정에서 발생할 수 있는 오류를 최소화할 수 있음. 이는 개발자들이 애플리케이션을 보다 안정적으로 배포하고 운영할 수 있도록 지원함.
2-7. palimpsest 는 이 레이어를 빌드하고 관리하는 기준을 dockerfile 을 사용할것임. dockerfile 을 사용하면 해당 레이어에 들어갈 정보를 개발자가 쉽게 정의할 수 있으며, 이를 통해 레이어의 설치 및 설정 과정을 자동화할 수 있음. 또한, dockerfile 로 관리하면 docker hub 나 registry에 있는 이미지들과 동일한 규격으로 레이어를 관리할 수 있기에 기존 이미지를 활용할 수 있음. 이는 개발자들이 기존에 사용하던 도구와 호환성을 유지하면서, palimpsest 의 레이어를 효율적으로 관리할 수 있도록 지원함.
3. palimpsest 의 레이어 관리 및 배포 과정은 다음과 같아.
3-1. palimpsest 에 dockerfile 을 작성하여 레이어를 정의하고, 이를 vm에 빌드하여 레이어를 생성함.
3-2. 생성된 레이어와 사용된 이미지 정보를 palimpsest registry 에 업로드함.
3-3. 사용될 이미지는 기본적으로 cloud image 를 사용하며, 필요에 따라 개발자가 직접 빌드한 이미지도 사용할 수 있음.
3-4. palimpsest registry 에 업로드된 레이어는 다른 개발자들이 다운로드하여 로컬에서 vm 으로 사용할 수 있으며 openstack 같은 클라우드에서는 해당 레이어를 기반으로 vm 을 생성하여 사용할 수 있음.
3-5. 현재는 nfs 같은 file share 를 통해 레이어를 공유하고 있지만, 이보다 더욱 효율적이거나 안정적인 방법이 있다면 이를 적용할 수 있음. 예를 들어, 레이어를 공유하는 전용 서버를 구축하거나, 클라우드 스토리지를 활용하여 레이어를 관리할 수 있음. 또한, 레이어의 버전 관리를 통해 개발자들이 필요한 버전을 쉽게 선택하고 사용할 수 있도록 지원함.
