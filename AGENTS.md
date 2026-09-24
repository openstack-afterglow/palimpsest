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
   - workflow 정의로 센 값이며 실측이 아니다. `Test` 실행은 시작 시점에 hosted job 15개(macOS 4개 포함)를 요구한다. aggregator 3개는 의존 job이 끝난 뒤 시작하므로 실행 하나의 hosted job은 모두 18개다. `kvm`은 self-hosted라 이 수에 들어가지 않는다.
   - 같은 event로 다른 workflow도 시작한다. `main`·`dev` push에는 `hub-docker.yml`의 `test`와 `development-package.yml`의 `verify`가 더해져 시작 시점 hosted job이 17개다. `main`·`dev` 대상 PR에는 `hub-docker.yml`의 `test`만 더해져 16개다.
   - 따라서 dev·main 동시 push(17+17개, macOS 4+4개)는 hosted 20개·macOS 5개 한도를 넘는다. 겹치는 실행의 대기도 함께 측정한다.
3. **게이트 job을 테스트 job 앞에 두지 않는다.**
   - `Lint, manifests, and package`(`checks`)는 portable shard의 `needs:`로 걸지 않고 병렬로 실행한다. 그 결과는 `Pure contracts (Python 3.12)` aggregator에서만 합친다.
   - `if: always()`와 `needs:`를 함께 가지는 job은 `Pure contracts (Python 3.12)`, `Unit tests (macOS 15)`, `Required native KVM proof` 세 aggregator뿐이다.
   - image·package **발행(push)과 배포**는 `Test` workflow 전체 결과로 게이팅한다. 아무것도 발행하지 않는 PR 검증 build는 테스트와 병렬로 돌려도 된다. `hub-docker.yml`의 `pull_request` 실행은 이 규칙을 지킨다. `build-and-push`가 두 image를 `push: false`로 build만 하기 때문이다.
   - 다음 세 발행 경로는 `Test` 전체 결과를 기다리지 않는다. 정본 규칙 3의 기존 예외다. 모두 테스트 크리티컬 패스 밖에 있고, 바꾸면 발행 의미가 달라진다. 그래서 소유자 결정 없이 바꾸지 않는다. 결정 항목은 [인계 문서](docs/development-handoff.md) `CI critical-path checkpoint (2026-09-24)` 절의 승인 대기 목록에 있다.
     - `hub-docker.yml` `build-and-push`: `main`·`dev` push, `v*` tag push, `workflow_dispatch`에서 GHCR에 image를 push한다. 자체 `Hub Unit Tests` job(`hub/tests/`)에만 `needs:`를 건다.
     - `development-package.yml` `publish`: `contents: write`로 SHA별 GitHub prerelease를 만든다. 자체 `verify`에만 `needs:`를 건다. `verify`가 실행하는 테스트는 `core-cli`·`qualification` lane과 development-package 계약뿐이다. 그 밖에는 architecture freshness·CLI reference·test-lane manifest 검사, 대상 파일 `ruff check`, package build를 한다.
     - `release.yml` `publish`(PyPI)와 그 뒤의 `github-release`: 자체 `verify`와 `kvm-proof`에만 건다. `verify`는 `tests/unit`, Kolla 계약, guest stage-1, BuildKit build를 실행하지만 macOS shard와 portable 전체는 실행하지 않는다.
4. **job당 고정비를 측정한다.**
   - 고정비는 checkout, `setup-uv`, `setup-python`, `uv sync --frozen --extra dev`에 드는 시간이다. 2026-09 기준으로 Linux shard 약 6초, macOS shard 약 9초였고 각각 job 시간의 약 7%다.
   - `setup-uv` v6의 기본 cache 덕분에 `uv sync`는 약 1초다. cache 복원이 재설치보다 느리면 cache를 쓰지 않는다.
   - 서비스 container를 추가하면 health-check interval은 짧게(예: 2초) 두고 retries나 start-period는 충분히 둔다.
