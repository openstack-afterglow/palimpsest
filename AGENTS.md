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
