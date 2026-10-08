# CI safety and performance requirements

## Purpose

This specification carries the 2026-09-27 Nova cutover and the detailed CI obligations from the root guidance. Earlier CI and self-hosted-runner checkpoints in [the handoff](../../../docs/development-handoff.md) describe their dated states; the trusted-ref, hosted-only policy below governs current changes.

## Requirements

### Requirement: Preserve measured CI and protected execution boundaries
CI changes MUST retain comparable measured critical paths, hosted-only untrusted PR jobs, verified shard execution and successful publication verification. Native qualification SHALL be explicitly opt-in using canonical lowercase `PALIMPSEST_KVM_ENABLED=true` or `false`: enabled requires successful native proof; disabled permits an intentionally skipped native job without claiming qualification. Missing/non-boolean flags, native failures/cancellation/missing outcomes, unsuccessful release verification, cancelled workflows and untrusted publication contexts MUST NOT authorize publication. Test's actual Bash verdict SHALL compare literal flags; Release job conditions retain GitHub's case-insensitive comparison semantics and MUST NOT be claimed to reject uppercase boolean aliases. Separate owner approval MUST precede native runner exposure, native opt-out, release bypasses or remote resource mutation; the 2026-10-07 owner request authorizes this explicit native opt-out only.

#### Scenario: Untrusted PR requests native proof
- **WHEN** a fork or other untrusted PR reaches the test workflow
- **THEN** it runs only hosted validation, with no privileged runner or publication credential exposure

#### Scenario: CI improvement is proposed
- **WHEN** a workflow change claims a shorter critical path
- **THEN** before/after comparable runs are measured and actual median, p90 and sample size are recorded rather than a projection claimed as a result

#### Scenario: Owner explicitly disables native qualification
- **WHEN** a trusted dev/main push has literal flag `false` and native job result `skipped`
- **THEN** the native policy verdict succeeds while reporting that no native KVM proof ran
- **AND** all ordinary validation remains required and the native protected environment is not requested

#### Scenario: Disabled native dependency precedes a release
- **WHEN** a non-cancelled trusted repository v-tag push has `verify=success`, flag `false` and native result `skipped`
- **THEN** root publication is allowed despite that intentionally skipped dependency
- **AND** its notice states that the release has no native KVM proof

#### Scenario: Enabled or invalid qualification cannot pass without proof
- **WHEN** flag is `true` without native `success`, flag is missing/non-boolean, disabled native result is not `skipped`, or release verification is not `success`
- **THEN** the policy rejects the result and release publication cannot run

#### Scenario: Cancelled or untrusted publication
- **WHEN** the workflow is cancelled, the repository is not the trusted repository, the event is not push, or the ref is not a v-tag
- **THEN** root publication cannot run regardless of native or verification outcomes

### Requirement: Complete GitHub release after permitted root publication

The final GitHub Release job SHALL require a non-cancelled workflow and successful root publication. An explicitly disabled native dependency SHALL NOT suppress completion after the existing trusted-ref, verification, native-policy and protected publisher gates have permitted root publication. No failed, skipped, cancelled or missing publisher result SHALL authorize GitHub Release creation.

#### Scenario: Native opt-out with successful publication

- **WHEN** native qualification was explicitly disabled and skipped, verification succeeded and permitted root publication succeeded
- **THEN** the GitHub Release job is eligible to attach the same verified root distributions
- **AND** no native qualification is claimed

#### Scenario: Publication unavailable or workflow cancelled

- **WHEN** root publication failed, was cancelled or skipped, has no result, or the workflow is cancelled
- **THEN** the GitHub Release job is ineligible

#### Scenario: Repair an existing immutable release

- **WHEN** a completed permitted tag run published distributions to PyPI but its final GitHub job was incorrectly skipped
- **THEN** an authorized repair attaches only hash-matched tag-run distributions without moving the tag, overwriting assets or republishing PyPI

## Reference

## CI 파이프라인 성능 규정

근거는 2026-09-24의 읽기 전용 실측이다. `Test` workflow(`.github/workflows/test.yml`)가 현재의 19-job 형태로 완료한 실행 21건(2026-09-15~09-23; 이 형태는 `1a23f9c`에서 dev에 들어왔다)을 측정했다. 크리티컬 패스는 실행 생성 시각(재실행은 `run_started_at`)부터 마지막 job 종료까지로 잡았고, 중앙값 325초·p90 781초였다. 단일 KVM runner에서 직렬화된 2026-09-21 12:39–12:43 burst 8건을 빼면 13건 기준 중앙값 303초·p90 346초였다. 마지막으로 끝난 job은 21건 중 19건이 `Unit tests (macOS 15)` aggregator였다.

