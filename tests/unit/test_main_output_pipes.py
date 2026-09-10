import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "guest/stage1/init.c"


def _function(source: str, name: str) -> str:
    start = source.index(name)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated {name}")


def test_stage1_main_output_pipe_helpers_compile_for_linux_x86_64():
    target = ["-target", "x86_64-linux-gnu"] if sys.platform == "darwin" else []
    result = subprocess.run(
        [
            "cc",
            *target,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-ffreestanding",
            "-fsyntax-only",
            str(SOURCE),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_prepare_main_output_binds_exact_owned_blocking_writer_contract():
    source = SOURCE.read_text()
    prepare = _function(source, "static int prepare_main_output")
    assert "SYS_pipe2" in prepare and "O_CLOEXEC" in prepare
    assert "pipes[stream][endpoint] < 3" in prepare
    assert "S_IFIFO" in prepare and "!= 0600" in prepare
    assert "SYS_fchown" in prepare and "process->uid, process->gid" in prepare
    assert prepare.count("F_GETFD") == 1
    assert "FD_CLOEXEC" in prepare
    assert prepare.count("(flags & O_ACCMODE) != O_RDONLY") == 2
    assert "(flags & O_ACCMODE) != O_WRONLY" in prepare
    assert "flags | O_NONBLOCK" in prepare
    assert "(flags & O_NONBLOCK)" in prepare
    assert "while (created)" in prepare


def test_main_child_and_parent_have_disjoint_endpoint_ownership():
    source = SOURCE.read_text()
    install = _function(source, "static int child_install_main_output")
    supervise = _function(source, "static int supervise_workload")
    exec_child = _function(source, "static __attribute__((noreturn)) void exec_child")
    assert install.index("main_output.read_fd[stream] = -1") < install.index("SYS_dup3")
    assert "main_output.write_fd[0], 1" in install
    assert "main_output.write_fd[1], 2" in install
    assert "main_output.write_fd[stream] = -1" in install
    assert supervise.index("prepare_main_output(process)") < supervise.index("SYS_fork")
    assert supervise.index("child_install_main_output(error_pipe[1])") < supervise.index("prepare_workload_isolation")
    assert supervise.count("parent_close_main_output_writers()") == 1
    assert "if (!close_main_output()) child_fail(error, 43, EIO);" in exec_child


def test_fork_failure_closes_every_main_output_endpoint():
    supervise = _function(SOURCE.read_text(), "static int supervise_workload")
    fork_failure = supervise[supervise.index("if (main_pid < 0)") :]
    assert "if (!close_main_output()) set_workload_failure(failure, 43, EIO);" in fork_failure


def test_prepare_main_output_actual_fault_matrix(tmp_path):
    source = SOURCE.read_text()
    extracted = _function(source, "static int prepare_main_output")
    harness = tmp_path / "main_output_pipe_harness.c"
    harness.write_text(
        textwrap.dedent(r"""
        #include <stdint.h>
        #include <stdlib.h>
        #include <string.h>
        typedef unsigned int u32; typedef unsigned long u64; typedef signed long i64;
        #define SYS_pipe2 293
        #define SYS_close 3
        #define SYS_fstat 5
        #define SYS_fcntl 72
        #define SYS_fchown 93
        #define O_RDONLY 0
        #define O_WRONLY 1
        #define O_ACCMODE 3
        #define O_NONBLOCK 04000
        #define O_CLOEXEC 02000000
        #define F_GETFD 1
        #define F_GETFL 3
        #define FD_CLOEXEC 1
        #define S_IFMT 0170000
        #define S_IFIFO 0010000
        struct stat_local { u64 dev, ino, nlink; u32 mode, uid, gid; };
        struct guest_process { u32 uid, gid; };
        struct main_output_pump { int value; };
        struct main_output_local { struct main_output_pump pump; int read_fd[2], write_fd[2]; };
        static struct main_output_local main_output = {{0}, {-1,-1}, {-1,-1}};
        static int scenario, pipes, closes, owners[8], flags[8];
        static void main_output_pump_init(struct main_output_pump *p) { p->value = 1; }
        static i64 sc1(i64 call, i64 fd) { if (call != SYS_close) return -1; closes++; flags[fd] = -1; return 0; }
        static i64 sc2(i64 call, i64 a, i64 b) {
          int fd; (void)b;
          if (call == SYS_pipe2) { int *p=(int *)(uintptr_t)a; if (scenario==1 && pipes==1) return -5;
            p[0]=scenario==2 && pipes==0 ? 0 : 3+pipes*2; p[1]=3+pipes*2+1;
            flags[p[0]]=O_RDONLY; flags[p[1]]=O_WRONLY; pipes++; return 0; }
          if (call == SYS_fstat) { struct stat_local *s=(void *)(uintptr_t)b; fd=(int)a; memset(s,0,sizeof(*s));
            s->dev=7; s->ino=scenario==2 ? (fd<=4 ? 100 : 101) : 100+(fd-3)/2; s->mode=S_IFIFO|0600; s->uid=owners[fd]; s->gid=owners[fd];
            if (scenario==3) s->mode=S_IFIFO|0644; if (scenario==4 && owners[fd]) s->ino++; return 0; }
          return -1;
        }
        static i64 sc3(i64 call, i64 a, i64 b, i64 c) { int fd=(int)a;
          if (call==SYS_fchown) { owners[fd]=(int)b; return b==c ? 0 : -1; }
          if (call==SYS_fcntl) { if (b==F_GETFD) return FD_CLOEXEC; if (b==F_GETFL) return (scenario==5 && fd==4) || (scenario==2 && fd==6) ? O_RDONLY : flags[fd];
            if (b==4) { flags[fd]=(int)c; return 0; } } return -1; }
    """)
        + "\n"
        + extracted
        + textwrap.dedent(r"""
        int main(int argc,char **argv) { struct guest_process p={101,101}; int result;
          scenario=argc>1?atoi(argv[1]):0; result=prepare_main_output(&p);
          if (!scenario) return !(result && pipes==2 && closes==0 && main_output.pump.value &&
            (flags[3]&O_NONBLOCK) && !(flags[4]&O_NONBLOCK) && (flags[5]&O_NONBLOCK) && !(flags[6]&O_NONBLOCK));
          if (scenario==2) return result || closes != 3;
          return result || closes != pipes*2;
        }
    """)
    )
    binary = tmp_path / "harness"
    built = subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(binary)], capture_output=True
    )
    assert built.returncode == 0, built.stderr.decode()
    for scenario in range(6):
        ran = subprocess.run([str(binary), str(scenario)], capture_output=True)
        assert ran.returncode == 0, f"scenario {scenario}: {ran.stderr.decode()}"


