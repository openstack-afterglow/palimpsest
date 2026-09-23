# Palimpsest Local contributor rules

## Resume ongoing work

- 새 세션에서는 [`README.md`](README.md)의 Development, [`docs/development-handoff.md`](docs/development-handoff.md), [`ARCHITECTURE.md`](ARCHITECTURE.md)를 읽고 현재 Git/source 상태와 대조한다. [`agent.md`](agent.md)는 이 파일로 연결하는 진입점이며 별도 규칙이 아니다.
- 인계 문서에는 전체 목표·확정 결정·이미지별 결과·미완료 작업·승인 대기·정확한 재개 순서가 있다. 날짜가 있는 기록을 현재 서버 상태나 새 승인으로 해석하지 않는다.
- 먼저 `git status --short`, `git diff --stat`, `git diff --cached --stat`, `git log -5 --oneline`으로 staged/unstaged와 마지막 commit을 구분한다. 기존 미승인 증거 문서와 새 작업을 섞어 commit하지 않는다.
- 사용자 지정 역할은 Astra의 계획·오케스트레이션·관리, Sol의 개발이며 독립 검토를 거친다. 호스트에서 해당 모델/검토가 불가능하면 대체를 조용히 확정하지 말고 제약을 보고한다.
- 개발 중에는 정확한 노드 또는 영향받은 test lane부터 실행한다. native/ML/GPU 실기는 별도 전제와 승인 범위에서 순차 실행하고, portable 통과를 실기 성공으로 기록하지 않는다.
- 인계에 남은 GitHub 게시 및 원격 helper 전송 차단은 명시적 승인 전까지 유지한다. 문서화·계속 진행 요청만으로 해제하지 않으며 다른 경로로 같은 차단 작업을 우회하지 않는다. 장치 재할당·driver unbind/reset·기존 실패 VM 제거는 읽기 전용 점검과 별도 권한이다.
- 작업 종료 시 인계의 다음 작업·근거 SHA·실행한 검사·실패·승인 대기를 갱신한다. 과거 결과를 새 성공으로 덮어쓰거나 실제 비밀값을 기록하지 않는다.

## Architecture maintenance

- 작업 전에 root [`ARCHITECTURE.md`](ARCHITECTURE.md)와 영향을 받는 상세 문서를 읽는다.
- code/config/schema/dependency/deploy/test를 바꾸면 현재 source를 먼저 검토하고, 영향받은 architecture 본문·code map·contracts·limits와 상세 문서를 같은 변경에서 갱신한다.
- bugfix/refactor가 구조에 영향을 주지 않는 경우에도 그 판단과 근거를 `ARCHITECTURE.md`의 최신 Maintenance review summary에 남긴다.
- 계획, roadmap, historical baseline은 구현 또는 검증 증거가 아니다. source와 현재 테스트 정의가 충돌하면 source를 정본으로 삼는다.
- 실제 source 검토가 끝난 뒤에만 review marker를 stamp한다. credential, password, bearer token, private key를 문서·ledger·로그에 복사하지 않는다.
- 완료 또는 commit 전에 working-tree 범위는 `python3 scripts/check_architecture.py`, staged 범위는 `python3 scripts/check_architecture.py --staged`로 검사한다. 이 검사는 문서 본문을 자동 생성하거나 source를 수정하지 않는다.
- 문서 변경만으로 테스트 통과를 주장하지 않는다. 테스트 정의(`test-defined`), 실제 실행(`test-passed`), 실제 환경 관찰(`live-verified`)을 구분한다.

## Verification entry points

- Portable manifest: `uv run python scripts/test_lanes.py list --check`
- Changed-file plan: `uv run python scripts/test_lanes.py plan --changed HEAD`
- Core lane: `uv run python scripts/test_lanes.py run core-cli`
- Architecture guard regression: `uv run pytest -q tests/unit/test_architecture_guard.py`
- Hub tests use the independent environment under `hub/`: `cd hub && uv run pytest -v`
- Native KVM, privileged filesystem, guest binary, BuildKit, and Gate 2 lanes are explicit opt-in proofs; their prerequisites and commands are documented in `ARCHITECTURE.md` and `docs/testing.md`.

## Protected historical material

`IMPLEMENTATION_PLAN.md`, `docs/oci-docker-hub-compatibility.md`, `docs/oci-public-runtime-roadmap.md`, and `tracking/afterglow-palimpsest.json` are historical or qualification records. Read them for context, but do not rewrite their evidence as current implementation or alter them without an explicitly scoped task.

## CI 파이프라인 성능 규정