병목은 세 가지였다.

- `portable-macos`의 `max-parallel: 2`가 두 번째 wave를 만들었다. 3/4·4/4 shard의 대기 중앙값이 151·170초였다.
- `portable-linux`의 `max-parallel: 3`도 두 번째 wave를 만들었다. 4–6 shard 대기가 91–105초였다.
- `Native KVM stage-1 proof`가 단일 self-hosted runner `pieroot-server-palimpsest-kvm`에서 직렬화된다. 실행 140초, 대기 중앙값 142초·p90 626초였다.

2026-09-24 `ci-perf` 변경은 두 matrix의 `max-parallel`을 제거했다. dev/PR 크리티컬 패스 약 155–170초라는 기대치는 KVM job 약 140초를 하한으로 한 추정이며, push 뒤 재측정 전까지 효과로 주장하지 않는다. CI를 바꾸는 모든 변경은 아래 규칙을 따른다.

Rules 1–12 below remain mandatory for CI changes, including their stated
coverage gaps and pending owner decisions. Historical measurements are dated
baselines, not proof of a new run. The explicit native policy in Requirements
above is authoritative: enabled requires proof; deliberate disabled/skipped
permits policy success with no qualification. Hosted-only PR execution and
fail-closed enabled-native verdicts cannot be traded for shorter wall-clock time.

1. **측정 먼저, 추정 금지.**
   - CI를 바꾸기 전과 후에 최근 20회 이상 `Test` 실행의 job·step 시간을 `gh run list --workflow test.yml`와 `gh api repos/openstack-afterglow/palimpsest/actions/runs/<id>/jobs`로 수집한다.
   - 크리티컬 패스 중앙값과 p90을 commit 본문, [인계 문서](../../../docs/development-handoff.md), [`ARCHITECTURE.md`](../../../ARCHITECTURE.md) Maintenance summary에 남긴다. 이 spec은 2026-09-25의 OpenSpec 부재 기록 이후의 문서 이전이며 과거 실행 결과를 갱신하지 않는다.
   - 절감은 합산되지 않는다. 가장 늦게 끝나는 job부터 줄이고, 효과는 실제 CI 전후 수치로만 주장한다.
   - 표본 수를 채우기 위해 강제 workflow 실행으로 측정값을 만들지 않는다.
2. **목표 지표를 먼저 정한다.**
   - 이 저장소는 public이고 GitHub-hosted runner 시간이 무료이므로 목표는 wall-clock이다.
   - org `openstack-afterglow`의 Free plan 동시성 한도(hosted 20 job, macOS 5 job)는 lumen·openstack-afterglow·drover·waygate·afterglow-crypto와 공유한다. macOS runner를 쓰는 저장소는 이곳뿐이다.
   - 2026-09-24 정의 기준 hosted 시작 job15개·전체18개는 self-hosted native job을 제외한 역사적 수다. 2026-09-27 cutover는 native orchestration도 `ubuntu-24.04`로 옮겼다. PR은 native job/aggregator를 실행하지 않고, trusted `main`·`dev` push는 flag=true일 때만 native job1개를 더 요청한다. Environment 승인 대기와 repo-wide native concurrency 대기도 wall-clock에 포함한다.
   - `main`·`dev` push는 `hub-docker.yml`의 `test`와 `development-package.yml`의 `verify`, PR은 Hub `test`를 별도로 시작한다. Workflow 정의상 trusted push는 native enabled일 때 시작 hosted job18개, disabled일 때17개(macOS4개), PR은16개(macOS4개)다. 실제 scheduling/승인 대기 실측과 구분한다.
   - 겹치는 dev/main 실행은 hosted20개·macOS5개 한도를 넘을 수 있다. 단독 실행과 겹친 실행을 나누어 측정한다.