5. **샤딩은 고정비가 작을 때만 하고, 모든 shard를 한 wave로 띄운다.**
   - Portable shard는 `scripts/test_lanes.py`의 안정 key 분할을 쓴다. key는 module/class/function과 parameter index의 sha256이다.
   - workflow step은 `uv run python scripts/test_lanes.py run portable --shard ${{ matrix.shard }}/M`을 직접 호출한다. 인자를 누락할 수 있는 다른 script나 `-- --shard` 형태의 wrapper로 감싸지 않는다. 계약 테스트가 이 명령 문자열을 고정한다.
   - matrix의 shard 목록, `--shard …/M`의 분모, job 이름의 분모는 서로 일치해야 한다. `max-parallel`은 없거나 shard 수 이상이어야 한다.
   - 각 shard는 "Lane shard" 수 줄을 출력하지만, CI는 이 수를 출력만 하고 검증하지 않는다. 실행 중에 실패로 처리되는 경우(fail-closed)는 세 가지다. 빈 shard는 pytest exit 5, 수집 오류는 exit 2, plugin 없이 `--test-lane-shard`가 전달되면 usage error exit 4다.
   - 정본 규칙 5는 "샤드별 실행 파일 수를 CI에서 검증한다"고 요구한다. 이 저장소는 실행 중 검사를 하지 않는다. shard별 node 수와 shard 합계가 portable 전체와 같은지는 CI에서 검사하지 않는다. 이 점이 정본 규칙과 다르다. 대신 아래 계약이 정본이 경고한 wrapper 위험을 막는다.
     - `test_ci_portable_matrix_runs_every_shard_in_one_wave`가 shard job의 step 전체와 명령 문자열을 고정한다. 정본이 경고한 위험, 즉 `test_lanes.py` 앞에서 `--shard`가 빠져 모든 shard가 전체 suite를 실행하는 경우를 막는 것은 이 고정뿐이다. 이 경우 `--test-lane-shard`가 아예 전달되지 않으므로 위 exit 4 검사도 발동하지 않는다.
     - `test_portable_command_shards_nodes_without_special_selectors_or_environment_mutation`은 `commands()`가 `--test-lane-shard`를 pytest argv에 넣는지 확인한다.
     - `test_node_shards_are_deterministic_disjoint_and_complete`와 `test_separate_process_random_uuid_parameters_have_exact_complete_shard_union`은 분할이 서로소이고 합이 전체가 되는지 확인한다.
   - 이 계약은 workflow 밖의 무력화를 막지 않는다. 예를 들어 `pyproject.toml`의 `addopts = "--collect-only"`나 conftest hook이 실행을 막아도 모든 shard는 "Lane shard" 줄을 출력하고 exit 0으로 끝난다. 2026-09-24에 `PYTEST_ADDOPTS=--collect-only`로 `run portable --shard 1/256`을 실행해 보니 selected 24 node를 출력하고 한 건도 실행하지 않은 채 exit 0이었다. 실행된 수를 selected 수와 대조하는 실행 중 검사라면 이런 무력화를 잡는다. 그 검사를 추가할지, 이 차이를 받아들일지는 소유자가 결정한다. 결정 항목은 [인계 문서](docs/development-handoff.md) `CI critical-path checkpoint (2026-09-24)` 절의 승인 대기 목록에 있다.
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
   - 위 두 항목은 정본 규칙 8을 의도적으로 구체화한 것이다. 정본의 "PR은 base..head"는 PR이 들여오는 변경(merge-base 의미)을 뜻하는 것으로 읽는다. git의 두 점 `git diff A..B`는 두 tip을 직접 비교하므로 그 뜻과 다르다. 정본이 금지한 `HEAD^1..HEAD`는 마지막 commit만 보는 비교를 뜻한다. merge ref의 merge commit에서는 첫 parent가 base tip이므로 PR 전체 변경을 비교하게 되어 금지 대상이 아니다. 두 점 diff로 되돌리지 않는다.
   - 발행 산출물(Hub image, development package)은 실제로 발행된 revision을 기준으로 판단한다.
