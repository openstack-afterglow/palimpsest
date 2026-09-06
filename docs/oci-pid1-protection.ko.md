# OCI VM의 PID 1 보호: 무엇을 보호하며, 왜 Gate 2와 충돌하는가

## 결론

후속 결정: 사용자는 PID 1 보호를 유지하고 Gate 2 검증 기준을 변경하는
방향을 승인했다. [새 root identity 계약](oci-root-proof.md)에 따라 구현과
검증을 진행한다. 아래의 원래 probe 실패는 변경 전 근거이며, 보호를
해제하라는 요청으로 해석하지 않는다.

여기서 PID 1은 **호스트 서버가 아니라 VM 내부의 Palimpsest stage1 관리
프로세스**다. OCI 이미지의 주 명령과 추가 `exec`는 그 자식으로 실행된다.
보호의 목적은 이미지 안의 프로그램이 VM 관리자의 메모리·인증 키·제어
채널·장치·cgroup 관리 권한을 가져가지 못하도록 경계를 두는 것이다.
단순히 PID 1에 대한 `kill`을 막는 옵션 하나를 뜻하지 않는다.

```text
호스트 Palimpsest monitor
  └─ 인증된 lifecycle 채널 ─→ VM 내부 PID 1: Palimpsest stage1
                               ├─ OCI 이미지의 주 명령
                               └─ 추가 exec 명령과 그 자손
```

OCI 레이어와 VM 전용 writable root로 구성한 OverlayFS가 실제 `/`가 된다.
PID 1은 루트 전환 후 자식을 만들며, 앱 쪽에만 일부 관리용 경로를 가리는
별도 mount namespace를 적용한다. 따라서 **“OCI 내용이 실제 `/`”**와
**“이미지 프로그램이 PID 1 또는 무제한 VM 관리자”**는 서로 다른 요구다.
현재는 전자를 제공하지만 후자는 제공하지 않는다.

## 현재 코드에 들어 있는 보호

기준은 공개 exec 검증을 마친 코드이며, 이번 보고 과정에서 보호를 변경하지 않았다.

| 장치 | 실제 적용 | 보호하려는 대상 |
|---|---|---|
| PID 1 non-dumpable | 첫 fork 전에 `PR_SET_DUMPABLE=0`을 설정하고 다시 확인 | 일반적인 ptrace 및 관련 `/proc` 접근, core dump를 통한 관리자 메모리 노출 억제 |
| 앱 capability 제거 | effective/permitted/inheritable/ambient/bounding capability를 비우고 securebits를 고정 | 이미지 User가 UID 0이어도 `CAP_SYS_PTRACE`, `CAP_SYS_ADMIN` 등의 관리자 권한을 주지 않음 |
| 권한 재획득 차단 | `no_new_privs=1` | exec 시 setuid/file capability를 통한 추가 권한 획득 차단 |
| 앱 syscall 필터 | ptrace, process_vm_read/write, pidfd_getfd, mount/setns/unshare, 새 namespace 생성 등 거부 | 메모리·FD·namespace 우회 경로 제한. 전체 syscall allowlist가 아니라 denylist임 |
| 앱 전용 mount view | `/dev`를 여섯 개의 제한된 장치로 대체, PID 1의 fd/fdinfo와 virtio-port 검색 경로를 가림, proc/sys/cgroup view를 read-only로 구성 | 관리자의 virtio 제어 장치, raw disk 접근 경로, cgroup 변경 권한 등을 이미지에 그대로 노출하지 않음 |
| 인증 키·FD 분리 | 최초 앱 fork/격리 확인 후 boot key 생성. 추가 exec fork에서는 상속된 키·제어 버퍼를 지우고 supervisor FD를 닫음 | 이미지 프로그램이 host-guest 제어 메시지의 송신자 또는 VM 종료 결과를 사칭할 위험 감소 |

Parent는 자식을 바로 실행시키지 않고 UID/GID, 보조 그룹, capability,
no_new_privs, seccomp 상태를 확인한 뒤 release barrier를 연다. 실행 결과와
STOP은 기존 인증 채널을 통해 전달하며, 추가 exec의 종료와 VM 자체의
TERMINAL을 구분한다.