3. **게이트 job을 테스트 job 앞에 두지 않는다.**
   - `Lint, manifests, and package`(`checks`)는 portable shard의 `needs:`로 걸지 않고 병렬로 실행한다. 그 결과는 `Pure contracts (Python 3.12)` aggregator에서만 합친다.
   - 의존 job verdict는 `Pure contracts (Python 3.12)`, `Unit tests (macOS 15)`, `Native KVM qualification policy` 세 aggregator가 담당한다. 앞의 둘은 `if: always()`, native policy aggregator는 `always()`와 아래 trusted repository/push/ref allowlist를 함께 적용한다. PR에서 native skip을 success로 바꾸지 않는다. Trusted push의 명시적 false/skipped 정책 통과도 native qualification으로 표시하지 않는다.
   - image·package **발행(push)과 배포**는 `Test` workflow 전체 결과로 게이팅한다. 아무것도 발행하지 않는 PR 검증 build는 테스트와 병렬로 돌려도 된다. `hub-docker.yml`의 `pull_request` 실행은 이 규칙을 지킨다. `build-and-push`가 두 image를 `push: false`로 build만 하기 때문이다.
   - 다음 세 발행 경로는 `Test` 전체 결과를 기다리지 않는다. 정본 규칙 3의 기존 예외다. 모두 테스트 크리티컬 패스 밖에 있고, 바꾸면 발행 의미가 달라진다. 그래서 소유자 결정 없이 바꾸지 않는다. 결정 항목은 [인계 문서](../../../docs/development-handoff.md) `CI critical-path checkpoint (2026-09-24)` 절의 승인 대기 목록에 있다.
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
   - 이 계약은 workflow 밖의 무력화를 막지 않는다. 예를 들어 `pyproject.toml`의 `addopts = "--collect-only"`나 conftest hook이 실행을 막아도 모든 shard는 "Lane shard" 줄을 출력하고 exit 0으로 끝난다. 2026-09-24에 `PYTEST_ADDOPTS=--collect-only`로 `run portable --shard 1/256`을 실행해 보니 selected 24 node를 출력하고 한 건도 실행하지 않은 채 exit 0이었다. 실행된 수를 selected 수와 대조하는 실행 중 검사라면 이런 무력화를 잡는다. 그 검사를 추가할지, 이 차이를 받아들일지는 소유자가 결정한다. 결정 항목은 [인계 문서](../../../docs/development-handoff.md) `CI critical-path checkpoint (2026-09-24)` 절의 승인 대기 목록에 있다.
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
10. **보안: PR은 hosted-only, native는 보호된 trusted ref의 일회성 Nova VM에서 실행한다.**
    - `Test.kvm`은 repository `openstack-afterglow/palimpsest`, event `push`, ref `refs/heads/dev` 또는 `refs/heads/main`, `PALIMPSEST_KVM_ENABLED == 'true'`를 모두 만족해야 한다. `kvm-required`는 같은 repository/event/ref와 `always()`를 요구하며 `true/success` 또는 소유자가 허용한 명시적 `false/skipped`만 받는다. 누락·잘못된 flag와 failure/cancel/빈 결과, enabled skip 및 disabled success는 거부한다. 후자의 정책 통과는 proof 미실행을 알리며 native qualification이 아니다.
    - PR required checks는 `Pure contracts (Python 3.12)`, `Unit tests (macOS 15)`, `Hub tests, lint, and build`, `OCI filesystem proof (privileged Linux)`, `Local OCI image product build`, `Guest stage-1 binary (Linux x86_64)` 여섯 개이며 strict=true다. Native aggregator를 PR required 목록에 넣거나 PR에 native success를 만들지 않는다. 기존 main deletion/non-fast-forward 보호를 보존한다.
    - `Test.kvm`과 tag-only `release.kvm-proof`는 `ubuntu-24.04`와 보호 environment `palimpsest-native-kvm`을 사용한다. Environment는 branch `dev`, branch `main`, tag `v*`만 허용하고 required reviewer `jung-geun`, admin bypass=false를 유지한다. 서버 설정으로 보호를 확인하기 전 secret을 넣거나 native를 활성화하지 않는다.
    - GitHub-hosted orchestration이 전용 member-only application credential로 새 CI project에 run당 최대1대 Nova VM(2vCPU/8GiB/boot20GiB)을 만든다. CI project quota는 instances1/cores2/ram8192/volumes1/gigabytes20/floatingip0이다. Admin credential, 후보 Hub host/state, 기존 domain을 사용하지 않는다. VM을 GitHub self-hosted runner로 등록하지 않으며 cloud/GitHub credential을 VM에 전달하지 않는다.
    - Helper `scripts/run_native_kvm_openstack.py`는 source/kernel/config pins, project와 owner metadata, console host-key, KVM API12, mounted filesystem gate, 정본 stage-1 proof와 증거 회수를 검증한다. Manifest의 exact-owned server/volume/port/SG/keypair만 회수하고 부재를 확인한다. Deadline, signal, cleanup 실패는 gate 실패이며 ownership 불일치는 삭제 거부다. SSH ingress는 job egress IPv4/32만 허용한다.
    - 두 native job의 repo-wide concurrency group은 `palimpsest-native-kvm`, cancel-in-progress=false다. GitHub가 pending job을 대체 취소하면 cancelled gate이지 성공이 아니다. 강제 runner 상실 뒤 수동 manifest 회수 필요성을 숨기지 않는다.
    - Release 기본 permissions는 contents:read다. PyPI publish만 id-token:write, GitHub release만 contents:write를 갖는다. Publication은 `!cancelled()`·trusted repository·push·v* tag·verify=success와 위 두 상태를 명시적으로 요구한다. Native 실패/cancel/빈 결과, enabled skip과 잘못된 flag는 발행을 차단한다. 소유자가 선택한 false/skipped 발행에는 native proof가 없다고 알린다. `hub-docker.yml`과 development-package 발행 예외는 규칙3 그대로다.
    - 기존 repository runner21은 승인된 stop 이후 offline으로 보존하며 재시작·재등록하지 않는다. YAML 수정만으로 PR-editable workflow의 self-hosted 노출을 제거했다고 주장하지 않는다. 이 cutover의 source/수동 proof와 원격 GitHub 적용·실행은 별개이며 commit/push/tag/발행은 여전히 별도 승인이다. 역사적 노출·조치는 인계의 날짜별 기록을 참고한다.