근거는 2026-09-24의 읽기 전용 실측이다. `Test` workflow(`.github/workflows/test.yml`)가 현재의 19-job 형태로 완료한 실행 21건(2026-09-15~09-23; 이 형태는 `1a23f9c`에서 dev에 들어왔다)을 측정했다. 크리티컬 패스는 실행 생성 시각(재실행은 `run_started_at`)부터 마지막 job 종료까지로 잡았고, 중앙값 325초·p90 781초였다. 단일 KVM runner에서 직렬화된 2026-09-21 12:39–12:43 burst 8건을 빼면 13건 기준 중앙값 303초·p90 346초였다. 마지막으로 끝난 job은 21건 중 19건이 `Unit tests (macOS 15)` aggregator였다.

병목은 세 가지였다.

- `portable-macos`의 `max-parallel: 2`가 두 번째 wave를 만들었다. 3/4·4/4 shard의 대기 중앙값이 151·170초였다.
- `portable-linux`의 `max-parallel: 3`도 두 번째 wave를 만들었다. 4–6 shard 대기가 91–105초였다.
- `Native KVM stage-1 proof`가 단일 self-hosted runner `pieroot-server-palimpsest-kvm`에서 직렬화된다. 실행 140초, 대기 중앙값 142초·p90 626초였다.

2026-09-24 `ci-perf` 변경은 두 matrix의 `max-parallel`을 제거했다. dev/PR 크리티컬 패스 약 155–170초라는 기대치는 KVM job 약 140초를 하한으로 한 추정이며, push 뒤 재측정 전까지 효과로 주장하지 않는다. CI를 바꾸는 모든 변경은 아래 규칙을 따른다.

1. **측정 먼저, 추정 금지.**
   - CI를 바꾸기 전과 후에 최근 20회 이상 `Test` 실행의 job·step 시간을 `gh run list --workflow test.yml`와 `gh api repos/openstack-afterglow/palimpsest/actions/runs/<id>/jobs`로 수집한다.
   - 크리티컬 패스 중앙값과 p90을 commit 본문, [인계 문서](docs/development-handoff.md), `ARCHITECTURE.md` Maintenance summary에 남긴다. 이 저장소에는 OpenSpec이 없다.
   - 절감은 합산되지 않는다. 가장 늦게 끝나는 job부터 줄이고, 효과는 실제 CI 전후 수치로만 주장한다.
2. **목표 지표를 먼저 정한다.**
   - 이 저장소는 public이고 GitHub-hosted runner 시간이 무료이므로 목표는 wall-clock이다.
   - org `openstack-afterglow`의 Free plan 동시성 한도(hosted 20 job, macOS 5 job)는 lumen·openstack-afterglow·drover·waygate·afterglow-crypto와 공유한다. macOS runner를 쓰는 저장소는 이곳뿐이다.
   - 한 `Test` 실행은 hosted job 약 15개를 동시에 요구한다. 따라서 dev·main 동시 push처럼 겹치는 실행의 대기도 함께 측정한다.
3. **게이트 job을 테스트 job 앞에 두지 않는다.**
   - `Lint, manifests, and package`(`checks`)는 portable shard의 `needs:`로 걸지 않고 병렬로 실행한다. 그 결과는 `Pure contracts (Python 3.12)` aggregator에서만 합친다.
   - `if: always()`와 `needs:`를 함께 가지는 job은 `Pure contracts (Python 3.12)`, `Unit tests (macOS 15)`, `Required native KVM proof` 세 aggregator뿐이다.
   - build·배포 게이팅은 테스트 전체 결과로 한다. `hub-docker.yml`의 `build-and-push`는 현재 자체 `Hub Unit Tests` job에 `needs:`를 건다. 이 구조는 테스트 크리티컬 패스 밖에 있고, 바꾸면 배포 의미가 달라지므로 별도 결정 없이 바꾸지 않는다.
4. **job당 고정비를 측정한다.**
   - 고정비는 checkout, `setup-uv`, `setup-python`, `uv sync --frozen --extra dev`에 드는 시간이다. 2026-09 기준으로 Linux shard 약 6초, macOS shard 약 9초였고 각각 job 시간의 약 7%다.
   - `setup-uv` v6의 기본 cache 덕분에 `uv sync`는 약 1초다. cache 복원이 재설치보다 느리면 cache를 쓰지 않는다.
   - 서비스 container를 추가하면 health-check interval은 짧게(예: 2초) 두고 retries나 start-period는 충분히 둔다.
5. **샤딩은 고정비가 작을 때만 하고, 모든 shard를 한 wave로 띄운다.**
   - Portable shard는 `scripts/test_lanes.py`의 안정 key 분할을 쓴다. key는 module/class/function과 parameter index의 sha256이다.
   - workflow step은 `uv run python scripts/test_lanes.py run portable --shard ${{ matrix.shard }}/M`을 직접 호출한다. 인자를 누락할 수 있는 다른 script나 `-- --shard` 형태의 wrapper로 감싸지 않는다. 계약 테스트가 이 명령 문자열을 고정한다.
   - matrix의 shard 목록, `--shard …/M`의 분모, job 이름의 분모는 서로 일치해야 한다. `max-parallel`은 없거나 shard 수 이상이어야 한다.
   - 각 shard는 "Lane shard" 수 줄을 출력한다. 빈 shard나 수집 오류가 나면 실패로 처리한다(fail-closed).
   - shard 수를 바꿀 때는 실측 대기 시간과 org 동시성 한도로 다시 판단한다.