9. **중복 실행은 입력 동일성으로만 제거한다.**
   - PR 테스트를 건너뛸 수 있으려면 세 조건을 모두 충족해야 한다.
     - 같은 저장소 branch에서 온 PR이다.
     - merge tree가 head tree와 같다.
     - 그 head branch의 push로 완료된 `Test` 실행이 같은 tree를 이미 테스트했다.
   - branch 이름만으로 판단하지 않는다. fork의 동명 branch로 우회할 수 있다.
   - `test.yml`의 push trigger는 `main`·`dev`뿐이다. 그래서 지금 세 번째 조건을 충족할 수 있는 head branch도 이 둘뿐이다. 다른 head branch(예: `codex/oci-root-phase1`)에서는 PR 실행이 그 branch의 유일한 `Test` 실행이므로 건너뛰지 않는다.
   - fork PR과 dependabot PR은 항상 테스트한다.
   - 2026-09 실측에서 가장 큰 중복은 같은 SHA를 dev와 main에 3–6초 간격으로 push한 경우였다. 현재 형태의 push 실행 14건 중 7건이 그랬고, 매번 KVM 대기가 142–144초 생겼다. 이를 줄이는 방법(예: dev가 green이 된 뒤 main을 fast-forward)은 소유자가 결정한다.
10. **보안: public 저장소의 `pull_request` 코드를 self-hosted runner에서 실행하지 않는다.**
    - `Test` workflow에서 self-hosted runner(`[self-hosted, linux, x64, kvm]`)를 쓰는 job은 `kvm`뿐이다. `release.yml`의 `kvm-proof`(`Required KVM clean-host proof`)도 같은 runner를 쓰지만 `v*` tag push에서만 실행된다.
    - 통제는 두 층이다. 정본 규칙 10은 workflow YAML의 `if:` gate를 두고, 저장소 설정으로도 보장하라고 요구한다.
      - 첫째 층은 YAML event gate다. 예를 들어 `kvm` job의 `if:`에 `github.event_name != 'pull_request'`를 더하면, workflow를 고치지 않은 PR은 이 runner에 오지 않는다.
      - 둘째 층은 설정이다. `pull_request` 실행은 PR 쪽 workflow 파일을 쓰므로 PR이 gate를 지울 수 있다. 설정은 그런 PR에 대한 backstop이다.
    - 설정만으로는 충분하지 않다.
      - fork PR 승인 정책은 fork PR에만 적용되고, 승인되면 실행을 막지 않는다.
      - runner group의 저장소 제한은 허용한 저장소 안에서 push 실행과 `pull_request` 실행을 구분하지 못한다.
      - 따라서 fork PR 승인을 `all_external_contributors`로 두고 저장소 제한을 걸어도, 같은 저장소 branch의 PR, Dependabot PR, 승인된 fork PR은 여전히 이 runner에서 실행된다. Dependabot PR은 Dependabot을 켠 경우에만 생긴다. 이 저장소에는 `.github/dependabot.yml`이 없고, security update 설정은 확인하지 않았다.
      - 이 PR들을 runner에서 떼어 놓는 것은 YAML event gate다. runner group의 workflow 접근 제한을 ref 단위로 걸 수 있다면 그것도 가능하다. 다만 이 org plan에서 그 제한을 쓸 수 있는지는 확인하지 않았다.
    - 2026-09-24 기준으로 두 층 모두 갖추지 않았다.
      - YAML event gate가 없다. `kvm`의 `if:`는 `vars.PALIMPSEST_KVM_ENABLED == 'true'`뿐이라 `pull_request`에서도 실행된다. repository variable을 읽는 이 조건도 YAML에 있으므로 PR이 지울 수 있다.
      - fork PR 승인 정책은 `first_time_contributors`다.
      - `pieroot-server-palimpsest-kvm`은 저장소 수준 runner로 등록돼 있어 runner group이 없다(repo runners API). org plan은 `free`이고 runner-group API는 403이었다. GitHub 문서상 추가 runner group은 Team plan부터 만들 수 있다. Free plan의 기본 group에 저장소 제한이나 workflow 제한을 걸 수 있는지는 확인하지 않았다.
      - 지금 PR의 workflow 수정까지 포함해 노출을 확실히 없애는 방법은 runner stop·unregister뿐이다.
    - 설정 층은 소유자 작업이다. 명시적 승인 전에는 바꾸지 않는다. 위치는 다음과 같다.
      - fork PR 승인: 저장소 `Settings` → `Actions` → `General`(`https://github.com/openstack-afterglow/palimpsest/settings/actions`)의 `Approval for running fork pull request workflows from contributors`에서 `Require approval for all external contributors`를 고르고 `Save`한다.
      - runner group 제한: 저장소 수준 runner에는 해당 설정이 없다. 쓰려면 먼저 runner를 org 수준으로 다시 등록한다. 그다음 org `Settings` → `Actions` → `Runner groups`(`https://github.com/organizations/openstack-afterglow/settings/actions/runner-groups`)에서 그 group의 repository access를 `Selected repositories`(이 저장소만)로 둔다. plan이 허용하면 workflow 접근도 ref 단위로 제한한다.
    - YAML gate를 지금 넣을 수 없게 하는 충돌이 있다. 다음 셋이 서로 맞물린다.
      - `kvm`에 event gate를 넣으면 PR에서 `kvm`이 skip된다. 그러면 `Required native KVM proof`가 skip을 실패로 받으므로 모든 PR이 red가 된다.
      - 이 규칙의 마지막 조항은 그 aggregator가 skip을 통과로 받도록 바꾸는 것을 금지한다.
      - `test_ci_test_jobs_start_without_a_gate_job_in_front`는 `kvm`의 `if`를 `vars.PALIMPSEST_KVM_ENABLED == 'true'`로 정확히 고정한다. 그래서 gate를 넣으면 계약이 실패한다.
    - 해소 방안은 소유자 결정 목록에 있다. 한 예로, KVM proof를 push 전용으로 두고 PR에서 `Required native KVM proof`가 무엇을 뜻할지 소유자가 정한다. PR에서만 event 조건으로 skip을 받게 하거나, PR의 required check에서 빼는 방법이 있다. 결정되면 gate, aggregator verdict, 계약, 이 규칙을 같은 변경에서 바꾼다.
    - `release.yml` 쪽 노출도 있다. `kvm-proof`에는 job-level `permissions`가 없어 workflow-level `contents: write`와 `id-token: write`를 받는다. runner는 persistent하다. 앞선 PR 실행이 runner에 남긴 state(예: runner-state volume의 tool cache나 작업 디렉터리)를 뒤의 release 실행이 쓰면, PR 코드가 그 job의 token에 닿을 수 있다. 그래서 PR 노출의 영향은 `Test` job 하나보다 크다.
    - 2026-09-24 읽기 전용 조회에서 API가 나열한 `pull_request` 실행 22건은 모두 이 저장소의 branch에서 왔고, fork에서 온 실행은 나열되지 않았다. KVM runner에서 실행된 run `35600862976`(`dev`)과 `35600812317`·`35029001178`(`codex/oci-root-phase1`)도 같은 저장소 branch의 PR 실행이다.
    - 따라서 fork 코드 노출은 잠재 위험이다. `first_time_contributors`에서는 이전에 merge된 기여가 있는 외부 contributor의 fork PR이 승인 없이 persistent KVM runner에서 실행될 수 있다. 같은 저장소 PR도 `pull_request` 코드다. 현재 `kvm` job은 `pull_request`에서도 실행되므로 이 규칙의 제목을 아직 충족하지 않는다.
    - 완화 조치는 [인계 문서](docs/development-handoff.md) `명시적 승인 대기 — 실행 금지` 절의 2번이며 소유자가 결정한다. 같은 문서 `CI critical-path checkpoint (2026-09-24)` 절의 승인 대기 1과 같은 항목이다. 명시적 승인 전에는 설정을 바꾸지 않는다.
    - 소유자 결정 전에는 `Required native KVM proof`가 skip을 통과로 받도록 바꾸지 않는다. 위 충돌의 해소가 결정되면 그 결정을 따른다.