11. **CI 형태는 계약 테스트로 고정한다.**
    - `tests/unit/test_test_lanes.py`는 다음을 검사한다.
      - portable matrix 두 개의 형태
        - job key는 정확히 `name`·`runs-on`·`strategy`·`steps`이다. job-level `if`·`env`·`defaults`·`continue-on-error`가 없어야 한다.
        - runner(`ubuntu-latest`·`macos-15`)를 고정한다.
        - `strategy` key는 `fail-fast`·`matrix`·`max-parallel`만 허용한다. `matrix`는 정확히 `{shard: [1..N]}`이어야 하므로 `exclude`·`include`를 쓸 수 없다.
        - `fail-fast: false`여야 하고, `max-parallel`은 없거나 N 이상이어야 한다.
        - step 목록 전체를 정확히 고정한다. 여기에는 action 버전과 `with`, shard 명령 문자열과 `--shard …/N` 분모가 포함된다. 따라서 step `if`·`shell`·`env`, checkout `ref`, `$GITHUB_ENV`에 쓰는 추가 step이 들어갈 수 없다.
        - workflow-level `env`가 없어야 한다.
      - aggregator 세 개의 key·정확한 `needs`와 portable 두 개의 외부 required check 이름; portable은 `always()`, native verdict는 규칙10의 trusted repository/event/ref와 `always()`를 함께 요구한다. Native 표시 이름의 문구만 고정하는 검사는 두지 않는다.
      - aggregator verdict의 dependency env와 실패 우회 금지. Native verdict는 실제 shell을 실행해 `true/success`·`false/skipped`만 통과하고 실패/cancel/빈 결과, 잘못된 flag 및 나머지 조합을 거부하는지 검사한다.
      - `test.yml`의 모든 job(11개)의 정확한 key 집합. 어느 job에도 job-level `env`·`permissions`·`continue-on-error`가 없고, `if`는 aggregator와 `kvm`만 가진다. Native job만 보호 environment·직렬 concurrency·timeout을 추가로 가진다. `defaults`는 `hub`의 `{run: {working-directory: hub}}`만 허용한다.
      - `test.yml`의 top-level key는 `name`·`on`·`permissions`·`jobs`뿐이고, `permissions`는 정확히 `{contents: read}`이다. 그래서 workflow `env`·`defaults`·`concurrency`와 write token이 들어갈 수 없다. 규칙 10의 노출 분석은 `kvm`이 읽기 전용 token을 받는다고 전제한다.
      - `test.yml`의 모든 step에 `shell`·`continue-on-error`가 없고, step `if`는 `always()`뿐이어야 한다. `always()`는 step을 건너뛰지 않으며 upload·cleanup step이 쓴다. `kvm` proof step도 이 규칙에 들어간다.
      - 테스트 job 앞에 gate job이 없는지, native 실행과 verdict의 positive repository/event/ref allowlist가 PR·fork·임의 branch·workflow_call을 실제 조건 평가에서 거부하는지
      - 모든 workflow의 runner가 hosted label 허용 목록(`ubuntu-latest`·`ubuntu-24.04`·`macos-15`)에 포함되는지. 비-hosted 예외 목록은 비어 있다. `runs-on`의 문자열·목록·mapping을 검사하고, group 지정·label 누락도 거부한다.
      - reusable workflow를 부르는 job(`uses:`)이 없는지. 새 호출 경로는 trigger와 credential 경계를 별도 검토한 뒤 계약에 반영한다.
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
      - 같은 파일은 `release.yml`의 tag-only trigger, publication 최소권한, native environment·concurrency·credential 범위, 실제 shell의 HTTPS kernel 입력 및 evidence/cleanup 누락 거부를 검사한다. 실제 publication 조건은 enabled/disabled/invalid flag와 verify/native outcomes, trusted repository/event/ref 및 cancellation 조합으로 평가한다.
    - 따라서 development-package의 두 job 모두 job key 집합, step `if`·`shell`·`continue-on-error`·`env`, 추가 step을 고정하지 않는다. job-level `if`도 `verify`의 포함 검사 외에는 보지 않는다. `verify`의 테스트 step을 건너뛰거나 그 실패를 무시하게 바꿔도 계약은 통과한다. 그러면 `publish`가 `contents: write`로 검증되지 않은 SHA별 prerelease를 만들 수 있다. 이 workflow를 바꿀 때는 이 공백을 리뷰에서 직접 확인한다. 2026-09-24 최종 검토의 임시 복사본 변형 9가지(`verify` step `continue-on-error`·`if: false`·`shell: 'true {0}'`, `verify` job `continue-on-error`, checksum step `continue-on-error`·`if: false`, `publish` job `if: always()`, 명령 `echo` 감싸기, `verify` `if`의 `|| true`)가 모두 이 계약을 통과했다.
    - `tests/unit/test_native_kvm_openstack.py`는 private manifest와 가짜 cloud state로 다른 project/owner·ID 충돌 삭제 거부, 생성 응답 유실, timeout/signal 이후 회수, Cinder copy-state 대기, SDK 예외 redaction, 실제 child-process secret 차단, canonical kernel/config evidence 불일치를 검사한다. 이는 실제 Nova/KVM proof의 대체물이 아니다.
    - 새 CI 불변식은 새 파일을 만들기보다 이 파일들을 확장한다. 새 test 파일은 `scripts/test_lanes.py`에 분류해야 하기 때문이다.