6. **격리 해제는 opt-in으로만 한다.**
   - 테스트 간 상태 공유나 프로세스 재사용 같은 최적화는 전역에 적용하지 않는다. 먼저 순서를 섞어(shuffle) 2회 이상 실행해 상태 누수가 없는지 확인하고, 안전한 파일만 명시적으로 opt-in한다.
   - 환경 변수나 전역 상태를 바꾼 테스트는 `monkeypatch`나 fixture로 반드시 복원한다.
   - `tests/conftest.py`의 `PALIMPSEST_LOG_HOME` 같은 session 고정값을 약화하지 않는다.
7. **테스트는 hermetic해야 병렬화할 수 있다.**
   - portable test는 실제 KVM·libvirt·Docker·원격 서비스·host 전역 디렉터리에 의존하지 않는다. host 설정 유무에 따라 결과나 시간이 달라지면 결함이다.
   - 현재 pytest-xdist는 쓰지 않는다. 도입하려면 먼저 세 가지를 충족한다.
     - shuffle 실행으로 hermetic함을 증명한다. Linux 6/6의 0.1초 lock-timeout flake가 아직 해결되지 않았다.
     - xdist controller는 `pytest_collection_modifyitems`를 실행하지 않아 "Lane shard" 증거 줄이 사라진다. 이 문제를 먼저 해결한다.
     - worker 수를 runner vCPU에 맞춰 명시한다. `-n auto`는 금지한다.
8. **변경 감지의 diff 기준을 정확히 한다.**
   - 현재 CI에는 변경 감지가 없어 모든 push/PR이 전체 portable을 실행한다. `test_lanes.py plan --changed`는 로컬 선택 도구다.
   - CI에 변경 감지를 도입하면 push는 `github.event.before..github.sha`로 비교하고, zero SHA·forced push·fetch 실패일 때는 전체를 실행한다.
   - PR은 merge-base 기준으로 비교한다. `git diff --name-only <base>...<head>`(세 점), 또는 PR merge ref(`refs/pull/<n>/merge`)의 merge commit에서 `HEAD^1..HEAD`를 쓴다. 두 tip을 바로 비교하는 `git diff <base>..<head>`는 base에만 있는 변경까지 포함하므로 쓰지 않는다. base를 fetch하지 못하면 전체를 실행한다.
   - 그 밖에 push나 PR head checkout에서 `HEAD^1..HEAD`처럼 마지막 commit만 보는 비교는 금지한다. 위 merge ref의 merge commit만 예외다.
   - 발행 산출물(Hub image, development package)은 실제로 발행된 revision을 기준으로 판단한다.
9. **중복 실행은 입력 동일성으로만 제거한다.**
   - PR 테스트를 건너뛸 수 있는 경우는 같은 저장소 branch에서 온 PR이면서 merge tree가 head tree와 같을 때뿐이다. branch 이름만으로 판단하지 않는다.
   - fork PR과 dependabot PR은 항상 테스트한다.
   - 2026-09 실측에서 가장 큰 중복은 같은 SHA를 dev와 main에 3–6초 간격으로 push한 경우였다. 현재 형태의 push 실행 14건 중 7건이 그랬고, 매번 KVM 대기가 142–144초 생겼다. 이를 줄이는 방법(예: dev가 green이 된 뒤 main을 fast-forward)은 소유자가 결정한다.
   - `codex/oci-root-phase1` → dev PR은 그 branch의 유일한 `Test` 실행이므로 건너뛰지 않는다.