def test_child_install_and_parent_close_actual_fault_matrix(tmp_path):
    source = SOURCE.read_text()
    extracted = "\n\n".join(
        (
            _function(source, "static int child_install_main_output"),
            _function(source, "static int parent_close_main_output_writers"),
        )
    )
    harness = tmp_path / "main_output_child_harness.c"
    harness.write_text(
        textwrap.dedent(r"""
        #include <setjmp.h>
        #include <stdint.h>
        #include <stdlib.h>
        typedef unsigned int u32; typedef signed long i64;
        #define SYS_close 3
        #define SYS_dup3 292
        #define EIO 5
        struct main_output_pump { int unused; };
        struct main_output_local { struct main_output_pump pump; int read_fd[2], write_fd[2]; };
        struct main_console_queue { unsigned int failed; };
        static struct main_output_local main_output={{0},{10,12},{11,13}};
        static struct main_console_queue main_console_queue;
        static jmp_buf failed; static int scenario, close_attempt[32], dup_attempts, stage_seen, error_seen;
        static __attribute__((noreturn)) void child_fail(int fd, u32 stage, u32 error) {
          (void)fd; stage_seen=(int)stage; error_seen=(int)error; longjmp(failed,1);
        }
        static i64 sc1(i64 call,i64 fd) { if(call!=SYS_close)return -99; close_attempt[fd]++;
              if ((scenario==1&&fd==10)||(scenario==2&&fd==12)||(scenario==5&&fd==11)||
                  (scenario==6&&fd==13)||(scenario==7&&fd==11)||(scenario==8&&fd==13)) { return -5; }
              return 0; }
            static i64 sc3(i64 call,i64 old,i64 target,i64 flags) { (void)old; (void)flags; if(call!=SYS_dup3)return -99;
          dup_attempts++; if((scenario==3&&target==1)||(scenario==4&&target==2))return -5; return target; }
    """)
        + "\n"
        + extracted
        + textwrap.dedent(r"""
        int main(int argc,char **argv) { int result=0; scenario=argc>1?atoi(argv[1]):0;
          if (scenario<=6) {
            if (!setjmp(failed)) result=child_install_main_output(20);
            if (!scenario) return !(result==1 && dup_attempts==2 && main_output.read_fd[0]==-1 &&
              main_output.read_fd[1]==-1 && main_output.write_fd[0]==-1 && main_output.write_fd[1]==-1);
            if (!stage_seen || stage_seen!=43 || error_seen!=EIO) return 10;
            if (scenario==1 && (dup_attempts || main_output.read_fd[0]!=-1 || main_output.read_fd[1]!=12)) return 11;
            if (scenario==2 && (dup_attempts || main_output.read_fd[1]!=-1)) return 12;
            if (scenario==3 && dup_attempts!=1) return 13;
            if (scenario==4 && dup_attempts!=2) return 14;
            if (scenario==5 && (dup_attempts!=2 || main_output.write_fd[0]!=-1 || close_attempt[13])) return 15;
            if (scenario==6 && (dup_attempts!=2 || main_output.write_fd[1]!=-1)) return 16;
            return 0;
          }
          result=parent_close_main_output_writers();
          if (result || !main_console_queue.failed || main_output.write_fd[0]!=-1 ||
              main_output.write_fd[1]!=-1 || close_attempt[11]!=1 || close_attempt[13]!=1) return 20;
          return 0;
        }
    """)
    )
    binary = tmp_path / "harness"
    built = subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(binary)], capture_output=True
    )
    assert built.returncode == 0, built.stderr.decode()
    for scenario in range(9):
        ran = subprocess.run([str(binary), str(scenario)], capture_output=True)
        assert ran.returncode == 0, f"scenario {scenario}: rc={ran.returncode} {ran.stderr.decode()}"


