# 개발 재개 진입점

자동으로 읽히는 짧은 규칙은 [AGENTS.md](AGENTS.md)에 있다. 상세 의무는 [contributor workflow](openspec/specs/contributor-workflow/spec.md)와 [CI safety/performance](openspec/specs/ci-safety-and-performance/spec.md)에 있다. **두 spec 모두** 적용되며 이 진입점은 승인 범위를 넓히지 않는다.

## 읽고 진행할 순서

1. [README Development](README.md#development), [개발 인계](docs/development-handoff.md)의 최신 날짜별 checkpoint와 승인 대기, [ARCHITECTURE.md](ARCHITECTURE.md), 두 spec을 읽는다. 2026-09-14 snapshot 및 2026-09-24 self-hosted CI 기록을 현재 상태나 새 승인으로 취급하지 않는다.
2. `git status --short`, staged/unstaged diff 및 최근 commit을 대조해 다른 사람의 변경·근거·index를 보존한다. 실제 source를 먼저 검토하고 영향 문서를 함께 갱신한다. Astra 계획/관리, Sol 개발, 독립 검토를 따르되 역할이 불가능하면 제약을 보고한다.
3. 영향받은 lane부터 검사한다. Native/ML/GPU는 별도 전제와 승인 아래에서만 실행하며 portable 성공을 실기 성공으로 승격하지 않는다. GitHub 게시·원격 helper 전송·GPU 재할당/driver reset·기존 실패 VM 제거는 각기 명시적 권한이 필요하다.
4. CI에서는 PR hosted-only, trusted ref의 보호 환경·quota 제한 Nova native, offline runner21, 최소 권한·직렬화·fail-closed 정리를 유지한다. 발행 예외와 shard 검증 공백은 소유자 결정 없이 바꾸지 않는다. 문서나 로컬 증거만으로 원격 실행·발행을 승인하지 않는다.
5. 완료 전 architecture marker는 실제 source review 뒤에만 stamp하고 해당 범위 guard를 검사한다. 인계에 실제 SHA·검사·실패·남은 승인을 남기며 비밀이나 가공된 성공 증거를 남기지 않는다.