구현 위치는 `guest/stage1/init.c`의 `prepare_workload_mount_boundary`
(3731), `prepare_workload_securebits`(3800), `clear_workload_capabilities`
(3823), `install_workload_seccomp`(3861), `verify_workload_isolation_status`
(3950), `wipe_child_control_authority`(4254), 최초 fork 전 dumpable 설정
(4588), 격리 확인 후 bootstrap(4702)이다. 기준 소스는
[검증된 init.c](https://github.com/openstack-afterglow/palimpsest/blob/9ca5c1b2ad6d785ba2911620065259c7e6da70c5/guest/stage1/init.c)다.

Linux의 세부 동작: [PR_SET_DUMPABLE](https://man7.org/linux/man-pages/man2/PR_SET_DUMPABLE.2const.html),
[no_new_privs](https://www.kernel.org/doc/html/latest/userspace-api/no_new_privs.html).

## `/proc/1/root`가 왜 차단되는가

Gate 2의 현재 이미지 probe는 같은 marker를 `/`와 `/proc/1/root` 양쪽에서
읽도록 요구한다. 후자는 단순한 다른 이름의 `/`가 아니다. Linux는 이
경로를 통해 **대상 프로세스의 root 및 mount namespace 관점**을 제공하며,
접근에 `PTRACE_MODE_READ_FSCREDS` 검사를 적용한다.
[Linux proc_pid_root 문서](https://man7.org/linux/man-pages/man5/proc_pid_root.5.html).

현재 앱은 capability가 없고 PID 1은 non-dumpable이다. 따라서 이미지 User가
UID 0이어도 이 접근은 거부된다. 같은 UID라는 사실만으로 검사를 통과하지
않는다. Dumpable만 켜더라도 capability 비교 등 다른 검사가 남아 있어
반드시 허용되는 것도 아니다.
[Linux ptrace 접근 검사](https://man7.org/linux/man-pages/man2/ptrace.2.html).

이 경로를 무조건 열면 앱 전용 namespace에서 가려 둔 경로 대신 PID 1의
관리용 mount view에 접근하는 통로가 생긴다. 이것이 모든 관리 자원을 즉시
탈취할 수 있다는 뜻은 아니지만, 검증을 통과시키려고 풀기에는 보호 범위가
넓다. `CAP_SYS_PTRACE` 부여나 dumpable 변경은 단순한 테스트 수정이 아니다.

기존 공개 exec 실기는 `/`와 `/proc/self/root`에서 이미지 marker를 읽는 데
성공했고, 원래 Gate 2 probe는 `/proc/1/root`에서 `Permission denied`로
실패했다. 따라서 해당 실패는 OCI root가 가짜라는 증거가 아니라 현재
검증 기준과 관리자 격리 정책의 충돌이다. 다만 앱이 자신의 root를 확인한
것만으로 **별도 PID 1 root 증명까지 완료했다고 주장해서도 안 된다.**

## 실제 PID 1 root는 어떻게 확인하고 있는가

PID 1 자신은 `verify_root_identity`(3324)에서 기존 merged root FD와 새 `/`,
`/proc/self/root`의 device/inode/mode를 대조하고 OverlayFS magic 및 sync를
확인한다. `transition_root`(3373)는 이 검사가 성공해야 진행한다. 그 뒤
생성된 자식은 같은 root를 상속하며, 추가 exec에도 같은 격리 정책을 적용한다.

이는 검증된 stage1 구현·실기 결과에 기반한 근거다. 기존 READY 메시지의
인증만으로 모든 root inode/marker가 별도의 완결된 원격 증명으로 전달된다고
과장하지 않는다. 하드웨어 attestation이나 guest kernel 전체의 무결성 증명도
아니다.
이 보고서의 기준 구현에서는 READY payload가 비어 있었다. 후속 계약은
인증된 최소 root identity를 추가하며 빈 legacy READY를 root 증거로 인정하지
않는다. 인증 envelope의 stage1 artifact 식별자는
stage1 transport에 대한 것이며 init 실행 파일의 SHA와 동일하다고 취급하면
안 된다. 선택적 assembly probe도 기본은 빈 목록이다. Boot key의 보호는
호스트가 관리하는 채널과 신뢰한 guest kernel/stage1을 전제로 한다.

## 사용자에게 생기는 제약과 선택지

현재는 이미지 안의 앱을 VM에서 관리하며 실행하는 모델이다. 일반 파일의
읽기·쓰기와 앱 실행은 가능하지만, privileged mount·커널 모듈 로딩·관리자
디버깅 등은 제한한다. 일반적인 full-root VM이나 이미지의 systemd를 PID 1로
직접 실행하는 모델과 같지 않다. 또 이 보호만으로 앱 간 완전한 격리, 자원
고갈 방지, guest kernel/hypervisor 취약점 방어, 호스트 관리자에 대한 방어가
증명되는 것은 아니다.

다음 중 **1번 방향은 후속 사용자 승인을 받았다.** 구현·실기 완료 여부는
새 계약의 검증 기록으로 구분한다. 2번은 아직 승인되지 않은 제안이다.

1. **현재 보호 유지:** 앱 probe는 자신의 실제 `/`를 검사하고, PID 1 root는
   필요한 최소 사실을 관리자가 직접 검사해 인증된 결과로 내보내는 별도
   계약을 설계한다. 이미지 임의 경로를 관리자 권한으로 읽는 API를 만들지
   않아야 한다. 기존 내부 검사/메시지가 무엇을 보장하는지도 명확히 구분한다.
2. **신뢰된 full-root VM 모드:** 이미지 프로세스의 관리자 권한이나 PID 1
   선택권이 제품 요구라면 별도 실행 정책으로 설계한다. 그 경우 현재 인증
   키와 lifecycle 관리자를 같은 신뢰 경계 안에 둬도 되는지 다시 검토한다.

승인된 1번 방향은 루트 파일시스템 경험을 유지하면서 관리 경계를 열지
않는다. 보호는 유지하고 원래 probe의 검증 기준만 명시적으로 변경한다.
Docker가 설치된 호스트 허용은 이 문제와 별개다.