def test_eof_and_full_cleanup_close_faults_actual(tmp_path):
    source = SOURCE.read_text()
    extracted = "\n\n".join((
        _function(source, "static int close_main_output_fd"),
        _function(source, "static int close_main_output(void)"),
        _function(source, "static main_output_count read_main_output_nonblocking"),
    ))
    harness = tmp_path / "main_output_close_harness.c"
    harness.write_text(textwrap.dedent(r'''
        #include <stdint.h>
        #include <stdlib.h>
        typedef unsigned int u32; typedef unsigned long usize; typedef signed long i64;
        typedef long main_output_count; typedef unsigned long main_output_size;
        #define SYS_close 3
        #define SYS_read 0
        #define EIO 5
        struct main_output_pump { unsigned int failed; };
        struct main_output_local { struct main_output_pump pump; int read_fd[2], write_fd[2]; };
        static struct main_output_local main_output={{0},{10,12},{11,13}};
        static int scenario, close_attempt[32], reads;
        static i64 sc1(i64 call,i64 fd) { if(call!=SYS_close)return -99; close_attempt[fd]++;
          if ((scenario==1&&fd==10)||(scenario==2&&fd==12)||(scenario==3&&fd==10)) return -5; return 0; }
        static i64 sc3(i64 call,i64 fd,i64 bytes,i64 size) { (void)fd;(void)bytes;(void)size;
          if(call!=SYS_read)return -99; reads++; return 0; }
    ''') + "\n" + extracted + textwrap.dedent(r'''
        int main(int argc,char **argv) { unsigned char byte; int result; scenario=argc>1?atoi(argv[1]):0;
          if (scenario<=2) {
            result=(int)read_main_output_nonblocking(&main_output,(unsigned int)(scenario==2),&byte,1);
            if (!scenario) return result || reads!=1 || main_output.read_fd[0]!=-1 || close_attempt[10]!=1;
            if (result!=-EIO || reads!=1) return 10;
            if (scenario==1 && (main_output.read_fd[0]!=-1 || close_attempt[10]!=1)) return 11;
            if (scenario==2 && (main_output.read_fd[1]!=-1 || close_attempt[12]!=1)) return 12;
            main_output.pump.failed=1; /* production pump latches the returned permanent error */
            return !main_output.pump.failed;
          }
          result=close_main_output();
          if (result || main_output.read_fd[0]!=-1 || main_output.read_fd[1]!=-1 ||
              main_output.write_fd[0]!=-1 || main_output.write_fd[1]!=-1) return 20;
          if (close_attempt[10]!=1 || close_attempt[11]!=1 || close_attempt[12]!=1 || close_attempt[13]!=1) return 21;
          return 0;
        }
    '''))
    binary = tmp_path / "harness"
    built = subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(binary)], capture_output=True)
    assert built.returncode == 0, built.stderr.decode()
    for scenario in range(4):
        ran = subprocess.run([str(binary), str(scenario)], capture_output=True)
        assert ran.returncode == 0, f"scenario {scenario}: rc={ran.returncode} {ran.stderr.decode()}"
