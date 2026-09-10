/* SPDX-License-Identifier: MIT
 * Freestanding Linux/x86_64 diagnostic for inherited and reopened stdio FDs.
 */
typedef unsigned int u32;
typedef unsigned long u64;
typedef long i64;
#define O_WRONLY 1
#define O_CREAT 0100
#define O_NOCTTY 0400
#define O_APPEND 02000
#define O_NONBLOCK 04000
#define O_NOFOLLOW 0400000
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
    struct stat_local first,second,root,alias_out,alias_err,reopened;
    int descriptor,reopen1,reopen2,alias1,alias2,meta1=0,meta2=0,target1=0,target2=0,open1=22,open2=22,same1=0,same2=0,write1=22,write2=22,stdin_alias,fd_alias,pid1; i64 group_count,securebits;
    if(stack[0]!=2||(!same(argv[1],"service")&&!same(argv[1],"exec")))finish(90);
    descriptor=(int)sc3(2,(i64)"/proc/self/status",O_NOFOLLOW,0);if(descriptor<0)finish(91);
    for(;;){i64 count=sc3(0,descriptor,(i64)(status+total),sizeof(status)-total);if(count<0)finish(92);if(!count)break;total+=(u64)count;if(total==sizeof(status))finish(93);}
    if(sc1(3,descriptor))finish(94);
    if(!field_hex(status,total,"CapInh:\t",&capinh)||!field_hex(status,total,"CapPrm:\t",&capprm)||!field_hex(status,total,"CapEff:\t",&capeff)||!field_hex(status,total,"CapBnd:\t",&capbnd)||!field_hex(status,total,"CapAmb:\t",&capamb)||!field_decimal(status,total,"NoNewPrivs:\t",&nnp)||!field_decimal(status,total,"Seccomp:\t",&seccomp))finish(95);
    if(sc2(5,1,(i64)&first)||sc2(5,2,(i64)&second)||sc2(4,(i64)"/",(i64)&root))finish(96);
    descriptor=(int)sc3(2,(i64)"/proc/self/fd/1",O_WRONLY|O_APPEND|O_NONBLOCK|O_NOCTTY,0);reopen1=descriptor<0?-descriptor:0;if(descriptor>=0&&sc1(3,descriptor))finish(97);
    descriptor=(int)sc3(2,(i64)"/proc/self/fd/2",O_WRONLY|O_APPEND|O_NONBLOCK|O_NOCTTY,0);reopen2=descriptor<0?-descriptor:0;if(descriptor>=0&&sc1(3,descriptor))finish(98);
    descriptor=(int)sc4(262,AT_FDCWD,(i64)"/dev/stdout",(i64)&alias_out,AT_SYMLINK_NOFOLLOW);alias1=descriptor<0?-descriptor:(int)(alias_out.mode&S_IFMT);if(!descriptor){meta1=exact_alias(&alias_out);target1=exact_link("/dev/stdout","/proc/self/fd/1");}
    descriptor=(int)sc4(262,AT_FDCWD,(i64)"/dev/stderr",(i64)&alias_err,AT_SYMLINK_NOFOLLOW);alias2=descriptor<0?-descriptor:(int)(alias_err.mode&S_IFMT);if(!descriptor){meta2=exact_alias(&alias_err);target2=exact_link("/dev/stderr","/proc/self/fd/2");}
    if(meta1&&target1){descriptor=(int)sc3(2,(i64)"/dev/stdout",O_WRONLY|O_APPEND|O_CREAT|O_NONBLOCK|O_NOCTTY,0666);open1=descriptor<0?-descriptor:0;if(descriptor>=0){if(sc2(5,descriptor,(i64)&reopened))finish(97);same1=stat_identity(&first,&reopened);if(same1)write1=write_result(descriptor,same(argv[1],"service")?"PALIMPSEST_STDIO_ALIAS_V2 service stdout\n":"PALIMPSEST_STDIO_ALIAS_V2 exec stdout\n");if(sc1(3,descriptor))finish(97);}}
    if(meta2&&target2){descriptor=(int)sc3(2,(i64)"/dev/stderr",O_WRONLY|O_APPEND|O_CREAT|O_NONBLOCK|O_NOCTTY,0666);open2=descriptor<0?-descriptor:0;if(descriptor>=0){if(sc2(5,descriptor,(i64)&reopened))finish(98);same2=stat_identity(&second,&reopened);if(same2)write2=write_result(descriptor,same(argv[1],"service")?"PALIMPSEST_STDIO_ALIAS_V2 service stderr\n":"PALIMPSEST_STDIO_ALIAS_V2 exec stderr\n");if(sc1(3,descriptor))finish(98);}}
    descriptor=(int)sc4(262,AT_FDCWD,(i64)"/dev/stdin",(i64)&reopened,AT_SYMLINK_NOFOLLOW);stdin_alias=descriptor<0?-descriptor:0;
    descriptor=(int)sc4(262,AT_FDCWD,(i64)"/dev/fd",(i64)&reopened,AT_SYMLINK_NOFOLLOW);fd_alias=descriptor<0?-descriptor:0;
    descriptor=(int)sc3(2,(i64)"/proc/1/root/oci-public-root",O_NOFOLLOW,0);pid1=descriptor<0?-descriptor:0;if(descriptor>=0){sc1(3,descriptor);finish(99);}
    group_count=sc2(115,1,(i64)groups);if(group_count<0)finish(100);securebits=sc2(157,PR_GET_SECUREBITS,0);if(securebits<0)finish(101);
#define A(text) do{if(!append(output,&used,text))finish(102);}while(0)
#define N(key,value) do{if(!append_pair(output,&used,key,(u64)(value)))finish(102);}while(0)
    A("PALIMPSEST_STDIO_FD_V2 role=");A(argv[1]);N(" uid=",sc0(102));N(" gid=",sc0(104));N(" groups=",group_count);
    A(" capinh=");if(!append_hex16(output,&used,capinh))finish(102);A(" capprm=");if(!append_hex16(output,&used,capprm))finish(102);A(" capeff=");if(!append_hex16(output,&used,capeff))finish(102);A(" capbnd=");if(!append_hex16(output,&used,capbnd))finish(102);A(" capamb=");if(!append_hex16(output,&used,capamb))finish(102);
    N(" securebits=",securebits);N(" nnp=",nnp);N(" seccomp=",seccomp);
    N(" fd1type=",first.mode&0170000);N(" fd1mode=",first.mode&07777);N(" fd1uid=",first.uid);N(" fd1gid=",first.gid);N(" fd1dev=",first.dev);N(" fd1ino=",first.ino);N(" fd1reopen=",reopen1);
    N(" fd2type=",second.mode&0170000);N(" fd2mode=",second.mode&07777);N(" fd2uid=",second.uid);N(" fd2gid=",second.gid);N(" fd2dev=",second.dev);N(" fd2ino=",second.ino);N(" fd2reopen=",reopen2);
    N(" stdout_alias=",alias1);N(" stdout_meta=",meta1);N(" stdout_target=",target1);N(" stdout_open=",open1);N(" stdout_same=",same1);N(" stdout_write=",write1);
    N(" stderr_alias=",alias2);N(" stderr_meta=",meta2);N(" stderr_target=",target2);N(" stderr_open=",open2);N(" stderr_same=",same2);N(" stderr_write=",write2);
    N(" stdin_alias=",stdin_alias);N(" fd_alias=",fd_alias);N(" rootdev=",root.dev);N(" rootino=",root.ino);N(" pid1root=",pid1);A("\n");
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