12. **지속 개선.**
    - CI를 바꾸는 변경에는 전후 실측을 첨부한다.
    - 다음 중 하나라도 해당하면 1번 절차로 다시 측정하고 가장 늦게 끝나는 job부터 개선한다.
      - `Test` 크리티컬 패스 중앙값이 마지막으로 기록된 기준보다 20% 이상 나빠진다.
      - portable node 수가 크게 늘어난다. 기준은 `60fa42f`의 CI run `35826465548` "Lane shard" 줄의 선택 node 합계 6,081이다(Linux 6 shard와 macOS 4 shard 합계가 같다; pass 수가 아니다). 그 뒤 CI 형태 계약이 node를 더했다. 로컬 기준으로 `2e37538`은 6,084, review 1차 반영 뒤는 6,088, review 2차 반영 뒤는 6,089, review 3차 반영 뒤는 6,091(`--collect-only`)이다.
      - 새 테스트 계층이나 job을 추가한다.
    - `.github/**`와 `AGENTS.md`는 architecture digest 범위에 들어간다. 이 파일을 바꾸면 Maintenance summary를 갱신하고 `--stamp`와 `--staged` 검사를 거친다.

#### Scenario: Fork PR attempts native proof

- **GIVEN** a PR or fork-origin event that does not meet the trusted repository/push/ref allowlist
- **WHEN** the `Test` workflow evaluates native execution and its aggregator
- **THEN** neither runs or reports native success for that PR, while the six strict hosted PR required checks remain required.

#### Scenario: Trusted native run has incomplete ownership evidence

- **GIVEN** a reviewed `dev` or `main` push with enabled native proof, protected-environment approval and a dedicated quota-limited Nova VM
- **WHEN** evidence is missing, resource ownership differs, the job is cancelled, or exact-owned cleanup cannot be confirmed
- **THEN** the native verdict fails, unknown resources are not deleted, release publication is blocked, and manual manifest recovery remains explicit when required.

#### Scenario: CI performance improvement claim

- **GIVEN** a workflow change that removes a scheduling bottleneck
- **WHEN** its effect is documented
- **THEN** before/after completed `Test` job and step measurements cover at least 20 runs each, report median and p90 including environment/concurrency waits, and distinguish observed values from estimates without triggering extra runs merely to fill the sample.

#### Scenario: Development package workflow change

- **GIVEN** a workflow edit to the SHA-specific prerelease verification or publication jobs
- **WHEN** existing contract tests pass
- **THEN** review must still inspect unpinned step/job `if`, `continue-on-error`, `shell`, `env`, and additional-step bypasses; passing inclusion-based contracts alone is not publication authorization.
