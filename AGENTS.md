# Palimpsest Local contributor rules

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