10. **보안: public 저장소의 `pull_request` 코드를 self-hosted runner에서 실행하지 않는다.**
    - `Test` workflow에서 self-hosted runner(`[self-hosted, linux, x64, kvm]`)를 쓰는 job은 `kvm`뿐이다. `release.yml`의 `kvm-proof`(`Required KVM clean-host proof`)도 같은 runner를 쓰지만 `v*` tag push에서만 실행된다.
    - `pull_request` 실행은 PR 쪽의 workflow 파일을 사용하므로 YAML `if:`(`vars.PALIMPSEST_KVM_ENABLED` 조건 포함)는 통제 수단이 아니다. PR이 그 조건을 지울 수 있다. runner group의 저장소 제한과 fork PR 승인 설정(`all_external_contributors`)으로 보장해야 한다.
    - 2026-09-24 기준으로 두 통제 모두 아직 충족하지 않았다.
      - fork PR 승인 정책은 `first_time_contributors`다.
      - runner group 제한은 org 수준 runner에만 적용된다. `pieroot-server-palimpsest-kvm`은 저장소 수준 runner로 등록돼 있다(repo runners API). 이 통제를 쓰려면 먼저 runner를 org runner group으로 옮겨야 한다. org plan(`free`)에서 저장소·workflow 제한을 쓸 수 있는지는 확인하지 않았다.
      - 두 통제가 갖춰지기 전까지 완전한 통제는 runner를 stop·unregister하는 것뿐이다.
    - 2026-09-24 읽기 전용 조회에서 API가 나열한 `pull_request` 실행 22건은 모두 이 저장소의 branch에서 왔고, fork에서 온 실행은 나열되지 않았다. KVM runner에서 실행된 run `35600862976`(`dev`)과 `35600812317`·`35029001178`(`codex/oci-root-phase1`)도 같은 저장소 branch의 PR 실행이다.
    - 따라서 fork 코드 노출은 잠재 위험이다. `first_time_contributors`에서는 이전에 merge된 기여가 있는 외부 contributor의 fork PR이 승인 없이 persistent KVM runner에서 실행될 수 있다. 같은 저장소 PR도 `pull_request` 코드다. 현재 `kvm` job은 `pull_request`에서도 실행되므로 이 규칙의 제목을 아직 충족하지 않는다.
    - 완화 조치는 인계 문서의 승인 대기 2번이며 소유자가 결정한다. 명시적 승인 전에는 설정을 바꾸지 않는다.
    - `Required native KVM proof`가 skip을 통과로 받도록 바꾸지 않는다.
11. **CI 형태는 계약 테스트로 고정한다.**
    - `tests/unit/test_test_lanes.py`는 다음을 검사한다.
      - portable matrix의 shard 목록·분모·`max-parallel`·`fail-fast`와 shard 명령 문자열
      - aggregator 세 개의 이름·`if: always()`·정확한 `needs`
      - aggregator마다 하나뿐인 verdict step의 `env`(각 `needs.<dep>.result`, KVM은 `vars.PALIMPSEST_KVM_ENABLED`도)와 성공만 받는 정확한 `run` 문자열. verdict step과 aggregator job에는 `shell`·`defaults`·`continue-on-error`가, workflow에는 `defaults`가 없어야 하며, 의존 job과 그 step에도 `continue-on-error`가 없어야 한다.
      - 테스트 job 앞에 gate job이 없는지, aggregator 밖의 job-level `if:`가 `kvm`의 `vars.PALIMPSEST_KVM_ENABLED == 'true'`뿐인지
      - 모든 workflow에서 GitHub-hosted label(`ubuntu-*`·`macos-*`·`windows-*`)이 아닌 runner를 쓰는 job이 `test.yml`의 `kvm`과 `release.yml`의 `kvm-proof`뿐인지, 그리고 `release.yml`이 `v*` tag push 전용인지. `runs-on`의 문자열·목록·mapping(`group`·`labels`) 형태를 모두 검사한다.
    - `tests/unit/test_oci_convert_security.py`는 `oci-fs-proof`부터 `unit-macos` 직전까지 job-level `if:`가 없는지 텍스트로 검사한다. 그러므로 이 구간의 job 순서를 바꾸지 않는다.
    - `tests/unit/test_development_package_workflow.py`는 development-package의 trigger·concurrency·step을 고정한다.
    - 새 CI 불변식은 새 파일을 만들기보다 이 파일들을 확장한다. 새 test 파일은 `scripts/test_lanes.py`에 분류해야 하기 때문이다.
12. **지속 개선.**
    - CI를 바꾸는 변경에는 전후 실측을 첨부한다.
    - 다음 중 하나라도 해당하면 1번 절차로 다시 측정하고 가장 늦게 끝나는 job부터 개선한다.
      - `Test` 크리티컬 패스 중앙값이 마지막으로 기록된 기준보다 20% 이상 나빠진다.
      - portable node 수가 크게 늘어난다. 기준은 `60fa42f`의 CI run `35826465548` "Lane shard" 줄의 선택 node 합계 6,081이다(Linux 6 shard와 macOS 4 shard 합계가 같다; pass 수가 아니다). 그 뒤 CI 형태 계약이 node를 더했다. 로컬 기준으로 `2e37538`은 6,084, review 1차 반영 뒤는 6,088(`--collect-only`)이다.
      - 새 테스트 계층이나 job을 추가한다.
    - `.github/**`와 `AGENTS.md`는 architecture digest 범위에 들어간다. 이 파일을 바꾸면 Maintenance summary를 갱신하고 `--stamp`와 `--staged` 검사를 거친다.