11. **CI 형태는 계약 테스트로 고정한다.**
    - `tests/unit/test_test_lanes.py`는 다음을 검사한다.
      - portable matrix 두 개의 형태
        - job key는 정확히 `name`·`runs-on`·`strategy`·`steps`이다. job-level `if`·`env`·`defaults`·`continue-on-error`가 없어야 한다.
        - runner(`ubuntu-latest`·`macos-15`)를 고정한다.
        - `strategy` key는 `fail-fast`·`matrix`·`max-parallel`만 허용한다. `matrix`는 정확히 `{shard: [1..N]}`이어야 하므로 `exclude`·`include`를 쓸 수 없다.
        - `fail-fast: false`여야 하고, `max-parallel`은 없거나 N 이상이어야 한다.
        - step 목록 전체를 정확히 고정한다. 여기에는 action 버전과 `with`, shard 명령 문자열과 `--shard …/N` 분모가 포함된다. 따라서 step `if`·`shell`·`env`, checkout `ref`, `$GITHUB_ENV`에 쓰는 추가 step이 들어갈 수 없다.
        - workflow-level `env`가 없어야 한다.
      - aggregator 세 개의 이름·`if: always()`·정확한 `needs`
      - aggregator마다 하나뿐인 verdict step의 `env`(각 `needs.<dep>.result`, KVM은 `vars.PALIMPSEST_KVM_ENABLED`도)와 성공만 받는 정확한 `run` 문자열. verdict step과 aggregator job에는 `shell`·`defaults`·`continue-on-error`가, workflow에는 `defaults`·`env`가 없어야 한다.
      - `test.yml`의 모든 job(11개)의 정확한 job key 집합. job id 집합도 계약의 표와 같아야 하므로 새 job은 표에 key 집합과 함께 추가해야 한다. 어느 job에도 job-level `env`·`permissions`·`continue-on-error`가 없고, `if`는 aggregator와 `kvm`만 가진다. `defaults`는 `hub`만 가지며 값이 정확히 `{run: {working-directory: hub}}`이어야 한다. 그래서 `defaults.run.shell: "true {0}"`을 넣을 수 없다.
      - `test.yml`의 top-level key는 `name`·`on`·`permissions`·`jobs`뿐이고, `permissions`는 정확히 `{contents: read}`이다. 그래서 workflow `env`·`defaults`·`concurrency`와 write token이 들어갈 수 없다. 규칙 10의 노출 분석은 `kvm`이 읽기 전용 token을 받는다고 전제한다.
      - `test.yml`의 모든 step에 `shell`·`continue-on-error`가 없고, step `if`는 `always()`뿐이어야 한다. `always()`는 step을 건너뛰지 않으며 upload·cleanup step이 쓴다. `kvm` proof step도 이 규칙에 들어간다.
      - 테스트 job 앞에 gate job이 없는지, aggregator 밖의 job-level `if:`가 `kvm`의 `vars.PALIMPSEST_KVM_ENABLED == 'true'`뿐인지
      - 모든 workflow에서 hosted label 허용 목록(`ubuntu-latest`·`ubuntu-24.04`·`macos-15`)에 없는 runner를 쓰는 job이 `test.yml`의 `kvm`과 `release.yml`의 `kvm-proof`뿐인지. self-hosted runner는 `ubuntu-kvm` 같은 임의 label을 가질 수 있으므로 prefix로는 판정하지 않는다. 새 hosted image는 허용 목록에 정확한 label로 추가한다. `runs-on`의 문자열·목록·mapping(`group`·`labels`) 형태를 모두 검사하고, `runs-on`이 없으면 비-hosted로 본다.
      - reusable workflow를 부르는 job(`uses:`)이 하나도 없는지. `test.yml`을 부르는 job은 호출자 trigger로 self-hosted `kvm` job을 물려받기 때문이다.
      - `test.yml`의 trigger가 정확히 `workflow_call`, `main`·`dev` push, `main`·`dev` 대상 `pull_request`인지(`pull_request_target` 없음). 또 `release.yml`이 `v*` tag push 전용인지. 규칙 10의 노출 분석은 이 trigger 집합을 전제로 한다.
      - 어느 workflow도 `pull_request_target`·`workflow_run` trigger를 쓰지 않는지. 허용 목록은 비어 있다. 두 trigger는 fork PR이 계기여도 base 저장소의 token과 secret으로 실행된다. 그래서 PR head checkout이나 PR artifact를 쓰면 hosted runner에서도 PR 코드가 쓰기 권한을 얻는다. `on`의 문자열·목록·mapping 형태를 모두 읽는다.
    - 고정하지 않는 것도 있다.
      - shard job이 아닌 job(`checks`, `hub`, proof job)의 step 내용. 즉 step `env`나 `$GITHUB_ENV`에 쓰는 추가 step은 고정하지 않는다. `kvm` proof는 evidence가 없으면 upload step의 `if-no-files-found: error`로 실행 중 실패한다.
      - workflow 밖의 무력화. `pyproject.toml`의 `addopts`나 conftest hook은 규칙 5에 적은 대로 막지 않는다.
      - `test.yml` 밖 workflow의 job 형태. `test_test_lanes.py`는 trigger, runner, reusable 호출만 본다. development-package도 예외가 아니다. 그 workflow의 자체 계약이 검사하는 항목과 검사하지 않는 항목은 아래 `test_development_package_workflow.py` 항목에 적었다.
    - `tests/unit/test_oci_convert_security.py`는 `oci-fs-proof`부터 `unit-macos` 직전까지 job-level `if:`가 없는지 텍스트로 검사한다. 그러므로 이 구간의 job 순서를 바꾸지 않는다.
    - `tests/unit/test_development_package_workflow.py`가 `development-package.yml`에서 검사하는 것은 다음뿐이다.
      - trigger가 `workflow_dispatch`와 허용 branch 세 개의 push인지, workflow `permissions`가 `{contents: read}`인지, ref 단위 `concurrency`와 job id 집합(`verify`·`publish`)
      - `verify`: job-level `permissions`가 없는지, job `if`에 허용 branch마다 ref 비교가 들어 있는지, step `run` 문자열을 이은 text에 필수 문자열 여섯 개가 들어 있는지. 모두 포함 여부만 본다. 그래서 `if`에 `|| true`를 더하거나 명령을 `echo`로 감싸도 통과한다. `qualification` lane과 `ruff` step은 필수 문자열에 없다.
      - `publish`: `needs: verify`, `permissions == {contents: write}`, 정확한 `uses` 목록, `sha256sum --check SHA256SUMS`를 담은 `run`이 helper `run`보다 앞에 있는지(순서만 본다), helper `run`의 `--repository`·`--sha`·`--dist-dir` 인자, 금지 문자열(`gh api`, `gh release create`, `--clobber` 등)
      - 같은 파일은 `release.yml`의 `v*` tag trigger와 PyPI `publish`의 `needs`·`if`를 텍스트로 확인한다.
    - 따라서 development-package의 두 job 모두 job key 집합, step `if`·`shell`·`continue-on-error`·`env`, 추가 step을 고정하지 않는다. job-level `if`도 `verify`의 포함 검사 외에는 보지 않는다. `verify`의 테스트 step을 건너뛰거나 그 실패를 무시하게 바꿔도 계약은 통과한다. 그러면 `publish`가 `contents: write`로 검증되지 않은 SHA별 prerelease를 만들 수 있다. 이 workflow를 바꿀 때는 이 공백을 리뷰에서 직접 확인한다. 2026-09-24 최종 검토의 임시 복사본 변형 9가지(`verify` step `continue-on-error`·`if: false`·`shell: 'true {0}'`, `verify` job `continue-on-error`, checksum step `continue-on-error`·`if: false`, `publish` job `if: always()`, 명령 `echo` 감싸기, `verify` `if`의 `|| true`)가 모두 이 계약을 통과했다.
    - 새 CI 불변식은 새 파일을 만들기보다 이 파일들을 확장한다. 새 test 파일은 `scripts/test_lanes.py`에 분류해야 하기 때문이다.
12. **지속 개선.**
    - CI를 바꾸는 변경에는 전후 실측을 첨부한다.
    - 다음 중 하나라도 해당하면 1번 절차로 다시 측정하고 가장 늦게 끝나는 job부터 개선한다.
      - `Test` 크리티컬 패스 중앙값이 마지막으로 기록된 기준보다 20% 이상 나빠진다.
      - portable node 수가 크게 늘어난다. 기준은 `60fa42f`의 CI run `35826465548` "Lane shard" 줄의 선택 node 합계 6,081이다(Linux 6 shard와 macOS 4 shard 합계가 같다; pass 수가 아니다). 그 뒤 CI 형태 계약이 node를 더했다. 로컬 기준으로 `2e37538`은 6,084, review 1차 반영 뒤는 6,088, review 2차 반영 뒤는 6,089, review 3차 반영 뒤는 6,091(`--collect-only`)이다.
      - 새 테스트 계층이나 job을 추가한다.
    - `.github/**`와 `AGENTS.md`는 architecture digest 범위에 들어간다. 이 파일을 바꾸면 Maintenance summary를 갱신하고 `--stamp`와 `--staged` 검사를 거친다.
