/* SPDX-License-Identifier: MIT
 * Freestanding Linux/x86_64 diagnostic for inherited and reopened stdio FDs.
 */
typedef unsigned int u32;
typedef unsigned long u64;
typedef long i64;
#define O_WRONLY 1
#define O_RDONLY 0
#define O_CREAT 0100
#define O_NOCTTY 0400
#define O_APPEND 02000
#define O_NONBLOCK 04000
#define O_NOFOLLOW 0400000
#define O_DIRECTORY 0200000
#define O_CLOEXEC 02000000
#define ENOENT 2
#define SYS_getdents64 217
#define SYS_pipe2 293
#define SYS_fstatfs 138
#define TMPFS_MAGIC 0x01021994
#define ST_RDONLY 1
#define AT_FDCWD -100
#define AT_SYMLINK_NOFOLLOW 0x100
#define PR_GET_SECUREBITS 27
#define BUFFER_SIZE 2048
#define S_IFMT 0170000
#define S_IFLNK 0120000
struct timespec_local { i64 sec; i64 nsec; };
struct stat_local {
    u64 dev; u64 ino; u64 nlink; u32 mode; u32 uid; u32 gid; u32 pad0; u64 rdev;
    i64 size; i64 blksize; i64 blocks;
    struct timespec_local atime; struct timespec_local mtime; struct timespec_local ctime;
    i64 reserved[3];
};
_Static_assert(sizeof(struct stat_local) == 144, "x86_64 stat ABI");
struct statfs_local { i64 type,bsize;u64 blocks,bfree,bavail,files,ffree;int fsid[2];i64 namelen,frsize,flags,spare[4]; };
static i64 sc0(i64 n) { i64 r; __asm__ volatile("syscall":"=a"(r):"a"(n):"rcx","r11","memory"); return r; }
static i64 sc1(i64 n,i64 a) { i64 r; __asm__ volatile("syscall":"=a"(r):"a"(n),"D"(a):"rcx","r11","memory"); return r; }
static i64 sc2(i64 n,i64 a,i64 b) { i64 r; __asm__ volatile("syscall":"=a"(r):"a"(n),"D"(a),"S"(b):"rcx","r11","memory"); return r; }
static i64 sc3(i64 n,i64 a,i64 b,i64 c) { i64 r; __asm__ volatile("syscall":"=a"(r):"a"(n),"D"(a),"S"(b),"d"(c):"rcx","r11","memory"); return r; }
static i64 sc4(i64 n,i64 a,i64 b,i64 c,i64 d) { register i64 r10 __asm__("r10")=d; i64 r; __asm__ volatile("syscall":"=a"(r):"a"(n),"D"(a),"S"(b),"d"(c),"r"(r10):"rcx","r11","memory"); return r; }
static __attribute__((noreturn)) void finish(i64 status) { sc1(60,status); for (;;) {} }
static int same(const char *a,const char *b) { while(*a&&*a==*b){a++;b++;} return *a==*b; }
static u64 text_length(const char *text) { u64 size=0; while(text[size])size++; return size; }
static int bytes_at(const char *body,u64 size,u64 offset,const char *expected) {
    u64 index, expected_size=text_length(expected);
    if(offset>size||expected_size>size-offset)return 0;
    for(index=0;index<expected_size;index++)if(body[offset+index]!=expected[index])return 0;
    return 1;
}
static int append(char *body,u64 *used,const char *text) {
    u64 size=text_length(text),index;
    if(*used>BUFFER_SIZE||size>BUFFER_SIZE-*used)return 0;
    for(index=0;index<size;index++)body[(*used)++]=text[index];
    return 1;
}
static int append_number(char *body,u64 *used,u64 value) {
    char reverse[24]; u64 count=0;
    do{reverse[count++]=(char)('0'+value%10);value/=10;}while(value);
    if(count>BUFFER_SIZE-*used)return 0;
    while(count)body[(*used)++]=reverse[--count];
    return 1;
}
static int append_hex16(char *body,u64 *used,u64 value) {
    static const char hex[]="0123456789abcdef"; int shift;
    if(16>BUFFER_SIZE-*used)return 0;
    for(shift=60;shift>=0;shift-=4)body[(*used)++]=hex[(value>>shift)&15];
    return 1;
}
static int write_all(int descriptor,const char *body,u64 size) {
    u64 used=0;
    while(used<size){i64 count=sc3(1,descriptor,(i64)(body+used),size-used);if(count==-4)continue;if(count<=0||(u64)count>size-used)return 0;used+=(u64)count;}
    return 1;
}
static int write_result(int descriptor,const char *body) {
    u64 size=text_length(body),used=0;
    while(used<size){i64 count=sc3(1,descriptor,(i64)(body+used),size-used);if(count==-4)continue;if(count<0)return (int)-count;if(!count||(u64)count>size-used)return 5;used+=(u64)count;}
    return 0;
}
static int stat_identity(const struct stat_local *left,const struct stat_local *right) {
    return left->dev==right->dev&&left->ino==right->ino&&left->rdev==right->rdev&&left->mode==right->mode&&left->uid==right->uid&&left->gid==right->gid;
}
static int exact_link(const char *path,const char *target) {
    char value[64];u64 expected=text_length(target);i64 size=sc4(267,AT_FDCWD,(i64)path,(i64)value,sizeof(value));u64 index;
    if(size<0||(u64)size!=expected)return 0;
    for(index=0;index<expected;index++)if(value[index]!=target[index])return 0;
    return 1;
}
static int exact_alias(const struct stat_local *metadata) {
    return (metadata->mode&S_IFMT)==S_IFLNK&&(metadata->mode&07777)==0777&&metadata->uid==0&&metadata->gid==0&&metadata->nlink==1;
}
static int decimal_path(char *path,int descriptor) {
    char reverse[24];u64 used=0,count=0,value=(u64)descriptor,index;const char *prefix="/dev/fd/";
    while(prefix[used]){path[used]=prefix[used];used++;}
    do{reverse[count++]=(char)('0'+value%10);value/=10;}while(value);
    if(used+count+1>64)return 0;
    for(index=0;index<count;index++)path[used+index]=reverse[count-index-1];
    path[used+count]=0;return 1;
}
static int inherited_fd_inventory(void) {
    unsigned char entries[1024];u64 seen=0;int directory=(int)sc3(2,(i64)"/proc/self/fd",O_RDONLY|O_DIRECTORY|O_CLOEXEC,0);
    if(directory<0||directory>=63)return 0;
    for(;;){i64 count=sc3(SYS_getdents64,directory,(i64)entries,sizeof(entries));u64 offset=0;if(count<0)return 0;if(!count)break;
        while(offset<(u64)count){unsigned char *entry=entries+offset;u64 length=0,value=0;u32 reclen;
            if((u64)count-offset<20)return 0;
            reclen=(u32)entry[16]|((u32)entry[17]<<8);
            if(reclen<20||reclen>(u64)count-offset)return 0;
            while(length<reclen-19&&entry[19+length])length++;
            if(length==reclen-19)return 0;
            if(!((length==1&&entry[19]=='.')||(length==2&&entry[19]=='.'&&entry[20]=='.'))){u64 at;if(!length)return 0;for(at=0;at<length;at++){if(entry[19+at]<'0'||entry[19+at]>'9')return 0;value=value*10+(entry[19+at]-'0');}if(value>=63||(seen&(1ul<<value)))return 0;seen|=1ul<<value;}
            offset+=reclen;}}
    if(sc1(3,directory))return 0;
    return seen==((1ul<<0)|(1ul<<1)|(1ul<<2)|(1ul<<directory));
}
static int exact_dev_inventory(void) {
    static const char *allowed[]={"null","zero","full","random","urandom","tty","stdout","stderr","fd"};
    unsigned char entries[1024];u32 seen=0;int directory=(int)sc3(2,(i64)"/dev",O_RDONLY|O_DIRECTORY|O_CLOEXEC,0);if(directory<0)return 0;
    for(;;){i64 count=sc3(SYS_getdents64,directory,(i64)entries,sizeof(entries));u64 offset=0;if(count<0)return 0;if(!count)break;
        while(offset<(u64)count){unsigned char *entry=entries+offset;u64 length=0,index;u32 reclen;int match=0;if((u64)count-offset<20)return 0;reclen=(u32)entry[16]|((u32)entry[17]<<8);if(reclen<20||reclen>(u64)count-offset)return 0;while(length<reclen-19&&entry[19+length])length++;if(length==reclen-19)return 0;
            if(!((length==1&&entry[19]=='.')||(length==2&&entry[19]=='.'&&entry[20]=='.'))){for(index=0;index<9;index++){u64 at;if(length!=text_length(allowed[index]))continue;for(at=0;at<length&&entry[19+at]==(unsigned char)allowed[index][at];at++){}if(at==length){match=(int)index+1;break;}}if(!match||(seen&(1u<<(match-1))))return 0;seen|=1u<<(match-1);}offset+=reclen;}}
    return sc1(3,directory)==0&&seen==0x1ff;
}
static int directory_is_empty(const char *path) {
    unsigned char entries[512];struct statfs_local filesystem;int directory=(int)sc3(2,(i64)path,O_RDONLY|O_DIRECTORY|O_CLOEXEC,0);if(directory<0)return 0;
    if(sc2(SYS_fstatfs,directory,(i64)&filesystem)||filesystem.type!=TMPFS_MAGIC||!(filesystem.flags&ST_RDONLY)){sc1(3,directory);return 0;}
    for(;;){i64 count=sc3(SYS_getdents64,directory,(i64)entries,sizeof(entries));u64 offset=0;if(count<0)return 0;if(!count)break;
        while(offset<(u64)count){unsigned char *entry=entries+offset;u64 length=0;u32 reclen;if((u64)count-offset<20)return 0;reclen=(u32)entry[16]|((u32)entry[17]<<8);if(reclen<20||reclen>(u64)count-offset)return 0;while(length<reclen-19&&entry[19+length])length++;if(length==reclen-19)return 0;if(!((length==1&&entry[19]=='.')||(length==2&&entry[19]=='.'&&entry[20]=='.')))return 0;offset+=reclen;}}
    return sc1(3,directory)==0;
}
static int field_hex(const char *body,u64 size,const char *key,u64 *value) {
    u64 offset,key_size=text_length(key);
    for(offset=0;offset+key_size+16<=size;offset++)if(bytes_at(body,size,offset,key)){
        u64 parsed=0,index;
        for(index=0;index<16;index++){char c=body[offset+key_size+index];u64 digit=c>='0'&&c<='9'?(u64)(c-'0'):c>='a'&&c<='f'?(u64)(c-'a'+10):99;if(digit>15)return 0;parsed=(parsed<<4)|digit;}
        *value=parsed;return 1;
    }
    return 0;
}
static int field_decimal(const char *body,u64 size,const char *key,u64 *value) {
    u64 offset,key_size=text_length(key);
    for(offset=0;offset+key_size<size;offset++)if(bytes_at(body,size,offset,key)){
        u64 parsed=0,index=offset+key_size;if(body[index]<'0'||body[index]>'9')return 0;
        while(index<size&&body[index]>='0'&&body[index]<='9')parsed=parsed*10+(u64)(body[index++]-'0');
        *value=parsed;return 1;
    }
    return 0;
}
static int append_pair(char *body,u64 *used,const char *key,u64 value) { return append(body,used,key)&&append_number(body,used,value); }
static __attribute__((used,noreturn)) void probe(u64 *stack) {
    char **argv=(char **)(stack+1),status[4096],output[BUFFER_SIZE];
    u64 used=0,total=0,capinh,capprm,capeff,capbnd,capamb,nnp,seccomp,groups[1];
    struct stat_local first,second,root,alias_out,alias_err,alias_fd,reopened,pipe_original;
    int descriptor,reopen1,reopen2,alias1,alias2,fdalias,meta1=0,meta2=0,fdmeta=0,target1=0,target2=0,fdtarget=0,open1=22,open2=22,same1=0,same2=0,write1=22,write2=22,fd1path_same=0,fd2path_same=0,fd1path_write=22,fd2path_write=22,stdin_alias,pid1,pid1fdempty,pid1fdinfoempty,pipefd[2],pipe_read_same=0,pipe_write_same=0,pipe_read=22,pipe_write=22,closed_fd=22; i64 group_count,securebits;char pipe_path[64];char byte;
    if(stack[0]!=2||(!same(argv[1],"service")&&!same(argv[1],"exec")))finish(90);
    if(!inherited_fd_inventory())finish(89);
    descriptor=(int)sc3(2,(i64)"/proc/self/status",O_NOFOLLOW,0);if(descriptor<0)finish(91);
    for(;;){i64 count=sc3(0,descriptor,(i64)(status+total),sizeof(status)-total);if(count<0)finish(92);if(!count)break;total+=(u64)count;if(total==sizeof(status))finish(93);}
    if(sc1(3,descriptor))finish(94);
    if(!field_hex(status,total,"CapInh:\t",&capinh)||!field_hex(status,total,"CapPrm:\t",&capprm)||!field_hex(status,total,"CapEff:\t",&capeff)||!field_hex(status,total,"CapBnd:\t",&capbnd)||!field_hex(status,total,"CapAmb:\t",&capamb)||!field_decimal(status,total,"NoNewPrivs:\t",&nnp)||!field_decimal(status,total,"Seccomp:\t",&seccomp))finish(95);
    if(sc2(5,1,(i64)&first)||sc2(5,2,(i64)&second)||sc2(4,(i64)"/",(i64)&root))finish(96);
    descriptor=(int)sc3(2,(i64)"/proc/self/fd/1",O_WRONLY|O_APPEND|O_NONBLOCK|O_NOCTTY,0);reopen1=descriptor<0?-descriptor:0;if(descriptor>=0&&sc1(3,descriptor))finish(97);
    descriptor=(int)sc3(2,(i64)"/proc/self/fd/2",O_WRONLY|O_APPEND|O_NONBLOCK|O_NOCTTY,0);reopen2=descriptor<0?-descriptor:0;if(descriptor>=0&&sc1(3,descriptor))finish(98);
    descriptor=(int)sc4(262,AT_FDCWD,(i64)"/dev/stdout",(i64)&alias_out,AT_SYMLINK_NOFOLLOW);alias1=descriptor<0?-descriptor:(int)(alias_out.mode&S_IFMT);if(!descriptor){meta1=exact_alias(&alias_out);target1=exact_link("/dev/stdout","/proc/self/fd/1");}
    descriptor=(int)sc4(262,AT_FDCWD,(i64)"/dev/stderr",(i64)&alias_err,AT_SYMLINK_NOFOLLOW);alias2=descriptor<0?-descriptor:(int)(alias_err.mode&S_IFMT);if(!descriptor){meta2=exact_alias(&alias_err);target2=exact_link("/dev/stderr","/proc/self/fd/2");}
    descriptor=(int)sc4(262,AT_FDCWD,(i64)"/dev/fd",(i64)&alias_fd,AT_SYMLINK_NOFOLLOW);fdalias=descriptor<0?-descriptor:(int)(alias_fd.mode&S_IFMT);if(!descriptor){fdmeta=exact_alias(&alias_fd);fdtarget=exact_link("/dev/fd","/proc/self/fd");}
    if(!exact_dev_inventory())finish(88);
    if(meta1&&target1){descriptor=(int)sc3(2,(i64)"/dev/stdout",O_WRONLY|O_APPEND|O_CREAT|O_NONBLOCK|O_NOCTTY,0666);open1=descriptor<0?-descriptor:0;if(descriptor>=0){if(sc2(5,descriptor,(i64)&reopened))finish(97);same1=stat_identity(&first,&reopened);if(same1)write1=write_result(descriptor,same(argv[1],"service")?"PALIMPSEST_STDIO_ALIAS_V3 service stdout\n":"PALIMPSEST_STDIO_ALIAS_V3 exec stdout\n");if(sc1(3,descriptor))finish(97);}}
    if(meta2&&target2){descriptor=(int)sc3(2,(i64)"/dev/stderr",O_WRONLY|O_APPEND|O_CREAT|O_NONBLOCK|O_NOCTTY,0666);open2=descriptor<0?-descriptor:0;if(descriptor>=0){if(sc2(5,descriptor,(i64)&reopened))finish(98);same2=stat_identity(&second,&reopened);if(same2)write2=write_result(descriptor,same(argv[1],"service")?"PALIMPSEST_STDIO_ALIAS_V3 service stderr\n":"PALIMPSEST_STDIO_ALIAS_V3 exec stderr\n");if(sc1(3,descriptor))finish(98);}}
    descriptor=(int)sc3(2,(i64)"/dev/fd/1",O_WRONLY|O_APPEND|O_NONBLOCK|O_NOCTTY,0);if(descriptor>=0){if(sc2(5,descriptor,(i64)&reopened))finish(97);fd1path_same=stat_identity(&first,&reopened);if(fd1path_same)fd1path_write=write_result(descriptor,same(argv[1],"service")?"PALIMPSEST_STDIO_FD_ALIAS_V3 service stdout\n":"PALIMPSEST_STDIO_FD_ALIAS_V3 exec stdout\n");if(sc1(3,descriptor))finish(97);}
    descriptor=(int)sc3(2,(i64)"/dev/fd/2",O_WRONLY|O_APPEND|O_NONBLOCK|O_NOCTTY,0);if(descriptor>=0){if(sc2(5,descriptor,(i64)&reopened))finish(98);fd2path_same=stat_identity(&second,&reopened);if(fd2path_same)fd2path_write=write_result(descriptor,same(argv[1],"service")?"PALIMPSEST_STDIO_FD_ALIAS_V3 service stderr\n":"PALIMPSEST_STDIO_FD_ALIAS_V3 exec stderr\n");if(sc1(3,descriptor))finish(98);}
    descriptor=(int)sc4(262,AT_FDCWD,(i64)"/dev/stdin",(i64)&reopened,AT_SYMLINK_NOFOLLOW);stdin_alias=descriptor<0?-descriptor:0;
    pid1fdempty=directory_is_empty("/proc/1/fd");pid1fdinfoempty=directory_is_empty("/proc/1/fdinfo");
    if(sc2(SYS_pipe2,(i64)pipefd,O_CLOEXEC|O_NONBLOCK)||pipefd[0]<=2||pipefd[1]<=2)finish(87);
    if(sc2(5,pipefd[0],(i64)&pipe_original)||!decimal_path(pipe_path,pipefd[0]))finish(87);
    descriptor=(int)sc3(2,(i64)pipe_path,O_RDONLY|O_CLOEXEC|O_NONBLOCK,0);pipe_read=descriptor<0?-descriptor:0;
    if(descriptor>=0){if(sc2(5,descriptor,(i64)&reopened))finish(87);pipe_read_same=stat_identity(&pipe_original,&reopened);if(sc3(1,pipefd[1],(i64)"r",1)!=1||sc3(0,descriptor,(i64)&byte,1)!=1||byte!='r')finish(87);if(sc1(3,descriptor))finish(87);}
    if(sc2(5,pipefd[1],(i64)&pipe_original)||!decimal_path(pipe_path,pipefd[1]))finish(87);
    descriptor=(int)sc3(2,(i64)pipe_path,O_WRONLY|O_CLOEXEC|O_NONBLOCK,0);pipe_write=descriptor<0?-descriptor:0;
    if(descriptor>=0){if(sc2(5,descriptor,(i64)&reopened))finish(87);pipe_write_same=stat_identity(&pipe_original,&reopened);if(sc3(1,descriptor,(i64)"w",1)!=1||sc3(0,pipefd[0],(i64)&byte,1)!=1||byte!='w')finish(87);if(sc1(3,descriptor))finish(87);}
    descriptor=pipefd[1];if(sc1(3,descriptor)||!decimal_path(pipe_path,descriptor))finish(87);descriptor=(int)sc3(2,(i64)pipe_path,O_WRONLY|O_CLOEXEC|O_NONBLOCK,0);closed_fd=descriptor<0?-descriptor:0;if(descriptor>=0){sc1(3,descriptor);finish(87);}if(sc1(3,pipefd[0]))finish(87);
    descriptor=(int)sc3(2,(i64)"/proc/1/root/oci-public-root",O_NOFOLLOW,0);pid1=descriptor<0?-descriptor:0;if(descriptor>=0){sc1(3,descriptor);finish(99);}
    group_count=sc2(115,1,(i64)groups);if(group_count<0)finish(100);securebits=sc2(157,PR_GET_SECUREBITS,0);if(securebits<0)finish(101);
#define A(text) do{if(!append(output,&used,text))finish(102);}while(0)
#define N(key,value) do{if(!append_pair(output,&used,key,(u64)(value)))finish(102);}while(0)
    A("PALIMPSEST_STDIO_FD_V3 role=");A(argv[1]);N(" uid=",sc0(102));N(" gid=",sc0(104));N(" groups=",group_count);
    A(" capinh=");if(!append_hex16(output,&used,capinh))finish(102);A(" capprm=");if(!append_hex16(output,&used,capprm))finish(102);A(" capeff=");if(!append_hex16(output,&used,capeff))finish(102);A(" capbnd=");if(!append_hex16(output,&used,capbnd))finish(102);A(" capamb=");if(!append_hex16(output,&used,capamb))finish(102);
    N(" securebits=",securebits);N(" nnp=",nnp);N(" seccomp=",seccomp);
    N(" fd1type=",first.mode&0170000);N(" fd1mode=",first.mode&07777);N(" fd1uid=",first.uid);N(" fd1gid=",first.gid);N(" fd1dev=",first.dev);N(" fd1ino=",first.ino);N(" fd1reopen=",reopen1);
    N(" fd2type=",second.mode&0170000);N(" fd2mode=",second.mode&07777);N(" fd2uid=",second.uid);N(" fd2gid=",second.gid);N(" fd2dev=",second.dev);N(" fd2ino=",second.ino);N(" fd2reopen=",reopen2);
    N(" stdout_alias=",alias1);N(" stdout_meta=",meta1);N(" stdout_target=",target1);N(" stdout_open=",open1);N(" stdout_same=",same1);N(" stdout_write=",write1);
    N(" stderr_alias=",alias2);N(" stderr_meta=",meta2);N(" stderr_target=",target2);N(" stderr_open=",open2);N(" stderr_same=",same2);N(" stderr_write=",write2);
    N(" stdin_alias=",stdin_alias);N(" fd_alias=",fdalias);N(" fd_meta=",fdmeta);N(" fd_target=",fdtarget);N(" inherited_fds=",1);N(" dev_entries=",9);N(" fd1path_same=",fd1path_same);N(" fd2path_same=",fd2path_same);N(" fd1path_write=",fd1path_write);N(" fd2path_write=",fd2path_write);N(" pipe_read_same=",pipe_read_same);N(" pipe_write_same=",pipe_write_same);N(" pipe_read=",pipe_read);N(" pipe_write=",pipe_write);N(" closed_fd=",closed_fd);N(" pid1fdempty=",pid1fdempty);N(" pid1fdinfoempty=",pid1fdinfoempty);N(" rootdev=",root.dev);N(" rootino=",root.ino);N(" pid1root=",pid1);A("\n");
#undef A
#undef N
    if (!write_all(1, output, used) || !write_all(2, output, used)) {
        finish(103);
    }
    if (same(argv[1], "exec")) {
        finish(0);
    }
    for (;;) {
        sc0(34);
    }
}
__asm__(".global _start\n_start:\nmov %rsp,%rdi\nand $-16,%rsp\ncall probe\n");
