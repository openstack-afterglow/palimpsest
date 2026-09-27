# 개발 재개 진입점

이 문서는 사람과 에이전트가 다음 작업을 찾기 위한 짧은 안내다. 자동 진입점 [AGENTS.md](AGENTS.md)와 [contributor workflow spec](openspec/specs/contributor-workflow/spec.md), [CI safeguards spec (rules 1–12)](openspec/specs/ci-safeguards/spec.md)를 모두 읽는다. 이 파일은 규칙을 대체하거나 승인 범위를 넓히지 않는다.

## 읽는 순서

1. [README의 Development](README.md#development): 환경 준비와 테스트 진입점.
2. [AGENTS.md](AGENTS.md) 및 위 두 specs: 작업·검토·문서 갱신과 CI 안전 규칙.
3. [개발 인계 문서](docs/development-handoff.md): 지금까지 한 일, 검증 수준, 남은 문제와 우선순위.
4. [ARCHITECTURE.md](ARCHITECTURE.md): 현재 source 계약과 수정할 영역의 상세 문서.

## 다음 세션의 첫 작업

인계 문서의 재개 절차에 따라 branch/HEAD와 staged/unstaged 변경을 확인하고 승인 대기부터 구분한다. 2026-09-14 기준 PCI 사전 점검기의 로컬 구현·검토·선별 테스트 기록은 당시 상태이며 현재 승인 또는 GPU passthrough/CUDA 성공이 아니다.

원격 게시·private helper 전송·서버 설치와 실행·GPU/VM mutation은 각각 정확한 대상과 별도 명시적 승인이 필요하다. 승인 전에는 차단된 작업을 우회하지 않고 가능한 로컬 범위만 진행한다. 승인 뒤에도 최신 PCI/IOMMU/driver/사용 현황과 exact SHA를 읽기 전용으로 확인하고 별도의 재할당 범위를 결정한다. OpenStack GPU instance를 OCI rootfs로 부팅한다는 선택을 로컬 KVM 중첩 지원이나 Nova host 조작 승인으로 확대하지 않는다.

## 재개 요청 예시

> README.md와 AGENTS.md, docs/development-handoff.md, ARCHITECTURE.md를 읽고
> 실제 Git 상태와 대조해 이어서 진행해. 승인 대기는 그대로 유지하고,
> 현재 가능한 첫 작업과 검증 범위를 먼저 알려줘.
