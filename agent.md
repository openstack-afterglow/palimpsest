# 개발 재개 진입점

이 문서는 사람과 에이전트가 다음 작업을 찾기 위한 짧은 안내다.
자동으로 읽히는 저장소 규칙의 정본은 [AGENTS.md](AGENTS.md)이며,
이 파일은 그 규칙을 대체하거나 승인 범위를 넓히지 않는다.

## 읽는 순서

1. [README의 Development](README.md#development): 환경 준비와 테스트 진입점.
2. [AGENTS.md](AGENTS.md): 작업·검토·문서 갱신 규칙.
3. [개발 인계 문서](docs/development-handoff.md): 지금까지 한 일, 검증 수준, 남은 문제와 우선순위.
4. [ARCHITECTURE.md](ARCHITECTURE.md): 현재 source 계약과 수정할 영역의 상세 문서.

## 다음 세션의 첫 작업

인계 문서의 재개 절차에 따라 Git의 staged/unstaged 변경을 확인하고
승인 대기 두 건부터 구분한다. 2026-09-14 기준 내부 PCI 사전 점검기는
로컬 구현·검토·선별 테스트까지 완료됐지만 commit/push와 서버 점검은
실행되지 않았다. 실제 GPU passthrough나 CUDA 성공으로 해석하지 않는다.

전송 승인이 없는 동안에는 차단된 원격 작업을 재시도하지 않고,
사용자 요청 범위 안에서 가능한 로컬 검토·개발·선별 검사를 진행한다.
승인 뒤에도 서버의 최신 PCI/IOMMU/driver/사용 현황을 먼저 확인하고,
그 결과를 보고 GPU 재할당의 별도 구현·운영 범위를 정한다.
OpenStack GPU instance 자체를 OCI rootfs로 부팅한다는 선택은 유지하며,
로컬 KVM PCI 실험을 중첩 VM 지원 또는 Nova host 조작 승인으로 확대하지 않는다.

## 재개 요청 예시

> README.md와 AGENTS.md, docs/development-handoff.md, ARCHITECTURE.md를 읽고
> 실제 Git 상태와 대조해 이어서 진행해. 승인 대기는 그대로 유지하고,
> 현재 가능한 첫 작업과 검증 범위를 먼저 알려줘.
