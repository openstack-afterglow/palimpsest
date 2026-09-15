#define SYS_write 1
#define SYS_open 2
#define SYS_close 3
#define SYS_fstat 5
#define SYS_pause 34
#define SYS_getpid 39
#define SYS_fcntl 72
#define SYS_mkdir 83
#define SYS_readlink 89
#define SYS_mount 165
#define SYS_statfs 137
#define O_WRONLY 1
#define O_ACCMODE 3
#define O_NONBLOCK 04000
#define O_CLOEXEC 02000000
#define O_NOFOLLOW 0400000
#define O_NOCTTY 0400
#define F_GETFD 1
#define F_GETFL 3
#define FD_CLOEXEC 1
#define ELOOP 40
#define EEXIST 17
#define EBUSY 16
#define S_IFMT 0170000
#define S_IFCHR 0020000
#define PROC_MAGIC 0x9fa0
#define SYSFS_MAGIC 0x62656572
#define DEVTMPFS_MAGIC 0x01021994
typedef unsigned long long u64;
typedef long long i64;
struct stat_local { u64 dev,ino,nlink; unsigned int mode,uid,gid,pad; u64 rdev; i64 size,blksize,blocks,atime,atime_ns,mtime,mtime_ns,ctime,ctime_ns,unused[3]; };
struct statfs_local { i64 type,bsize; u64 blocks,bfree,bavail,files,ffree; i64 fsid[2],namelen,frsize,flags,spare[4]; };
static i64 sc0(i64 n) { i64 r; __asm__ volatile("syscall":"=a"(r):"a"(n):"rcx","r11","memory"); return r; }
static i64 sc1(i64 n,i64 a) { i64 r; __asm__ volatile("syscall":"=a"(r):"a"(n),"D"(a):"rcx","r11","memory"); return r; }
static i64 sc2(i64 n,i64 a,i64 b) { i64 r; __asm__ volatile("syscall":"=a"(r):"a"(n),"D"(a),"S"(b):"rcx","r11","memory"); return r; }
static i64 sc3(i64 n,i64 a,i64 b,i64 c) { i64 r; register i64 d __asm__("rdx")=c; __asm__ volatile("syscall":"=a"(r):"a"(n),"D"(a),"S"(b),"d"(d):"rcx","r11","memory"); return r; }
static i64 sc5(i64 n,i64 a,i64 b,i64 c,i64 d,i64 e) { i64 r; register i64 r10 __asm__("r10")=d,r8 __asm__("r8")=e,rdx __asm__("rdx")=c; __asm__ volatile("syscall":"=a"(r):"a"(n),"D"(a),"S"(b),"d"(rdx),"r"(r10),"r"(r8):"rcx","r11","memory"); return r; }
static unsigned long length(const char *s) { unsigned long n=0; while(s[n]) n++; return n; }
static int line(int fd,const char *s) { unsigned long n=length(s); return sc3(SYS_write,fd,(i64)s,n)==(i64)n; }
static __attribute__((noreturn)) void stop(const char *s) { (void)line(2,s); for(;;) sc0(SYS_pause); }
static int mounted(const char *source,const char *path,const char *type,i64 flags,i64 magic) {
    struct statfs_local fs;
    i64 made=sc2(SYS_mkdir,(i64)path,0755), mount_result;
    if(made!=0 && made!=-EEXIST) return 0;
    mount_result=sc5(SYS_mount,(i64)source,(i64)path,(i64)type,flags,0);
    return (mount_result==0 || mount_result==-EBUSY) && sc2(SYS_statfs,(i64)path,(i64)&fs)==0 && fs.type==magic;
}
static int same(const struct stat_local *a,const struct stat_local *b) {
    return a->dev==b->dev && a->ino==b->ino && a->rdev==b->rdev && a->mode==b->mode &&
           a->uid==b->uid && a->gid==b->gid && (a->mode&S_IFMT)==S_IFCHR && (b->mode&S_IFMT)==S_IFCHR;
}
static __attribute__((noreturn,used)) void start(void) {
    struct stat_local before,after,reopened; char self[8];
    i64 original_flags,newfd,newflags,nofollow,self_size;
    if(sc0(SYS_getpid)!=1) stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL pid\n");
    if(!mounted("proc","/proc","proc",14,PROC_MAGIC) || !mounted("sysfs","/sys","sysfs",14,SYSFS_MAGIC) ||
       !mounted("devtmpfs","/dev","devtmpfs",10,DEVTMPFS_MAGIC)) stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL prepare-live\n");
    self_size=sc3(SYS_readlink,(i64)"/proc/self",(i64)self,sizeof(self));
    if(self_size!=1 || self[0]!='1') stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL self\n");
    if(sc2(SYS_fstat,1,(i64)&before)!=0 || (before.mode&S_IFMT)!=S_IFCHR || before.uid!=0 || before.gid!=0 || (before.mode&0777)!=0600)
        stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL original\n");
    original_flags=sc2(SYS_fcntl,1,F_GETFL);
    if(original_flags<0 || (original_flags&O_NONBLOCK)) stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL original-flags\n");
    nofollow=sc3(SYS_open,(i64)"/proc/self/fd/1",O_WRONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW|O_NOCTTY,0);
    if(nofollow!=-ELOOP) stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL nofollow\n");
    newfd=sc3(SYS_open,(i64)"/proc/self/fd/1",O_WRONLY|O_NONBLOCK|O_CLOEXEC|O_NOCTTY,0);
    if(newfd<0 || sc2(SYS_fstat,newfd,(i64)&reopened)!=0 || sc2(SYS_fstat,1,(i64)&after)!=0 ||
       !same(&before,&after) || !same(&before,&reopened)) stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL reopen\n");
    newflags=sc2(SYS_fcntl,newfd,F_GETFL);
    if(newflags<0 || (newflags&O_ACCMODE)!=O_WRONLY || !(newflags&O_NONBLOCK) ||
       sc2(SYS_fcntl,newfd,F_GETFD)!=FD_CLOEXEC || sc2(SYS_fcntl,1,F_GETFL)!=original_flags)
        stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL ofd\n");
    if(!line(1,"PALIMPSEST_CONSOLE_OFD_V1 ORIGINAL_WRITE\n") || !line((int)newfd,"PALIMPSEST_CONSOLE_OFD_V1 REOPEN_WRITE\n"))
        stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL write\n");
    if(sc1(SYS_close,newfd)!=0) stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL close\n");
    if(!line(1,"PALIMPSEST_CONSOLE_OFD_V1 PASS\n")) stop("PALIMPSEST_CONSOLE_OFD_V1 FAIL pass-write\n");
    for(;;) sc0(SYS_pause);
}
__attribute__((naked,noreturn,visibility("default"))) void _start(void) { __asm__ volatile("and $-16,%rsp\ncall start\n"); }
