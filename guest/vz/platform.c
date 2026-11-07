/*
 * Palimpsest native VZ ARM64 platform, ABI palimpsest.vz-platform.v2.
 * SPDX-License-Identifier: Apache-2.0
 * Build on Linux/aarch64: musl-gcc -std=c11 -Os -static -Wall -Wextra
 *   -Werror -fstack-protector-strong -o plm-vz guest/vz/platform.c
 * This is an owner-root development VM, not KVM sandbox/security parity.
 * All platform dependencies remain outside the OCI root's libraries/accounts.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <inttypes.h>
#include <limits.h>
#include <linux/loop.h>
#include <linux/magic.h>
#include <linux/mount.h>
 #include <linux/vm_sockets.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/reboot.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/statvfs.h>
#include <sys/vfs.h>
#include <sys/sysmacros.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <sys/un.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include "sha256.h"
#include "json.h"

#define P "/.palimpsest"
#define VOL P "/state"
#define PS VOL "/platform-state"
#define RUN P "/run"
#define JAIL P "/control"
#define LIMA_USER "plmctl"
#define LIMA_UID 1000
#define LIMA_GID 1000
#define LIMA_HOME "/home/plmctl"
#define BB P "/bin/busybox"
#define SELF P "/bin/plm-vz"
#define DB P "/bin/dropbearmulti"
#define MAX_PLAN (1024*1024)
#define MAX_SESSIONS 32
#define QUIESCE_SECONDS 30
#define DEFAULT_PATH "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
#define PLM_PLATFORM_ABI "palimpsest.vz-platform.v2"

static bool fatal_safe_poweroff=true;
static _Noreturn void bad(const char *s){
 int e=errno;dprintf(2,"plm-vz: %s (errno=%d)\n",s,e);
 if(getpid()==1){
  sync();
  if(fatal_safe_poweroff){
   (void)reboot(RB_POWER_OFF);
   dprintf(2,"plm-vz: pre-handoff poweroff returned (errno=%d); custody retained\n",errno);
  }
  for(;;)pause();
 }
 _exit(125);
}
static void must(int ok,const char *s){if(!ok)bad(s);}
static void fmt(char *b,size_t n,const char *f,...){va_list a;int k;va_start(a,f);k=vsnprintf(b,n,f,a);va_end(a);must(k>=0&&(size_t)k<n,"path/output bound");}
static uint64_t mono(void){struct timespec t;must(!clock_gettime(CLOCK_MONOTONIC,&t),"monotonic clock");return (uint64_t)t.tv_sec;}
static uint64_t mono_ms(void){struct timespec t;must(!clock_gettime(CLOCK_MONOTONIC,&t),"monotonic millisecond clock");return (uint64_t)t.tv_sec*1000+(uint64_t)t.tv_nsec/1000000;}
static uint64_t wall(void){time_t t=time(NULL);must(t>0,"wall clock");return (uint64_t)t;}
static void allwrite(int fd,const void *v,size_t n){const char *p=v;while(n){ssize_t z=write(fd,p,n);if(z<0&&errno==EINTR)continue;must(z>0,"write");p+=z;n-=(size_t)z;}}
static void oom_score(int score){char value[32];fmt(value,sizeof value,"%d\n",score);int f=open("/proc/self/oom_score_adj",O_WRONLY|O_CLOEXEC|O_NOFOLLOW);must(f>=0,"OOM policy open");allwrite(f,value,strlen(value));must(!close(f),"OOM policy close");}
static void child_signal_defaults(void){signal(SIGTERM,SIG_DFL);signal(SIGHUP,SIG_DFL);signal(SIGINT,SIG_DFL);signal(SIGCHLD,SIG_DFL);signal(SIGPIPE,SIG_DFL);}
static void block_payload_signals(sigset_t *saved){sigset_t blocked;must(!sigemptyset(&blocked)&&!sigaddset(&blocked,SIGTERM)&&!sigaddset(&blocked,SIGHUP)&&!sigaddset(&blocked,SIGINT)&&!sigprocmask(SIG_BLOCK,&blocked,saved),"block payload setup signals");}
static void payload_signals(const sigset_t *saved,bool workload){child_signal_defaults();if(workload)oom_score(0);must(!sigprocmask(SIG_SETMASK,saved,NULL),"restore payload signals");}
static char *readfd(int fd,size_t cap,size_t *len){char *s=malloc(cap+1);size_t n=0;must(s!=NULL,"read allocation");while(n<=cap){ssize_t z=read(fd,s+n,cap+1-n);if(z<0&&errno==EINTR)continue;must(z>=0,"read");if(!z)break;n+=(size_t)z;must(n<=cap,"file size bound");}s[n]=0;if(len)*len=n;return s;}
static char *readfile(const char *p,size_t cap,size_t *n){int f=open(p,O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);struct stat st;char *s;must(f>=0,"open bounded file");must(!fstat(f,&st)&&S_ISREG(st.st_mode),"regular bounded file");s=readfd(f,cap,n);must(!close(f),"close file");return s;}
static void mkdir1(const char *p,mode_t mode){struct stat s;if(mkdir(p,mode)){must(errno==EEXIST&&!lstat(p,&s)&&S_ISDIR(s.st_mode)&&!S_ISLNK(s.st_mode),"mkdir safe directory");}else{int f=open(p,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);must(f>=0&&!fchmod(f,mode)&&!close(f),"new directory exact mode");}}
static void newfile(const char *p,const void *s,size_t n,mode_t mode){int f=open(p,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,mode);must(f>=0&&!fchmod(f,mode),"create file exact mode");allwrite(f,s,n);must(!fsync(f)&&!close(f),"sync private file");}
static void textfile(const char *p,const char *s,mode_t mode){newfile(p,s,strlen(s),mode);}
static void copyfile(const char *from,const char *to,mode_t mode){char b[65536];int a=open(from,O_RDONLY|O_CLOEXEC|O_NOFOLLOW),f;struct stat st;uint64_t total=0;must(a>=0&&!fstat(a,&st)&&S_ISREG(st.st_mode)&&st.st_size>0&&st.st_size<128*1024*1024,"platform asset source");f=open(to,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,mode);must(f>=0&&!fchmod(f,mode),"copy asset exact mode");for(;;){ssize_t n=read(a,b,sizeof b);if(n<0&&errno==EINTR)continue;must(n>=0,"copy asset read");if(!n)break;total+=(uint64_t)n;must(total<128*1024*1024,"asset copy bound");allwrite(f,b,(size_t)n);}must(!close(a)&&!close(f),"copy asset close");}
static void mnt(const char *s,const char *t,const char *fs,unsigned long flags,const char *data){
 if(syscall(SYS_mount,s,t,fs,flags,data)){
  int e=errno;
  dprintf(2,"plm-vz: mount failed source=%s target=%s fs=%s flags=0x%lx errno=%d\n",s?s:"(null)",t?t:"(null)",fs?fs:"(null)",flags,e);
  errno=e;bad("mount");
 }
}
/*
 * A pre-bind target FD still names the covered inode after attachment; remounting
 * /proc/self/fd/N would therefore miss the new mount root. On the supported 6.8
 * kernel, harden a detached clone before attaching it to the pinned target FD.
 */
static int readonly_bind_fd(const char *s,int target){
 int source=-1,tree=-1,result=-1,e;struct stat st;
 struct mount_attr attr={.attr_set=MOUNT_ATTR_RDONLY|MOUNT_ATTR_NOSUID|MOUNT_ATTR_NODEV};
 source=open(s,O_PATH|O_NOFOLLOW|O_CLOEXEC);if(source<0)goto out;
 if(fstat(source,&st))goto out;
 if(!S_ISREG(st.st_mode)&&!S_ISDIR(st.st_mode)){errno=EINVAL;goto out;}
 tree=(int)syscall(SYS_open_tree,source,"",OPEN_TREE_CLONE|OPEN_TREE_CLOEXEC|AT_EMPTY_PATH);if(tree<0)goto out;
 /* attr_clr stays zero: retain source noexec, atime and all other hardening. */
 if(syscall(SYS_mount_setattr,tree,"",AT_EMPTY_PATH,&attr,sizeof attr))goto out;
 if(syscall(SYS_move_mount,tree,"",target,"",MOVE_MOUNT_F_EMPTY_PATH|MOVE_MOUNT_T_EMPTY_PATH))goto out;
 result=0;
out:
 e=errno;
 if(tree>=0&&close(tree)&&!result){e=errno;result=-1;}
 if(source>=0&&close(source)&&!result){e=errno;result=-1;}
 errno=e;return result;
}
static void bindmount(const char *s,const char *t,int ro){
 if(!ro){mnt(s,t,NULL,MS_BIND,NULL);return;}
 int target=open(t,O_PATH|O_NOFOLLOW|O_CLOEXEC),result=-1,e;struct stat st;
 must(target>=0,"readonly bind target");
 if(!fstat(target,&st)){
  if(S_ISREG(st.st_mode)||S_ISDIR(st.st_mode))result=readonly_bind_fd(s,target);
  else errno=EINVAL;
 }
 e=errno;if(close(target)&&!result){e=errno;result=-1;}errno=e;must(!result,"readonly detached bind");
}
static int wait_child(pid_t p){int s;pid_t r;do{r=waitpid(p,&s,0);}while(r<0&&errno==EINTR);must(r==p,"wait child");return WIFEXITED(s)?WEXITSTATUS(s):128+WTERMSIG(s);}
static int command(char *const argv[],char *const env[]){pid_t p=fork();must(p>=0,"fork tool");if(!p){execve(argv[0],argv,env);_exit(127);}return wait_child(p);}
static char *const cleanenv[]={"PATH=/platform:/bin:/usr/bin","HOME=/root","USER=root","LOGNAME=root","LANG=C.UTF-8",NULL};
static unsigned guest_ptrace_scope(void){size_t n;char *b=readfile("/proc/sys/kernel/yama/ptrace_scope",16,&n);must(n==2&&b[0]>='0'&&b[0]<='3'&&b[1]=='\n',"guest Yama policy value");unsigned scope=(unsigned)(b[0]-'0');free(b);return scope;}
static void protect_bootstrap_identity(void){if(!guest_ptrace_scope()){int f=open("/proc/sys/kernel/yama/ptrace_scope",O_WRONLY|O_CLOEXEC|O_NOFOLLOW);must(f>=0,"guest Yama policy open");allwrite(f,"1\n",2);must(!close(f),"guest Yama policy close");}must(guest_ptrace_scope()>=1,"guest same-UID bootstrap protection");}
static char *const limaenv[]={"PATH=/platform:/bin:/usr/bin:/usr/local/bin","HOME=" LIMA_HOME,"USER=" LIMA_USER,"LOGNAME=" LIMA_USER,"LANG=C.UTF-8",NULL};
static void tool(char *const a[]){must(command(a,cleanenv)==0,"platform tool failed");}
static bool hexn(const char *s,size_t n){size_t i;if(strlen(s)!=n)return false;for(i=0;i<n;i++)if(!strchr("0123456789abcdef",s[i]))return false;return true;}
static bool uuid(const char *s){size_t i;if(strlen(s)!=36)return false;for(i=0;i<36;i++)if(i==8||i==13||i==18||i==23){if(s[i]!='-')return false;}else if(!strchr("0123456789abcdef",s[i]))return false;return true;}
static void digest(const char *s){must(!strncmp(s,"sha256:",7)&&hexn(s+7,64),"SHA256 spelling");}
static void hashfile(const char *path,uint64_t size,const char *expected){int f=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);struct stat st;struct sha256 h;char b[65536],out[65];uint64_t total=0;must(f>=0&&!fstat(f,&st)&&S_ISREG(st.st_mode)&&(uint64_t)st.st_size==size,"lower exact size/type");sha_init(&h);for(;;){ssize_t n=read(f,b,sizeof b);if(n<0&&errno==EINTR)continue;must(n>=0,"lower read");if(!n)break;total+=(uint64_t)n;must(total<=size,"lower grew");sha_add(&h,b,(size_t)n);}must(total==size,"lower shrank");sha_hex(&h,out);must(!strcmp(out,expected),"lower SHA256 mismatch");close(f);}
static bool account(const char *s){size_t i,n=strlen(s);if(!n||n>32)return false;if(isdigit((unsigned char)s[0])){uint64_t x=0;for(i=0;i<n;i++){if(!isdigit((unsigned char)s[i]))return false;x=x*10+(unsigned)(s[i]-'0');if(x>UINT32_MAX-1)return false;}return n==1||s[0]!='0';}if(!(isalpha((unsigned char)s[0])||s[0]=='_'))return false;for(i=1;i<n;i++)if(!(isalnum((unsigned char)s[i])||strchr("_.-",s[i])))return false;return true;}
static bool envname(const char *s){size_t i;if(!(isalpha((unsigned char)s[0])||s[0]=='_'))return false;for(i=1;s[i];i++)if(!(isalnum((unsigned char)s[i])||s[i]=='_'))return false;return true;}
static bool canonical_path(const char *p){const char *s=p;if(*s!='/'||strlen(s)>32768||strstr(s,"//"))return false;while(*s){const char *e=strchr(++s,'/');size_t n=e?(size_t)(e-s):strlen(s);if((n==1&&s[0]=='.')||(n==2&&!memcmp(s,"..",2)))return false;if(!e)break;s=e;}return strlen(p)==1||p[strlen(p)-1]!='/';}
struct process {char *argv[4097],*env[4097],*cwd,*user,*group;unsigned argc,envc;int stop;};
static size_t process_context_bytes(const struct process *p){size_t n=strlen(p->cwd)+strlen(p->user)+23+(p->group?strlen(p->group)+2:4);for(unsigned i=0;i<p->envc;i++)n+=strlen(p->env[i])+1;return n;}
struct layer {char *filename,*digest;uint64_t size;};
struct host_share {char *tag,*path;bool ro;};
struct plan {struct json json;char *bytes;char hash[65];char *run_id,*name,*volume,*graph,*source,*fsuuid,*nonce,*tag,*mode,*restart,*controlkey,*userkey,*hostkey;struct layer layers[128];unsigned nlayers;struct process proc;char *dns[8];unsigned ndns;uint64_t expiry,idle;struct host_share shares[8];unsigned nshares;};
static struct plan plan;
#define KEYS(j,o,...) do{const char *const names[]={__VA_ARGS__};keys((j),(o),names,sizeof names/sizeof names[0]);}while(0)
static bool overlaps(const char *a,const char *b){size_t n=strlen(a),m=strlen(b);return (!strncmp(a,b,n)&&(b[n]=='/'||b[n]==0))||(!strncmp(a,b,m)&&(a[m]=='/'||a[m]==0));}
static void parse_shares(struct json *j){
 int a=field(j,0,"host_shares"),k;const char *protected[]={P,"/proc","/sys","/dev","/run",NULL};
 must(j->t[a].kind=='['&&j->t[a].count<=8,"host share bound");
 for(k=j->t[a].child;k>=0&&k<j->t[a].next;k=j->t[k].next){
  struct host_share *s=&plan.shares[plan.nshares];int r;KEYS(j,k,"tag","mount_path","read_only");
  s->tag=string(j,field(j,k,"tag"));s->path=string(j,field(j,k,"mount_path"));r=field(j,k,"read_only");
  must(!strncmp(s->tag,"lima-",5)&&hexn(s->tag+5,16)&&strcmp(s->tag,plan.tag),"host share tag");must(canonical_path(s->path)&&strcmp(s->path,"/"),"host share path");
  must(j->t[r].kind=='t'||j->t[r].kind=='f',"host share writability");s->ro=j->t[r].kind=='t';
  for(unsigned i=0;protected[i];i++)must(!overlaps(s->path,protected[i]),"protected host share");
  for(unsigned i=0;i<plan.nshares;i++)must(strcmp(s->tag,plan.shares[i].tag)&&!overlaps(s->path,plan.shares[i].path),"overlapping host shares");
  plan.nshares++;
 }
}
static void parse_process(struct json *j,int o,struct process *p){
 int a,k,u;size_t total=0;bool path=false;KEYS(j,o,"argv","environment","cwd","user","stop_signal");
 a=field(j,o,"argv");must(j->t[a].kind=='['&&j->t[a].count<=4096,"process argv");
 for(k=j->t[a].child;k>=0&&k<j->t[a].next;k=j->t[k].next){p->argv[p->argc++]=string(j,k);total+=strlen(string(j,k))+1;}p->argv[p->argc]=NULL;
 a=field(j,o,"environment");must(j->t[a].kind=='['&&j->t[a].count<=4096,"process environment");
 for(k=j->t[a].child;k>=0&&k<j->t[a].next;k=j->t[k].next){
  char *n,*v,*entry;KEYS(j,k,"name","value");n=string(j,field(j,k,"name"));v=string(j,field(j,k,"value"));must(envname(n),"environment name");
  for(unsigned i=0;i<p->envc;i++)must(strncmp(p->env[i],n,strlen(n))||p->env[i][strlen(n)]!='=',"duplicate environment name");
  entry=malloc(strlen(n)+strlen(v)+2);must(entry!=NULL,"environment allocation");sprintf(entry,"%s=%s",n,v);p->env[p->envc++]=entry;if(!strcmp(n,"PATH"))path=true;
 }
 p->env[p->envc]=NULL;must(path,"canonical process missing PATH");p->cwd=string(j,field(j,o,"cwd"));must(canonical_path(p->cwd),"process cwd");
 u=field(j,o,"user");KEYS(j,u,"user","group");p->user=string(j,field(j,u,"user"));must(account(p->user),"process user");k=field(j,u,"group");if(j->t[k].kind!='0'){p->group=string(j,k);must(account(p->group),"process group");}
 p->stop=(int)integer(j,field(j,o,"stop_signal"),64);must(p->stop>0&&p->stop!=SIGKILL,"process stop signal/no-force policy");
 must(total+process_context_bytes(p)<=256*1024,"process bytes");
}
static void keycheck(const char *s){
 static const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
 static const unsigned char header[19]={0,0,0,11,'s','s','h','-','e','d','2','5','5','1','9',0,0,0,32};
 unsigned char decoded[51];unsigned acc=0,bits=0,used=0;size_t n=strlen(s);must(n>=80&&n<512&&!strncmp(s,"ssh-ed25519 ",12)&&(s[80]==0||s[80]==' '),"ed25519 public key");
 for(unsigned i=12;i<80;i++){const char *p=strchr(alphabet,s[i]);must(p!=NULL,"ed25519 key encoding");acc=(acc<<6)|(unsigned)(p-alphabet);bits+=6;if(bits>=8){bits-=8;decoded[used++]=(unsigned char)(acc>>bits);acc&=(1U<<bits)-1;}}
 must(used==sizeof decoded&&bits==0&&!memcmp(decoded,header,sizeof header),"ed25519 key structure");
 for(const char *p=s+80;*p;p++)must((unsigned char)*p>=32&&(unsigned char)*p<127&&*p!='"'&&*p!='\\',"public key comment");
}
static void parse_plan(char *bytes,size_t n,const char *expected){
 struct json *j=&plan.json;struct sha256 h;int a,k;unsigned i;
 sha_init(&h);sha_add(&h,bytes,n);sha_hex(&h,plan.hash);must(!strcmp(plan.hash,expected),"boot plan SHA256");plan.bytes=bytes;*j=js_parse(bytes,n);
 KEYS(j,0,"schema","run_id","run_name","root_volume_id","root_graph_digest","source_snapshot_binding_digest","root_filesystem_uuid","root_nonce","lower_share_tag","layers","process","mode","restart","idle_grace_seconds","expires_unix","network","access","host_shares");
 must(!strcmp(string(j,field(j,0,"schema")),"palimpsest.vz-root-plan.v1"),"plan schema");
 plan.run_id=string(j,field(j,0,"run_id"));plan.name=string(j,field(j,0,"run_name"));plan.volume=string(j,field(j,0,"root_volume_id"));plan.fsuuid=string(j,field(j,0,"root_filesystem_uuid"));plan.nonce=string(j,field(j,0,"root_nonce"));plan.graph=string(j,field(j,0,"root_graph_digest"));plan.source=string(j,field(j,0,"source_snapshot_binding_digest"));plan.tag=string(j,field(j,0,"lower_share_tag"));
 must(uuid(plan.run_id)&&uuid(plan.volume)&&uuid(plan.fsuuid)&&!strcmp(plan.volume,plan.fsuuid)&&hexn(plan.nonce,64),"plan identities");digest(plan.graph);digest(plan.source);
 must(strlen(plan.name)>0&&strlen(plan.name)<=63,"run name bound");for(i=0;plan.name[i];i++)must(isalnum((unsigned char)plan.name[i])||plan.name[i]=='-',"run name");
 must(!strncmp(plan.tag,"lima-",5)&&hexn(plan.tag+5,16),"lower virtiofs tag");parse_shares(j);
 a=field(j,0,"layers");must(j->t[a].kind=='['&&j->t[a].count>0&&j->t[a].count<=128,"layer count");
 for(k=j->t[a].child;k>=0&&k<j->t[a].next;k=j->t[k].next){
  struct layer *l=&plan.layers[plan.nlayers];char expectedfile[80];KEYS(j,k,"ordinal","filename","digest","size");
  must(integer(j,field(j,k,"ordinal"),127)==plan.nlayers,"layer ordinal");l->digest=string(j,field(j,k,"digest"));digest(l->digest);l->filename=string(j,field(j,k,"filename"));fmt(expectedfile,sizeof expectedfile,"%s.sqfs",l->digest+7);must(!strcmp(l->filename,expectedfile),"layer filename");l->size=integer(j,field(j,k,"size"),UINT64_C(1)<<40);must(l->size>=4096,"layer size");plan.nlayers++;
 }
 parse_process(j,field(j,0,"process"),&plan.proc);plan.mode=string(j,field(j,0,"mode"));plan.restart=string(j,field(j,0,"restart"));
 must((!strcmp(plan.mode,"dev")||!strcmp(plan.mode,"prod"))&&(!strcmp(plan.restart,"no")||!strcmp(plan.restart,"unless-stopped")),"mode/restart policy");
 if(!strcmp(plan.mode,"prod"))must(plan.proc.argc&&plan.proc.argv[0][0],"prod requires process");
 plan.idle=integer(j,field(j,0,"idle_grace_seconds"),86400);must(plan.idle>0,"idle grace");
 k=field(j,0,"expires_unix");if(j->t[k].kind!='0'){plan.expiry=integer(j,k,INT64_MAX);must(plan.expiry>0,"runtime expiry bound");}
 a=field(j,0,"network");KEYS(j,a,"dns");a=field(j,a,"dns");must(j->t[a].kind=='['&&j->t[a].count>0&&j->t[a].count<=8,"DNS count");
 for(k=j->t[a].child;k>=0&&k<j->t[a].next;k=j->t[k].next){unsigned char addr[16];char *s=string(j,k);must(inet_pton(AF_INET,s,addr)==1||inet_pton(AF_INET6,s,addr)==1,"DNS address");plan.dns[plan.ndns++]=s;}
 a=field(j,0,"access");KEYS(j,a,"control_authorized_key","user_authorized_key","host_public_key");plan.controlkey=string(j,field(j,a,"control_authorized_key"));plan.userkey=string(j,field(j,a,"user_authorized_key"));plan.hostkey=string(j,field(j,a,"host_public_key"));keycheck(plan.controlkey);keycheck(plan.userkey);keycheck(plan.hostkey);must(memcmp(plan.controlkey,plan.userkey,80),"key classes must differ by key bytes");
}
static void distinct_seed_tag(const char *tag){must(strcmp(tag,plan.tag),"seed/lower share tag collision");for(unsigned i=0;i<plan.nshares;i++)must(strcmp(tag,plan.shares[i].tag),"seed/host share tag collision");}

/* Mount construction occurs before any workload or SSH process is admitted. */
static void early_mounts(void){
 mkdir1("/proc",0555);mnt("proc","/proc","proc",MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL);
 mkdir1("/sys",0555);mnt("sysfs","/sys","sysfs",MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL);
 mkdir1("/dev",0755);mnt("devtmpfs","/dev","devtmpfs",MS_NOSUID,"mode=0755");
 {int f=open("/dev/console",O_RDWR|O_NOCTTY|O_CLOEXEC);must(f>=0,"open actual guest console");for(int i=0;i<3;i++)must(dup2(f,i)>=0&&!fcntl(i,F_SETFD,0),"initialize guest stdio");if(f>2)close(f);}oom_score(-1000);
 protect_bootstrap_identity();
 mkdir1(P,0700);mnt("tmpfs",P,"tmpfs",MS_NOSUID|MS_NODEV,"mode=0700,size=256m,nr_inodes=16384");
 mkdir1(P "/bin",0700);mkdir1(P "/seed",0700);mkdir1(P "/lower-input",0700);mkdir1(P "/lowers",0700);mkdir1(VOL,0700);mkdir1(RUN,0700);mkdir1(JAIL,0700);
 copyfile("/platform/plm-vz",SELF,0755);copyfile("/platform/busybox",BB,0755);copyfile("/platform/dropbearmulti",DB,0755);copyfile("/platform/lima-guestagent",P "/bin/lima-guestagent",0755);
 {size_t n;unsigned count=0;char *b=readfile("/platform/modules.load",512*1024,&n),*save,*name;must(!n||b[n-1]=='\n',"module load list");for(name=strtok_r(b,"\n",&save);name;name=strtok_r(NULL,"\n",&save)){must(++count<=4096&&strlen(name)<=128,"module load bound");for(char *p=name;*p;p++)must(isalnum((unsigned char)*p)||*p=='_',"module load name");char *a[]={"/platform/busybox","modprobe",name,NULL};tool(a);}free(b);}
}
static void cmdline(char tag[32],char hash[65]){
 size_t n;char *s=readfile("/proc/cmdline",8192,&n),*save,*p;unsigned tags=0,hashes=0;(void)n;
 for(p=strtok_r(s," \t\n",&save);p;p=strtok_r(NULL," \t\n",&save)){
  if(!strncmp(p,"plm.seed_tag=",13)){must(++tags==1&&strlen(p+13)<32,"seed tag cmdline");strcpy(tag,p+13);}
  if(!strncmp(p,"plm.plan_sha256=",16)){must(++hashes==1&&hexn(p+16,64),"plan hash cmdline");strcpy(hash,p+16);}
 }
 must(tags==1&&hashes==1&&!strncmp(tag,"lima-",5)&&hexn(tag+5,16),"authenticated cmdline");free(s);
}
static void ext4_uuid(const char *device,char out[37]){
 unsigned char b[1024];int f=open(device,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);ssize_t n;struct stat st;
 must(f>=0&&!fstat(f,&st)&&S_ISBLK(st.st_mode),"block candidate");n=pread(f,b,sizeof b,1024);close(f);out[0]=0;
 if(n!=(ssize_t)sizeof b||b[56]!=0x53||b[57]!=0xef)return;
 fmt(out,37,"%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",b[104],b[105],b[106],b[107],b[108],b[109],b[110],b[111],b[112],b[113],b[114],b[115],b[116],b[117],b[118],b[119]);
}
static void syncdir(const char *path){int f=open(path,O_DIRECTORY|O_RDONLY|O_NOFOLLOW|O_CLOEXEC);must(f>=0&&!fsync(f)&&!close(f),"durable directory");}
static bool directory_exists(const char *p){struct stat st;if(lstat(p,&st)){must(errno==ENOENT,"directory inspection");return false;}must(S_ISDIR(st.st_mode)&&st.st_uid==0&&(st.st_mode&077)==0,"private safe directory");return true;}
static bool fresh_upper,first_boot;
static bool upper_exists(void){struct stat st,root;if(lstat(VOL "/upper",&st)){must(errno==ENOENT,"upper directory inspection");return false;}must(!stat(VOL,&root)&&S_ISDIR(st.st_mode)&&st.st_dev==root.st_dev,"upper directory type/filesystem");return true;}
static void upper_mount(void){
 DIR *d;struct dirent *e;char selected[PATH_MAX]="";unsigned count=0,tries;char actual[37];
 for(tries=0;tries<100;tries++){
  d=opendir("/sys/class/block");must(d!=NULL,"block enumeration");count=0;
  while((e=readdir(d))){char dev[PATH_MAX];if(e->d_name[0]=='.')continue;fmt(dev,sizeof dev,"/dev/%s",e->d_name);struct stat st;if(lstat(dev,&st)||!S_ISBLK(st.st_mode))continue;ext4_uuid(dev,actual);if(!strcmp(actual,plan.fsuuid)){count++;strcpy(selected,dev);}}
  closedir(d);must(count<=1,"duplicate upper filesystem UUID");if(count)break;usleep(100000);
 }
 must(count==1,"unique upper filesystem absent");mnt(selected,VOL,"ext4",MS_NOSUID|MS_NODEV,"errors=remount-ro");
 {char *b;size_t n;struct json j;char *phase;bool upper,work;
  must(directory_exists(PS),"upper private state absent");b=readfile(PS "/binding.json",8192,&n);j=js_parse(b,n);
  KEYS(&j,0,"schema","run_id","root_volume_id","root_graph_digest","root_nonce","phase");
  must(!strcmp(string(&j,field(&j,0,"schema")),"palimpsest.vz-upper-binding.v1")&&!strcmp(string(&j,field(&j,0,"run_id")),plan.run_id)&&!strcmp(string(&j,field(&j,0,"root_volume_id")),plan.volume)&&!strcmp(string(&j,field(&j,0,"root_graph_digest")),plan.graph)&&!strcmp(string(&j,field(&j,0,"root_nonce")),plan.nonce),"upper binding mismatch");
  phase=string(&j,field(&j,0,"phase"));upper=upper_exists();work=directory_exists(VOL "/work");
  if(!strcmp(phase,"prepared")){must(!upper&&!work,"prepared upper is not pristine");fresh_upper=true;}
  else must(!strcmp(phase,"initialized")&&upper&&work,"uninitialized/partial resume");
  js_free(&j);free(b);
 }
}
static void upper_initialize(const char *lower){
 int source,target;struct stat st;char marker[1024],names[65536],value[65536];ssize_t n;size_t used=0;unsigned count=0;
 if(!fresh_upper)return;
 /* A crash before marker consumption leaves prepared+partial and fails resume. */
 must(!mkdir(VOL "/upper",0700)&&!mkdir(VOL "/work",0700),"initialize upper/work");first_boot=true;
 source=open(lower,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);target=open(VOL "/upper",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
 must(source>=0&&target>=0&&!fstat(source,&st),"initial root metadata");
 must(!fchown(target,st.st_uid,st.st_gid)&&!fchmod(target,st.st_mode&07777),"preserve image root identity/mode");
 n=flistxattr(source,names,sizeof names);must(n>=0||(errno==ENOTSUP),"image root xattr list");if(n<0)n=0;
 for(size_t i=0;i<(size_t)n;){
  size_t len=strnlen(names+i,(size_t)n-i);must(len>0&&len<(size_t)n-i&&len<=255&&++count<=256,"root xattr name bound");
  const char *name=names+i;i+=len+1;must(strncmp(name,"trusted.overlay.",16)&&strncmp(name,"user.overlay.",13),"unexpected root overlay metadata");
  ssize_t z=fgetxattr(source,name,value,sizeof value);must(z>=0&&used+(size_t)z<=256*1024,"root xattr value bound");used+=(size_t)z;must(!fsetxattr(target,name,value,(size_t)z,0),"preserve image root xattr");
 }
 {struct timespec times[2]={st.st_atim,st.st_mtim};must(!futimens(target,times)&&!fsync(target),"preserve/sync root timestamps");}
 must(!close(source)&&!close(target),"root metadata handles");syncdir(VOL "/work");syncdir(VOL);
 fmt(marker,sizeof marker,"{\"schema\":\"palimpsest.vz-upper-binding.v1\",\"run_id\":\"%s\",\"root_volume_id\":\"%s\",\"root_graph_digest\":\"%s\",\"root_nonce\":\"%s\",\"phase\":\"initialized\"}\n",plan.run_id,plan.volume,plan.graph,plan.nonce);
 textfile(PS "/binding.next",marker,0600);must(!rename(PS "/binding.next",PS "/binding.json"),"consume first-use binding");syncdir(PS);fresh_upper=false;
}
static void reserved_namespace(void){
 struct stat st;char *bytes;size_t n;struct json j;
 if(first_boot){must(lstat("/newroot/.palimpsest",&st)&&errno==ENOENT,"reserved image namespace collision");must(!mkdir("/newroot/.palimpsest",0700),"reserved private placeholder");}
 must(!lstat(VOL "/upper/.palimpsest",&st)&&S_ISDIR(st.st_mode)&&st.st_uid==0&&(st.st_mode&07777)==0700,"reserved upper placeholder type/owner");
 DIR *d=opendir(VOL "/upper/.palimpsest");struct dirent *e;must(d!=NULL,"reserved placeholder enumeration");while((e=readdir(d)))must(!strcmp(e->d_name,".")||!strcmp(e->d_name,".."),"reserved placeholder not empty");closedir(d);
 if(first_boot){char b[512];fmt(b,sizeof b,"{\"schema\":\"palimpsest.vz-reserved-namespace.v1\",\"run_id\":\"%s\",\"root_nonce\":\"%s\",\"inode\":%ju}\n",plan.run_id,plan.nonce,(uintmax_t)st.st_ino);textfile(PS "/reserved-namespace.json",b,0600);syncdir(VOL "/upper/.palimpsest");syncdir(VOL "/upper");syncdir(PS);}
 else{bytes=readfile(PS "/reserved-namespace.json",8192,&n);j=js_parse(bytes,n);KEYS(&j,0,"schema","run_id","root_nonce","inode");must(!strcmp(string(&j,field(&j,0,"schema")),"palimpsest.vz-reserved-namespace.v1")&&!strcmp(string(&j,field(&j,0,"run_id")),plan.run_id)&&!strcmp(string(&j,field(&j,0,"root_nonce")),plan.nonce)&&integer(&j,field(&j,0,"inode"),UINT64_MAX)==st.st_ino,"retained reserved namespace binding");js_free(&j);free(bytes);}
}
static void lowers_mount(void){
 unsigned i;char opts[32768]="lowerdir=";size_t used=strlen(opts);mnt(plan.tag,P "/lower-input","virtiofs",MS_RDONLY|MS_NOSUID|MS_NODEV,NULL);
 /* Exact allowlist: repeated occurrences share a payload but never lose ordinal. */
 {DIR *d=opendir(P "/lower-input");struct dirent *e;unsigned seen=0;must(d!=NULL,"lower share enumeration");while((e=readdir(d))){bool found=false;if(!strcmp(e->d_name,".")||!strcmp(e->d_name,".."))continue;for(i=0;i<plan.nlayers;i++)if(!strcmp(e->d_name,plan.layers[i].filename))found=true;must(found,"unexpected lower share entry");seen++;}closedir(d);must(seen>0&&seen<=plan.nlayers,"lower share count");}
 for(i=0;i<plan.nlayers;i++){
  char file[PATH_MAX],target[PATH_MAX],loop[64];int ctl,idx,fd,img;struct loop_info64 info;
  fmt(file,sizeof file,P "/lower-input/%s",plan.layers[i].filename);hashfile(file,plan.layers[i].size,plan.layers[i].digest+7);fmt(target,sizeof target,P "/lowers/%03u",i);mkdir1(target,0700);
  ctl=open("/dev/loop-control",O_RDWR|O_CLOEXEC);must(ctl>=0,"loop-control");idx=ioctl(ctl,LOOP_CTL_GET_FREE);close(ctl);must(idx>=0,"free loop");fmt(loop,sizeof loop,"/dev/loop%d",idx);fd=open(loop,O_RDWR|O_CLOEXEC);img=open(file,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);must(fd>=0&&img>=0,"open readonly loop");must(!ioctl(fd,LOOP_SET_FD,img),"attach lower loop");memset(&info,0,sizeof info);info.lo_flags=LO_FLAGS_READ_ONLY|LO_FLAGS_AUTOCLEAR;must(!ioctl(fd,LOOP_SET_STATUS64,&info),"readonly loop flags");mnt(loop,target,"squashfs",MS_RDONLY|MS_NOSUID|MS_NODEV,NULL);close(img);close(fd);
 }
 {char highest[PATH_MAX];fmt(highest,sizeof highest,P "/lowers/%03u",plan.nlayers-1);upper_initialize(highest);}
 for(i=plan.nlayers;i>0;i--){char part[96];int z;fmt(part,sizeof part,"%s" P "/lowers/%03u",i==plan.nlayers?"":":",i-1);z=snprintf(opts+used,sizeof opts-used,"%s",part);must(z>=0&&(size_t)z<sizeof opts-used,"overlay options bound");used+=(size_t)z;}
 {int n=snprintf(opts+used,sizeof opts-used,",upperdir=" VOL "/upper,workdir=" VOL "/work");must(n>=0&&(size_t)n<sizeof opts-used,"upper options");}
 mkdir1("/newroot",0700);mnt("overlay","/newroot","overlay",0,opts);
 reserved_namespace();
}
static void root_transition(void){
 /* Autoloading must never invoke a workload image's /sbin/modprobe as root. */
 {int f=open("/proc/sys/kernel/modprobe",O_WRONLY|O_CLOEXEC|O_NOFOLLOW);must(f>=0,"module autoload fence");allwrite(f,"\n",1);must(!close(f),"module autoload fence close");}
 mkdir1("/newroot/proc",0555);mkdir1("/newroot/sys",0555);mkdir1("/newroot/dev",0755);
 mnt("/proc","/newroot/proc",NULL,MS_MOVE,NULL);mnt("/sys","/newroot/sys",NULL,MS_MOVE,NULL);mnt("/dev","/newroot/dev",NULL,MS_MOVE,NULL);
 mnt(P,"/newroot/.palimpsest",NULL,MS_MOVE,NULL);
 must(!chdir("/newroot"),"newroot chdir");mnt(".","/",NULL,MS_MOVE,NULL);must(!chroot(".")&&!chdir("/"),"PID1 actual-root transition");
}
static void root_proof(void){struct stat a,b;struct statfs fs;must(!stat("/",&a)&&!stat("/proc/1/root/.",&b)&&a.st_dev==b.st_dev&&a.st_ino==b.st_ino&&!statfs("/",&fs)&&(unsigned long)fs.f_type==OVERLAYFS_SUPER_MAGIC,"PID1 actual OverlayFS root mismatch");}
/* Resolve symlinks in the verified root, never through kernel magic mounts. */
static void path_normalize(const char *input,char out[PATH_MAX]){
 char copy[PATH_MAX],*save,*part;size_t used=0;must(input[0]=='/'&&strlen(input)<sizeof copy,"root symlink path bound");strcpy(copy,input);out[0]=0;
 for(part=strtok_r(copy,"/",&save);part;part=strtok_r(NULL,"/",&save)){
  if(!strcmp(part,"."))continue;
  if(!strcmp(part,"..")){must(used>0,"root symlink escape");while(used&&out[--used]!='/'){}out[used]=0;continue;}
  size_t n=strlen(part);must(n<=NAME_MAX&&used+n+2<PATH_MAX,"normalized path bound");out[used++]='/';memcpy(out+used,part,n+1);used+=n;
 }
 if(!used)strcpy(out,"/");
}
static void json_quote(const char *s,char *out,size_t cap){
 size_t n=0;must(cap>2,"JSON quote capacity");out[n++]='"';
 for(;*s;s++){unsigned char c=(unsigned char)*s;if(c=='"'||c=='\\'){must(n+2<cap,"JSON quote bound");out[n++]='\\';out[n++]=(char)c;}else if(c<32){must(n+6<cap,"JSON quote bound");int z=snprintf(out+n,cap-n,"\\u%04x",c);must(z==6,"JSON quote");n+=6;}else{must(n+1<cap,"JSON quote bound");out[n++]=(char)c;}}
 must(n+2<=cap,"JSON quote suffix");out[n++]='"';out[n]=0;
}
static void record_placeholder(const char *path,bool directory){
 char quoted[PATH_MAX*6+3],record[PATH_MAX*6+80];int f;json_quote(path,quoted,sizeof quoted);fmt(record,sizeof record,"{\"path\":%s,\"directory\":%s}\n",quoted,directory?"true":"false");
 f=open(RUN "/generated-placeholders.jsonl",O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);must(f>=0,"placeholder record");allwrite(f,record,strlen(record));must(!close(f),"placeholder record close");
}
/* Missing image account files are valid only to callers selecting optional reads. */
static int root_open(const char *path,int flags,bool create){
 int dir=open("/",O_DIRECTORY|O_RDONLY|O_CLOEXEC),f;char todo[PATH_MAX],resolved[PATH_MAX]="",part[NAME_MAX+1];unsigned links=0,steps=0;
 must(dir>=0&&canonical_path(path)&&strlen(path)<sizeof todo,"rooted path");strcpy(todo,path+1);
 while(todo[0]){
  char *slash=strchr(todo,'/'),rest[PATH_MAX],full[PATH_MAX];size_t n=slash?(size_t)(slash-todo):strlen(todo);struct stat st;
  must(n>0&&n<=NAME_MAX&&++steps<256,"rooted path component");memcpy(part,todo,n);part[n]=0;strcpy(rest,slash?slash+1:"");fmt(full,sizeof full,"%s/%s",resolved,part);
  if(!resolved[0])must(strcmp(part,".palimpsest")&&strcmp(part,"proc")&&strcmp(part,"sys")&&strcmp(part,"dev"),"private/pseudo image path forbidden");
  if(fstatat(dir,part,&st,AT_SYMLINK_NOFOLLOW)){
   must(errno==ENOENT,"image target inspection");
   if(!create){close(dir);return -1;}
   bool directory=rest[0]||(flags&O_DIRECTORY);record_placeholder(full,directory);
   if(directory){must(!mkdirat(dir,part,0755),"generated image directory");f=openat(dir,part,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);must(f>=0&&!fchmod(f,0755)&&!close(f)&&!fstatat(dir,part,&st,AT_SYMLINK_NOFOLLOW),"generated directory exact mode");}
   else{f=openat(dir,part,O_CREAT|O_EXCL|O_RDWR|O_NOFOLLOW|O_CLOEXEC,0644);must(f>=0&&!fchmod(f,0644),"generated bind placeholder");close(dir);return f;}
  }
  if(S_ISLNK(st.st_mode)){
   char link[PATH_MAX],combined[PATH_MAX],normal[PATH_MAX];ssize_t z;must(++links<=16,"rooted symlink bound");z=readlinkat(dir,part,link,sizeof link-1);must(z>0&&(size_t)z<sizeof link-1,"rooted symlink");link[z]=0;
   fmt(combined,sizeof combined,"%s%s%s%s%s",link[0]=='/'?"":resolved,link[0]=='/'?"":"/",link,rest[0]?"/":"",rest);path_normalize(combined,normal);
   close(dir);dir=open("/",O_DIRECTORY|O_RDONLY|O_CLOEXEC);must(dir>=0,"root symlink anchor");resolved[0]=0;strcpy(todo,normal+1);continue;
  }
  if(!rest[0]){must((flags&O_DIRECTORY)?S_ISDIR(st.st_mode):S_ISREG(st.st_mode),"rooted final type");f=openat(dir,part,flags|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC);must(f>=0&&!fstat(f,&st)&&((flags&O_DIRECTORY)?S_ISDIR(st.st_mode):S_ISREG(st.st_mode)),"open rooted final");close(dir);return f;}
  must(S_ISDIR(st.st_mode),"rooted parent directory");f=openat(dir,part,O_DIRECTORY|O_RDONLY|O_NOFOLLOW|O_CLOEXEC);must(f>=0,"open rooted directory");close(dir);dir=f;strcpy(resolved,full);strcpy(todo,rest);
 }
 must(flags&O_DIRECTORY,"empty rooted file path");return dir;
}
static void host_shares_mount(void){
 for(unsigned i=0;i<plan.nshares;i++){struct host_share *s=&plan.shares[i];int fd=root_open(s->path,O_DIRECTORY|O_RDONLY,true);char target[64],resolved[PATH_MAX];fmt(target,sizeof target,"/proc/self/fd/%d",fd);
  ssize_t n=readlink(target,resolved,sizeof resolved-1);must(n>0&&(size_t)n<sizeof resolved-1,"host share canonical target");resolved[n]=0;must(!strcmp(resolved,s->path),"host share symlink target forbidden");
  mnt(s->tag,target,"virtiofs",MS_NOSUID|MS_NODEV|(s->ro?MS_RDONLY:0),NULL);close(fd);
 }
}
static void inject_file(const char *path,const char *name,const char *data){
 char src[PATH_MAX],target[64],record[PATH_MAX];int fd;struct stat st;fmt(src,sizeof src,RUN "/%s",name);textfile(src,data,0644);fd=root_open(path,O_RDONLY,true);must(fd>=0&&!fstat(fd,&st)&&S_ISREG(st.st_mode),"injection regular target");fmt(target,sizeof target,"/proc/self/fd/%d",fd);if(readonly_bind_fd(src,fd)){int e=errno;close(fd);errno=e;bad("readonly injection bind");}fmt(record,sizeof record,RUN "/injection-%s",name);{char b[PATH_MAX*6+160],resolved[PATH_MAX],quoted[PATH_MAX*6+3];ssize_t n=readlink(target,resolved,sizeof resolved-1);must(n>0&&(size_t)n<sizeof resolved-1,"injection canonical target");resolved[n]=0;json_quote(resolved,quoted,sizeof quoted);fmt(b,sizeof b,"{\"path\":%s,\"device\":%ju,\"inode\":%ju}\n",quoted,(uintmax_t)st.st_dev,(uintmax_t)st.st_ino);textfile(record,b,0600);}close(fd);
}
static void identities(void){
 char b[2048],machine[33],host[128];unsigned i;size_t n=0,j=0;for(i=0;plan.run_id[i];i++)if(plan.run_id[i]!='-')machine[j++]=plan.run_id[i];machine[j]=0;fmt(b,sizeof b,"%s\n",machine);inject_file("/etc/machine-id","machine-id",b);
 {struct stat st;if(!lstat("/var/lib/dbus",&st)&&S_ISDIR(st.st_mode)){int fd=root_open("/var/lib/dbus/machine-id",O_RDONLY,true);close(fd);inject_file("/var/lib/dbus/machine-id","dbus-machine-id",b);}}
 fmt(host,sizeof host,"%s\n",plan.name);must(!sethostname(plan.name,strlen(plan.name)),"set guest hostname");inject_file("/etc/hostname","hostname",host);
 b[0]=0;for(i=0;i<plan.ndns;i++){int z=snprintf(b+n,sizeof b-n,"nameserver %s\n",plan.dns[i]);must(z>0&&(size_t)z<sizeof b-n,"resolver bound");n+=(size_t)z;}inject_file("/etc/resolv.conf","resolv.conf",b);
 mkdir1("/dev/pts",0755);mnt("devpts","/dev/pts","devpts",MS_NOSUID|MS_NOEXEC,"newinstance,ptmxmode=0666,mode=0620");if(access("/dev/ptmx",F_OK))must(!symlink("pts/ptmx","/dev/ptmx"),"dev ptmx");else bindmount("/dev/pts/ptmx","/dev/ptmx",0);
}

static void image_ids(struct process *p,uid_t *uid,gid_t *gid){
 bool numeric=isdigit((unsigned char)p->user[0]),need=!numeric||!p->group;unsigned matches=0;uint64_t u=numeric?strtoull(p->user,NULL,10):0,g=0;char *b=NULL,*save,*line;size_t n;
 if(need){
  int f=root_open("/etc/passwd",O_RDONLY,false);if(f<0){must(numeric,"named user passwd absent");b=strdup("");must(b!=NULL,"empty passwd allocation");n=0;}else{b=readfd(f,65536,&n);close(f);}
  must(!n||b[n-1]=='\n',"passwd unterminated");for(size_t i=0;i<n;i++)must((unsigned char)b[i]<128&&b[i],"passwd ASCII");
  for(line=strtok_r(b,"\n",&save);line;line=strtok_r(NULL,"\n",&save)){char *fields[7],*v=line;unsigned i;if(!line[0]||line[0]=='#')continue;for(i=0;i<7;i++){fields[i]=strsep(&v,":");must(fields[i]!=NULL,"passwd field count");}must(v==NULL&&account(fields[0])&&!isdigit((unsigned char)fields[0][0])&&account(fields[2])&&isdigit((unsigned char)fields[2][0])&&account(fields[3])&&isdigit((unsigned char)fields[3][0]),"passwd record");uint64_t a=strtoull(fields[2],NULL,10),c=strtoull(fields[3],NULL,10);if(numeric?a==u:!strcmp(fields[0],p->user)){matches++;u=a;g=c;}}
  free(b);must(matches<=1&&(numeric||matches==1),"passwd identity absent/ambiguous");
 }
 if(p->group){if(isdigit((unsigned char)p->group[0]))g=strtoull(p->group,NULL,10);else{
  int f=root_open("/etc/group",O_RDONLY,false);must(f>=0,"named group database absent");matches=0;b=readfd(f,65536,&n);close(f);must(!n||b[n-1]=='\n',"group unterminated");for(size_t i=0;i<n;i++)must((unsigned char)b[i]<128&&b[i],"group ASCII");
  for(line=strtok_r(b,"\n",&save);line;line=strtok_r(NULL,"\n",&save)){char *fields[4],*v=line;unsigned i;if(!line[0]||line[0]=='#')continue;for(i=0;i<4;i++){fields[i]=strsep(&v,":");must(fields[i]!=NULL,"group field count");}must(v==NULL&&account(fields[0])&&!isdigit((unsigned char)fields[0][0])&&account(fields[2])&&isdigit((unsigned char)fields[2][0]),"group record");if(!strcmp(fields[0],p->group)){matches++;g=strtoull(fields[2],NULL,10);}}
  free(b);must(matches==1,"group identity absent/ambiguous");
 }}
 must(u<=UINT32_MAX-1&&g<=UINT32_MAX-1,"identity integer bound");*uid=(uid_t)u;*gid=(gid_t)g;
}
static void process_exec(struct process *p,char **args){
 uid_t u;gid_t g;const char *path=NULL;root_proof();image_ids(p,&u,&g);
 must(!chdir(p->cwd)&&!setgroups(0,NULL)&&!setresgid(g,g,g)&&!setresuid(u,u,u),"OCI context");
 must(args&&args[0]&&args[0][0],"empty OCI command");if(strchr(args[0],'/')){execve(args[0],args,p->env);bad("OCI execve");}
 for(unsigned i=0;i<p->envc;i++)if(!strncmp(p->env[i],"PATH=",5))path=p->env[i]+5;
 must(path!=NULL,"OCI PATH absent");while(1){const char *e=strchr(path,':');size_t n=e?(size_t)(e-path):strlen(path);char candidate[PATH_MAX];must(n+strlen(args[0])+2<sizeof candidate,"PATH candidate bound");if(n){memcpy(candidate,path,n);candidate[n]='/';strcpy(candidate+n+1,args[0]);}else strcpy(candidate,args[0]);execve(candidate,args,p->env);if(errno!=ENOENT&&errno!=ENOTDIR&&errno!=EACCES)bad("OCI PATH execve");if(!e)break;path=e+1;}bad("OCI executable unavailable");
}
static void verify_hostkey(void){
 int pipefd[2];pid_t p;size_t n;char *out,*line,*save;unsigned found=0;struct stat st;
 must(!lstat(PS "/ssh_host_ed25519_key",&st)&&S_ISREG(st.st_mode)&&st.st_uid==0&&st.st_nlink==1&&(st.st_mode&077)==0&&st.st_size>0&&st.st_size<4096,"bound private host key");
 must(!pipe2(pipefd,O_CLOEXEC),"hostkey output pipe");p=fork();must(p>=0,"hostkey fork");if(!p){char *a[]={"dropbearkey","-y","-f",PS "/ssh_host_ed25519_key",NULL};close(pipefd[0]);must(dup2(pipefd[1],1)>=0,"hostkey stdout");close(pipefd[1]);execve(DB,a,cleanenv);_exit(127);}close(pipefd[1]);out=readfd(pipefd[0],8192,&n);close(pipefd[0]);must(wait_child(p)==0,"dropbearkey verification");(void)n;
 for(line=strtok_r(out,"\n",&save);line;line=strtok_r(NULL,"\n",&save))if(!strncmp(line,"ssh-ed25519 ",12)){
  char *a=strchr(line+12,' '),*b=strchr(plan.hostkey+12,' ');size_t la=a?(size_t)(a-line):strlen(line),lb=b?(size_t)(b-plan.hostkey):strlen(plan.hostkey);must(la==lb&&!memcmp(line,plan.hostkey,la),"host public key pin");found++;
 }
 free(out);must(found==1,"host key output unambiguous");
}
static void private_directory(const char *path){struct stat st;must(!lstat(path,&st)&&S_ISDIR(st.st_mode)&&st.st_uid==0&&!(st.st_mode&022),"immutable private jail ancestor");}
static void private_suid_context(void){
 uid_t real,effective,saved;struct stat root,expected,outer,binary,running;struct statfs fs;struct statvfs flags;
 must(!getresuid(&real,&effective,&saved)&&effective==0&&saved==0&&(real==0||real==LIMA_UID)&&(real==0||getgid()==LIMA_GID),"private elevation principal");
 must(!stat("/",&root)&&!stat("/proc/1/root" JAIL,&expected)&&!stat("/proc/1/root" P,&outer)&&root.st_dev==expected.st_dev&&root.st_ino==expected.st_ino&&root.st_dev==outer.st_dev&&root.st_uid==0&&outer.st_uid==0&&(outer.st_mode&0777)==0700,"private elevation root binding");
 must(!statfs("/",&fs)&&(unsigned long)fs.f_type==TMPFS_MAGIC&&!statfs("/proc",&fs)&&(unsigned long)fs.f_type==PROC_SUPER_MAGIC,"private elevation filesystem binding");
 must(!statvfs("/",&flags)&&!(flags.f_flag&ST_NOSUID)&&(flags.f_flag&ST_NODEV)&&prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0)==0&&guest_ptrace_scope()>=1,"private elevation kernel policy");
 private_directory("/");private_directory("/platform");private_directory("/bin");private_directory("/usr");private_directory("/usr/bin");private_directory("/usr/local");private_directory("/usr/local/bin");private_directory("/home");private_directory(LIMA_HOME);private_directory(LIMA_HOME "/.ssh");
 must(!lstat("/platform/plm-vz",&binary)&&S_ISREG(binary.st_mode)&&binary.st_uid==0&&binary.st_nlink==1&&(binary.st_mode&07777)==04755&&!stat("/proc/self/exe",&running)&&running.st_dev==binary.st_dev&&running.st_ino==binary.st_ino,"private elevation executable binding");
 must(!lstat(LIMA_HOME "/.ssh/authorized_keys",&binary)&&S_ISREG(binary.st_mode)&&binary.st_uid==0&&binary.st_nlink==1&&(binary.st_mode&07777)==0644,"immutable bootstrap authorized key");
}
static void private_root_identity(void){must(!setgroups(0,NULL)&&!setresgid(0,0,0)&&!setresuid(0,0,0)&&getuid()==0&&geteuid()==0&&getgid()==0&&getegid()==0,"actual private sudo root identity");}
static void jail_setup(void){
 const char *dirs[]={"/platform","/bin","/usr","/usr/bin","/usr/local","/usr/local/bin","/etc","/root","/root/.ssh","/home",LIMA_HOME,LIMA_HOME "/.ssh","/proc","/sys","/dev","/run","/tmp","/mnt","/mnt/lima-cidata","/var","/var/run","/var/log",NULL};
 char p[PATH_MAX],b[2048];unsigned i;struct stat st;struct statvfs flags;
 int root=open(JAIL,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);must(root>=0&&!fstat(root,&st)&&st.st_uid==0&&!fchmod(root,0755)&&!close(root),"traversable private jail root");
 /* Only this private self-bind permits the one verified setuid helper. */
 mnt(JAIL,JAIL,NULL,MS_BIND,NULL);mnt(NULL,JAIL,NULL,MS_BIND|MS_REMOUNT|MS_NODEV,NULL);
 must(!statvfs(JAIL,&flags)&&!(flags.f_flag&ST_NOSUID)&&(flags.f_flag&ST_NODEV),"private jail setuid mount");
 must(!statvfs(P,&flags)&&(flags.f_flag&ST_NOSUID)&&(flags.f_flag&ST_NODEV)&&prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0)==0,"outer nosuid/no-new-privileges policy");
 for(i=0;dirs[i];i++){fmt(p,sizeof p,"%s%s",JAIL,dirs[i]);mkdir1(p,!strcmp(dirs[i],"/root")||!strcmp(dirs[i],"/root/.ssh")?0700:0755);}
 copyfile(SELF,JAIL "/platform/plm-vz",0755);copyfile(BB,JAIL "/platform/busybox",0755);copyfile(DB,JAIL "/platform/dropbearmulti",0755);copyfile(P "/bin/lima-guestagent",JAIL "/usr/local/bin/lima-guestagent",0755);
 {int helper=open(JAIL "/platform/plm-vz",O_RDONLY|O_NOFOLLOW|O_CLOEXEC);must(helper>=0&&!fstat(helper,&st)&&S_ISREG(st.st_mode)&&st.st_uid==0&&st.st_nlink==1&&(st.st_mode&07777)==0755&&!fchmod(helper,04755)&&!fstat(helper,&st)&&(st.st_mode&07777)==04755&&!close(helper),"verified private setuid helper");}
 {const char *apps[]={"sh","cat","uname","env","printf","test","true","false","grep","sed","awk","head","tail","readlink","id","mkdir","chmod","ls","sleep","date","touch","kill","ps","wc","cut","tr","sort","tee","rm","ln","mv","cp","stat","find","timeout",NULL};for(i=0;apps[i];i++){fmt(p,sizeof p,JAIL "/bin/%s",apps[i]);must(!symlink("/platform/busybox",p),"jail applet");}}
 textfile(JAIL "/bin/bash","#!/bin/sh\nexec /platform/busybox sh \"$@\"\n",0755);
 must(!symlink("/bin/env",JAIL "/usr/bin/env"),"jail env");must(!symlink("/platform/plm-vz",JAIL "/usr/bin/sudo"),"jail sudo");
 textfile(JAIL "/etc/passwd","root:x:0:0:Palimpsest control:/root:/bin/sh\n" LIMA_USER ":x:1000:1000:Palimpsest Lima control:" LIMA_HOME ":/bin/bash\n",0644);
 textfile(JAIL "/etc/group","root:x:0:\n" LIMA_USER ":x:1000:\n",0644);textfile(JAIL "/etc/shadow","root:!:0:0:99999:7:::\n" LIMA_USER ":!:0:0:99999:7:::\n",0600);textfile(JAIL "/etc/shells","/bin/sh\n/bin/bash\n",0644);
 fmt(b,sizeof b,"restrict,command=\"/platform/plm-vz session control\" %s\nno-port-forwarding,no-agent-forwarding,no-X11-forwarding,command=\"/platform/plm-vz session user\" %s\n",plan.controlkey,plan.userkey);textfile(JAIL "/root/.ssh/authorized_keys",b,0600);
 fmt(b,sizeof b,"restrict,command=\"/platform/plm-vz session lima\" %s\n",plan.controlkey);textfile(JAIL LIMA_HOME "/.ssh/authorized_keys",b,0644);
 bindmount("/proc",JAIL "/proc",0);bindmount("/sys",JAIL "/sys",1);mnt("/dev",JAIL "/dev",NULL,MS_BIND|MS_REC,NULL);
 {int run=open(RUN,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);must(run>=0&&!fchmod(run,0755)&&!close(run),"readable private readiness directory");}bindmount(RUN,JAIL "/run",0);
 textfile(JAIL "/platform/hostkey","",0600);bindmount(PS "/ssh_host_ed25519_key",JAIL "/platform/hostkey",1);
}
static void dhcp_callback(const char *event){
 const char *iface=getenv("interface"),*ip=getenv("ip"),*mask=getenv("subnet"),*router=getenv("router");char addr[128],gateway[64];unsigned char a[4];
 must(iface&&!strcmp(iface,"eth0"),"DHCP interface");
 if(!strcmp(event,"deconfig")){char *v[]={BB,"ip","-4","addr","flush","dev","eth0",NULL};tool(v);return;}
 must(!strcmp(event,"bound")||!strcmp(event,"renew"),"DHCP event");must(ip&&mask&&inet_pton(AF_INET,ip,a)==1&&inet_pton(AF_INET,mask,a)==1&&router&&strlen(router)<256,"DHCP IPv4 observation");
 {const char *end=strchr(router,' ');size_t n=end?(size_t)(end-router):strlen(router);must(n>0&&n<sizeof gateway,"DHCP gateway bound");memcpy(gateway,router,n);gateway[n]=0;must(inet_pton(AF_INET,gateway,a)==1,"DHCP gateway");}
 fmt(addr,sizeof addr,"%s/%s",ip,mask);
 {char *v[]={BB,"ip","-4","addr","replace",addr,"dev","eth0",NULL};tool(v);}
 {char *v[]={BB,"ip","link","set","dev","eth0","up",NULL};tool(v);}
 {char *v[]={BB,"ip","-4","route","replace","default","via",gateway,"dev","eth0",NULL};tool(v);}
 {int f=open(RUN "/dhcp.bound",O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC|O_NOFOLLOW,0600);must(f>=0,"DHCP witness");allwrite(f,addr,strlen(addr));must(!fsync(f)&&!close(f),"DHCP durable witness");}
}
static void network_setup(void){
 {char *v[]={BB,"ip","link","set","lo","up",NULL};tool(v);}
 {char *v[]={BB,"ip","link","set","eth0","up",NULL};tool(v);}
 textfile(RUN "/udhcpc-script","#!/.palimpsest/bin/busybox sh\nexec /.palimpsest/bin/plm-vz dhcp \"$1\"\n",0700);
 {char *v[]={BB,"udhcpc","-b","-n","-i","eth0","-t","10","-T","3","-s",RUN "/udhcpc-script",NULL};tool(v);}
 must(!access(RUN "/dhcp.bound",R_OK),"DHCP callback did not succeed");
}

#define CG "/sys/fs/cgroup"
#define APP CG "/application"
#define MAINCG APP "/main"
static void kernel_value(const char *path,const char *value){int f=open(path,O_WRONLY|O_NOFOLLOW|O_CLOEXEC);must(f>=0,"kernel control open");allwrite(f,value,strlen(value));must(!close(f),"kernel control close");}
static uint64_t kernel_counter(const char *path,const char *name){
 size_t n;unsigned found=0;uint64_t result=0;char *b=readfile(path,8192,&n),*save,*line;(void)n;
 for(line=strtok_r(b,"\n",&save);line;line=strtok_r(NULL,"\n",&save)){char key[64],extra;uint64_t value;must(sscanf(line,"%63s %" SCNu64 " %c",key,&value,&extra)==2,"kernel counter record");if(!strcmp(key,name)){result=value;found++;}}
 free(b);must(found==1,"kernel counter absent/ambiguous");return result;
}
static void application_setup(void){
 mkdir1(CG,0755);mnt("cgroup2",CG,"cgroup2",MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL);kernel_value(CG "/cgroup.subtree_control","+memory +pids\n");
 mkdir1(APP,0700);kernel_value(APP "/pids.max","2048\n");kernel_value(APP "/cgroup.subtree_control","+memory +pids\n");
 mkdir1(MAINCG,0700);kernel_value(MAINCG "/memory.oom.group","1\n");kernel_value(MAINCG "/pids.max","1024\n");mkdir1(APP "/sessions",0700);
}
static void application_enter(bool main){kernel_value(main?MAINCG "/cgroup.procs":APP "/sessions/cgroup.procs","0\n");}
static bool cgroup_tasks(const char *path,bool terminate){
 size_t n;char *b=readfile(path,65536,&n),*p=b,*end;bool any=false;(void)n;
 while(*p){while(isspace((unsigned char)*p))p++;if(!*p)break;errno=0;long id=strtol(p,&end,10);must(end!=p&&!errno&&id>1&&id<=INT_MAX,"application PID record");any=true;if(terminate){kill((pid_t)id,SIGTERM);kill((pid_t)id,SIGCONT);}p=end;}
 free(b);return any;
}
static bool main_descendants(bool terminate){return cgroup_tasks(MAINCG "/cgroup.procs",terminate);}
static void load_stop_fence(void);
static void main_stop_once(pid_t leader,int sig){
 if(leader>1&&kill(-leader,sig)&&errno==ESRCH)(void)kill(leader,sig);
 size_t n;char *b=readfile(MAINCG "/cgroup.procs",65536,&n),*p=b,*end;(void)n;
 while(*p){while(isspace((unsigned char)*p))p++;if(!*p)break;errno=0;long id=strtol(p,&end,10);must(end!=p&&!errno&&id>1&&id<=INT_MAX,"main stop PID");pid_t group=getpgid((pid_t)id);if(group>=0&&group!=leader)(void)kill((pid_t)id,sig);p=end;}
 free(b);
}
/* SSH-to-PID1 protocol is private, fixed-size and root-only. Public wire is JSON. */
enum request_op { OP_STATUS=1,OP_HANDOFF,OP_STOP,OP_SESSION_OPEN,OP_SESSION_CLOSE,OP_RESUME,OP_LOGS,OP_POWEROFF };
struct request { uint32_t version,operation; int32_t process_group; uint32_t reserved,stream,offset; };
struct session {int fd;pid_t peer,group;};
static struct session sessions[MAX_SESSIONS];
static bool sessions_known=true,handed_off=false,stopping=false,stop_failed=false,stop_fenced=false,stop_ready=false,stop_automatic=true,poweroff_requested=false,main_output_complete=true;
static int listener=-1,tcp_listener=-1,vsock_listener=-1;
static pid_t main_pid=0,agent_pid=0;static int main_exit=-1;static const char *main_state="not-started";
static uint64_t idle_since=0,stop_started=0,restart_at=0,main_started=0,poweroff_started=0;static unsigned restart_delay=1;static int main_signal=0;
static unsigned user_count(void){unsigned n=0;for(unsigned i=0;i<MAX_SESSIONS;i++)if(sessions[i].fd>=0)n++;return n;}
static int connect_control_path(const char *path){
 int f=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0);struct sockaddr_un a={0};struct timeval t={0};a.sun_family=AF_UNIX;t.tv_sec=5;must(strlen(path)<sizeof a.sun_path,"control socket path");strcpy(a.sun_path,path);
 must(f>=0&&!setsockopt(f,SOL_SOCKET,SO_RCVTIMEO,&t,sizeof t)&&!setsockopt(f,SOL_SOCKET,SO_SNDTIMEO,&t,sizeof t)&&!connect(f,(struct sockaddr*)&a,sizeof a),"private PID1 control connection");return f;
}
static int connect_control(void){return connect_control_path("/run/control.sock");}
static int request_control(unsigned op,pid_t group,char *out,size_t cap){
 int f=connect_control();struct request r={.version=1,.operation=op,.process_group=group,.reserved=0};ssize_t n;
 must(send(f,&r,sizeof r,MSG_NOSIGNAL)==(ssize_t)sizeof r,"control send");n=recv(f,out,cap-1,0);must(n>0&&(size_t)n<cap-1,"control response");out[n]=0;if(op!=OP_SESSION_OPEN)close(f);return f;
}
static void status_json(int fd){
 struct stat a,b;char out[65536];size_t n=0;int z;root_proof();must(!stat("/",&a)&&!stat("/proc/1/root/.",&b),"status root proof");
#define OUT(...) do{z=snprintf(out+n,sizeof out-n,__VA_ARGS__);must(z>=0&&(size_t)z<sizeof out-n,"status size");n+=(size_t)z;}while(0)
 OUT("{\"schema\":\"palimpsest.vz-root-status.v1\",\"platform_abi\":\"" PLM_PLATFORM_ABI "\",\"run_id\":\"%s\",\"plan_sha256\":\"%s\",\"phase\":\"%s\",\"root_filesystem\":\"overlay\",\"root_device\":%ju,\"root_inode\":%ju,\"pid1_root_device\":%ju,\"pid1_root_inode\":%ju,\"root_filesystem_uuid\":\"%s\",\"root_nonce\":\"%s\",\"lower_mounts\":[",plan.run_id,plan.hash,stop_failed?"stop-failed":poweroff_requested?"powering-off":stop_ready?"stop-ready":stopping?"stopping":handed_off?"ready":"awaiting-handoff",(uintmax_t)a.st_dev,(uintmax_t)a.st_ino,(uintmax_t)b.st_dev,(uintmax_t)b.st_ino,plan.fsuuid,plan.nonce);
 for(unsigned i=0;i<plan.nlayers;i++){struct stat st;struct statfs fs;struct statvfs v;char p[80];fmt(p,sizeof p,P "/lowers/%03u",i);must(!stat(p,&st)&&!statfs(p,&fs)&&(unsigned long)fs.f_type==SQUASHFS_MAGIC&&!statvfs(p,&v)&&(v.f_flag&ST_RDONLY),"status readonly SquashFS lower proof");OUT("%s{\"ordinal\":%u,\"digest\":\"%s\",\"mountpoint\":\"%s\",\"readonly\":true,\"device\":%ju,\"inode\":%ju}",i?",":"",i,plan.layers[i].digest,p,(uintmax_t)st.st_dev,(uintmax_t)st.st_ino);}
 OUT("],\"bootstrap_tcp_open\":%s,\"main\":{\"pid\":%d,\"state\":\"%s\",\"exit_code\":",tcp_listener>=0?"true":"false",(int)main_pid,main_state);if(main_exit<0)OUT("null");else OUT("%d",main_exit);OUT(",\"exit_signal\":%d,\"oom_kill_count\":%" PRIu64 ",\"output_complete\":%s},\"user_session_count\":%u,\"user_sessions_known\":%s,\"explicit_stop_fenced\":%s}\n",main_signal,kernel_counter(MAINCG "/memory.events","oom_kill"),main_output_complete?"true":"false",user_count(),sessions_known?"true":"false",stop_fenced?"true":"false");
#undef OUT
 /* A departing probe/follower may close its peer while this reply is built. */
 (void)send(fd,out,n,MSG_NOSIGNAL);
}
static int unix_listener(void){
 struct sockaddr_un a={0};int f=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC|SOCK_NONBLOCK,0);a.sun_family=AF_UNIX;strcpy(a.sun_path,RUN "/control.sock");
 must(f>=0&&!bind(f,(struct sockaddr*)&a,sizeof a)&&!chmod(a.sun_path,0600)&&!listen(f,32),"root control listener");return f;
}
static int tcp22(void){int f=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0),one=1;struct sockaddr_in a={0};a.sin_family=AF_INET;a.sin_port=htons(22);a.sin_addr.s_addr=htonl(INADDR_ANY);must(f>=0&&!setsockopt(f,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one)&&!bind(f,(struct sockaddr*)&a,sizeof a)&&!listen(f,16),"genuine bootstrap TCP22");return f;}
static int vsock22(void){int f=socket(AF_VSOCK,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);struct sockaddr_vm a={0};a.svm_family=AF_VSOCK;a.svm_port=22;a.svm_cid=VMADDR_CID_ANY;must(f>=0&&!bind(f,(struct sockaddr*)&a,sizeof a)&&!listen(f,16),"vsock22 listener");return f;}
static void close_other_fds(int keep){
 struct rlimit r;must(!getrlimit(RLIMIT_NOFILE,&r),"fd limit");rlim_t max=r.rlim_cur;if(max>65536)max=65536;for(int i=3;i<(int)max;i++)if(i!=keep)close(i);
}
static void dropbear_exec(int sock){
 must(!chdir(JAIL)&&!chroot(".")&&!chdir("/"),"SSH private control root");must(dup2(sock,0)>=0&&dup2(sock,1)>=0,"SSH connected AF_INET stdio");close_other_fds(-1);
 /* The qualified static build has no syslog or password/PAM authentication. */
 {char *a[]={"dropbear","-i","-j","-k","-m","-r","/platform/hostkey","-K","30","-T","3",NULL};execve("/platform/dropbearmulti",a,cleanenv);}bad("Dropbear exec");
}
static void connected_pair(int *a,int *b){
 int l=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC,0),c,s;struct sockaddr_in addr={0},client={0},peer={0};socklen_t n=sizeof addr,cn=sizeof client,pn=sizeof peer;addr.sin_family=AF_INET;addr.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
 must(l>=0&&!bind(l,(struct sockaddr*)&addr,sizeof addr)&&!listen(l,1)&&!getsockname(l,(struct sockaddr*)&addr,&n),"ephemeral TCP pair listener");c=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC,0);must(c>=0&&!connect(c,(struct sockaddr*)&addr,sizeof addr)&&!getsockname(c,(struct sockaddr*)&client,&cn),"ephemeral pair connect");s=accept4(l,(struct sockaddr*)&peer,&pn,SOCK_CLOEXEC);close(l);must(s>=0&&peer.sin_family==AF_INET&&peer.sin_addr.s_addr==htonl(INADDR_LOOPBACK)&&peer.sin_port==client.sin_port,"exact ephemeral pair accept");*a=c;*b=s;
}
struct relaybuf {unsigned char data[32768];size_t begin,end;bool eof,shut;};
static void relay_ssh(int remote){
 int pair,server;pid_t db;struct relaybuf ab={0},ba={0};
 connected_pair(&pair,&server);db=fork();must(db>=0,"Dropbear connection fork");if(!db){close(remote);close(pair);dropbear_exec(server);}
 close(server);must(!fcntl(remote,F_SETFL,O_NONBLOCK)&&!fcntl(pair,F_SETFL,O_NONBLOCK),"relay nonblocking");signal(SIGPIPE,SIG_IGN);
 for(;;){
  struct pollfd f[2]={{0}};int ret;f[0].fd=remote;f[1].fd=pair;
  if(ab.begin==ab.end)ab.begin=ab.end=0;
  if(ba.begin==ba.end)ba.begin=ba.end=0;
  if(ab.eof&&ab.begin==ab.end&&!ab.shut){shutdown(pair,SHUT_WR);ab.shut=true;}
  if(ba.eof&&ba.begin==ba.end&&!ba.shut){shutdown(remote,SHUT_WR);ba.shut=true;}
  if(ab.shut&&ba.shut)break;
  if(!ab.eof&&ab.end<sizeof ab.data)f[0].events|=POLLIN;
  if(ba.begin<ba.end)f[0].events|=POLLOUT;
  if(!ba.eof&&ba.end<sizeof ba.data)f[1].events|=POLLIN;
  if(ab.begin<ab.end)f[1].events|=POLLOUT;
  ret=poll(f,2,1000);if(ret<0&&errno==EINTR)continue;must(ret>=0,"SSH relay poll");
  for(unsigned i=0;i<2;i++){
   struct relaybuf *in=i?&ba:&ab,*out=i?&ab:&ba;int fd=i?pair:remote;
   if(f[i].revents&(POLLIN|POLLHUP)){if(!in->eof&&in->end<sizeof in->data){ssize_t n=read(fd,in->data+in->end,sizeof in->data-in->end);if(n>0)in->end+=(size_t)n;else if(!n)in->eof=true;else if(errno!=EAGAIN&&errno!=EWOULDBLOCK&&errno!=EINTR)in->eof=true;}}
   if((f[i].revents&POLLOUT)&&out->begin<out->end){ssize_t n=send(fd,out->data+out->begin,out->end-out->begin,MSG_NOSIGNAL);if(n>0)out->begin+=(size_t)n;else if(n<0&&errno!=EAGAIN&&errno!=EWOULDBLOCK&&errno!=EINTR){out->begin=out->end;out->eof=true;}}
   if(f[i].revents&(POLLERR|POLLNVAL)){ab.eof=ba.eof=true;ab.begin=ab.end;ba.begin=ba.end;}
  }
 }
 shutdown(remote,SHUT_RDWR);shutdown(pair,SHUT_RDWR);close(remote);close(pair);
 /* Do not turn a peer close into SIGKILL, even for a misbehaving SSH child. */
 {uint64_t until=mono()+QUIESCE_SECONDS;int s;while(waitpid(db,&s,WNOHANG)==0){if(mono()>=until){kill(db,SIGTERM);_exit(124);}usleep(100000);}}
 _exit(0);
}

static char lima_iid[96],agent_port[16];
static void lima_cidata(void){
 DIR *d=opendir("/sys/class/block");struct dirent *e;unsigned count=0;char candidate[PATH_MAX]="";
 must(d!=NULL,"cidata block enumeration");mkdir1(RUN "/cidata-probe",0700);
 while((e=readdir(d))){
  char dev[PATH_MAX];unsigned char sig[5];int f;struct stat st;if(e->d_name[0]=='.')continue;fmt(dev,sizeof dev,"/dev/%s",e->d_name);if(lstat(dev,&st)||!S_ISBLK(st.st_mode))continue;
  f=open(dev,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(f<0)continue;ssize_t n=pread(f,sig,5,32769);close(f);if(n!=5||memcmp(sig,"CD001",5))continue;
  if(syscall(SYS_mount,dev,RUN "/cidata-probe","iso9660",MS_RDONLY|MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL))continue;
  if(!access(RUN "/cidata-probe/lima.env",R_OK)&&!access(RUN "/cidata-probe/meta-data",R_OK)&&!access(RUN "/cidata-probe/param.env",R_OK)){count++;strcpy(candidate,dev);}
  must(!syscall(SYS_umount2,RUN "/cidata-probe",0),"unmount cidata probe");
 }
 closedir(d);must(count==1,"original Lima CIDATA ambiguous/absent");mnt(candidate,JAIL "/mnt/lima-cidata","iso9660",MS_RDONLY|MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL);
 {size_t n;char *env=readfile(JAIL "/mnt/lima-cidata/lima.env",65536,&n),*save,*line;unsigned iid=0,port=0,user=0,uid=0,home=0,shell=0,plain=0,mtype=0;
  must(!n||env[n-1]=='\n',"Lima env unterminated");must(!memchr(env,0,n),"Lima env NUL");
  for(line=strtok_r(env,"\n",&save);line;line=strtok_r(NULL,"\n",&save)){
   char *v=strchr(line,'=');must(v!=NULL,"Lima env field");*v++=0;must(envname(line)&&strlen(v)<4096,"Lima env bounds");
   if(!strcmp(line,"LIMA_CIDATA_IID")){must(++iid==1&&!strncmp(v,"iid-",4)&&strlen(v)<sizeof lima_iid,"Lima IID");for(char *p=v+4;*p;p++)must(isdigit((unsigned char)*p),"Lima IID numeric");strcpy(lima_iid,v);}
   if(!strcmp(line,"LIMA_CIDATA_VSOCK_PORT")){char *end;unsigned long x;errno=0;x=strtoul(v,&end,10);must(++port==1&&!errno&&*v&&!*end&&x>22&&x<=UINT32_MAX&&strlen(v)<sizeof agent_port,"Lima agent vsock port");strcpy(agent_port,v);}
   if(!strcmp(line,"LIMA_CIDATA_USER"))must(++user==1&&!strcmp(v,LIMA_USER),"Lima bootstrap user");
   if(!strcmp(line,"LIMA_CIDATA_UID"))must(++uid==1&&!strcmp(v,"1000"),"Lima bootstrap UID");
   if(!strcmp(line,"LIMA_CIDATA_HOME"))must(++home==1&&!strcmp(v,LIMA_HOME),"Lima bootstrap home");
   if(!strcmp(line,"LIMA_CIDATA_SHELL"))must(++shell==1&&!strcmp(v,"/bin/bash"),"Lima bootstrap shell");
   if(!strcmp(line,"LIMA_CIDATA_PLAIN"))must(++plain==1&&(!*v||!strcmp(v,"0")||!strcmp(v,"false")),"Lima plain forbidden");
   if(!strcmp(line,"LIMA_CIDATA_MOUNTTYPE"))must(++mtype==1&&!strcmp(v,"virtiofs"),"Lima mount transport");
   if(!strcmp(line,"LIMA_CIDATA_VIRTIO_PORT")||!strcmp(line,"LIMA_CIDATA_CONTAINERD_USER")||!strcmp(line,"LIMA_CIDATA_CONTAINERD_SYSTEM"))must(!*v||!strcmp(v,"0")||!strcmp(v,"false"),"unsupported Lima provisioning");
  }
  must(iid==1&&port==1&&user==1&&uid==1&&home==1&&shell==1&&mtype==1,"Lima required parameters");
  free(env);
 }
 {size_t n;char *meta=readfile(JAIL "/mnt/lima-cidata/meta-data",4096,&n),needle[128];fmt(needle,sizeof needle,"instance-id: %s\n",lima_iid);must(!strncmp(meta,needle,strlen(needle)),"CIDATA current IID mismatch");free(meta);(void)n;}
 /* Lima generates this current-boot epoch in the owned CIDATA on every start. */
 {char *end;errno=0;uint64_t epoch=strtoull(lima_iid+4,&end,10);must(!errno&&!*end&&strlen(lima_iid+4)==10&&epoch>=UINT64_C(1704067200)&&epoch<UINT64_C(4102444800),"current boot epoch bound");
  time_t current=time(NULL);must(current>=0&&(uint64_t)current<epoch+86400,"guest clock incompatible with current boot");
  if((uint64_t)current<epoch){struct timespec ts={.tv_sec=(time_t)(epoch+mono()),.tv_nsec=0};must(!clock_settime(CLOCK_REALTIME,&ts),"initialize current boot clock");}
  must(!plan.expiry||wall()<plan.expiry,"fixed runtime expired before access");
 }
 /* Execute only selected real requirements, never conventional disk provisioning. */
 {size_t n;char *params=readfile(JAIL "/mnt/lima-cidata/param.env",65536,&n);must(!memchr(params,0,n),"param.env NUL");free(params);}
}
static int agent_output=-1,agent_log=-1;static uint64_t agent_bytes;static bool agent_listening,agent_unhealthy;
static void drain_agent(void){
 static char line[8192];static size_t used;static bool discarding;char b[4096],wanted[192];
 fmt(wanted,sizeof wanted,"level=info msg=\"serving the guest agent on vsock port: %s (host CID only)\"",agent_port);
 for(unsigned turn=0;turn<16&&agent_output>=0;turn++){
  ssize_t n=read(agent_output,b,sizeof b);if(n<0&&(errno==EINTR||errno==EAGAIN||errno==EWOULDBLOCK))return;
  if(n<=0){close(agent_output);agent_output=-1;agent_unhealthy=true;return;}
  size_t keep=(size_t)n;if(agent_bytes>=1024*1024)keep=0;else if(keep>1024*1024-agent_bytes)keep=(size_t)(1024*1024-agent_bytes);
  if(keep&&agent_log>=0){size_t done=0;while(done<keep){ssize_t z=write(agent_log,b+done,keep-done);if(z<0&&errno==EINTR)continue;if(z<=0){close(agent_log);agent_log=-1;agent_unhealthy=true;break;}done+=(size_t)z;}agent_bytes+=done;}
  for(ssize_t i=0;i<n;i++){if(b[i]=='\n'){if(!discarding){line[used]=0;char *p=strstr(line,wanted);if(p&&p[strlen(wanted)]==0)agent_listening=true;}used=0;discarding=false;}else if(!discarding){if(used+1<sizeof line)line[used++]=b[i];else{used=0;discarding=true;}}}
 }
}
static void agent_start(void){
 int pipes[2];must(!pipe2(pipes,O_CLOEXEC),"guestagent startup pipe");agent_log=open(RUN "/guestagent.log",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);must(agent_log>=0,"guestagent bounded log");
 agent_pid=fork();must(agent_pid>=0,"guestagent fork");if(!agent_pid){
  char *a[]={"/usr/local/bin/lima-guestagent","daemon","--runtime-dir","/run/lima-guestagent","--socket-owner","0","--vsock-port",agent_port,NULL};
  close(pipes[0]);must(dup2(pipes[1],2)>=0,"guestagent stderr");{int f=open("/dev/null",O_RDWR|O_CLOEXEC);must(f>=0&&dup2(f,0)>=0&&dup2(f,1)>=0,"guestagent stdio");close(f);}
  must(setsid()>=0&&!chdir(JAIL)&&!chroot(".")&&!chdir("/"),"guestagent private jail");close_other_fds(-1);execve(a[0],a,cleanenv);_exit(127);
 }
 close(pipes[1]);agent_output=pipes[0];must(!fcntl(agent_output,F_SETFL,O_NONBLOCK),"guestagent output nonblocking");
 {uint64_t until=mono()+15;while(mono()<until){int s;must(waitpid(agent_pid,&s,WNOHANG)==0,"guestagent exited during startup");drain_agent();if(agent_listening)break;struct pollfd p={.fd=agent_output,.events=POLLIN};int r=poll(&p,1,100);must(r>=0||errno==EINTR,"guestagent startup poll");}must(agent_listening,"guestagent did not report successful guest vsock listen");}
}
static void lima_ready(void){
 char iid[128];int s;root_proof();must(tcp_listener>=0&&vsock_listener>=0&&agent_listening&&agent_pid>0&&waitpid(agent_pid,&s,WNOHANG)==0&&!access(RUN "/dhcp.bound",R_OK),"native readiness predicate");fmt(iid,sizeof iid,"%s\n",lima_iid);textfile(RUN "/lima-ssh-ready",iid,0644);textfile(RUN "/lima-boot-done",iid,0644);
}
static void enter_image(void){
 int root=open("/proc/1/root",O_DIRECTORY|O_RDONLY|O_CLOEXEC);struct stat a,b;must(root>=0&&!fstat(root,&a),"PID1 image root handle");
 must(!fchdir(root)&&!chroot(".")&&!chdir("/"),"user actual image root");close(root);must(!stat("/",&b)&&a.st_dev==b.st_dev&&a.st_ino==b.st_ino,"user image root transition");root_proof();
}
static char *decode64(const char *s,size_t *outn){
 static const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";size_t n=strlen(s),used=0;unsigned acc=0,bits=0;char *out;must(n>0&&n<=360000&&n%4!=1,"base64url bounds");out=malloc(n*3/4+1);must(out!=NULL,"base64url allocation");
 for(size_t i=0;i<n;i++){const char *p=strchr(alphabet,s[i]);must(p!=NULL,"base64url alphabet");unsigned x=(unsigned)(p-alphabet);acc=(acc<<6)|x;bits+=6;if(bits>=8){bits-=8;out[used++]=(char)(acc>>bits);acc&=(1U<<bits)-1;}}
 must(acc==0,"base64url noncanonical padding");out[used]=0;*outn=used;return out;
}
static volatile sig_atomic_t session_signal;
static void session_sig(int s){session_signal=s;}
static bool children_alive(void){int s;pid_t p;for(;;){p=waitpid(-1,&s,WNOHANG);if(p>0)continue;if(p==0)return true;if(errno==EINTR)continue;must(errno==ECHILD,"session child observation");return false;}}
static void signal_children(pid_t group,int sig){
 char path[128],*b,*p,*end;size_t n;fmt(path,sizeof path,"/proc/self/task/%d/children",(int)getpid());b=readfile(path,65536,&n);(void)n;p=b;
 if(group>1){kill(-group,sig);kill(-group,SIGCONT);}
 while(*p){while(*p==' ')p++;if(!*p)break;errno=0;long child=strtol(p,&end,10);must(end!=p&&!errno&&child>1&&child<=INT_MAX,"children PID");kill((pid_t)child,sig);kill((pid_t)child,SIGCONT);p=end;}
 free(b);
}
static bool output_disconnected(void){
 struct pollfd f[2]={{.fd=STDOUT_FILENO,.events=0},{.fd=STDERR_FILENO,.events=0}};
 int r=poll(f,2,0);must(r>=0||errno==EINTR,"SSH output-channel liveness");
 return r>0&&((f[0].revents|f[1].revents)&(POLLHUP|POLLERR|POLLNVAL));
}
static int supervise_session(pid_t child,int fd,uint64_t milliseconds){
 uint64_t deadline=milliseconds?mono_ms()+milliseconds:UINT64_MAX,quiesce=0;int result=125;bool leader=false,terminated=false,timed_out=false;
 for(;;){
  int s;pid_t p;while((p=waitpid(-1,&s,WNOHANG))>0)if(p==child){leader=true;result=WIFEXITED(s)?WEXITSTATUS(s):128+WTERMSIG(s);}
  if(leader&&!children_alive())break;
  uint64_t now=mono_ms();
  if(session_signal||output_disconnected()||now>=deadline||leader){if(!terminated){terminated=true;quiesce=now+QUIESCE_SECONDS*1000;signal_children(child,session_signal?session_signal:SIGTERM);if(now>=deadline&&!session_signal)timed_out=true;}else signal_children(child,SIGTERM);}
  if(terminated&&now>=quiesce){
   /* A non-quiescent session remains accounted, not falsely declared closed. */
   if(fd>=0){char unknown='U';(void)send(fd,&unknown,1,MSG_NOSIGNAL);}
   quiesce=UINT64_MAX;result=124;
  }
  uint64_t until=terminated?quiesce:deadline;uint64_t delay=until>now?until-now:1;if(delay>100)delay=100;struct timespec sleep_time={.tv_sec=0,.tv_nsec=(long)delay*1000000};(void)nanosleep(&sleep_time,NULL);
 }
 if(fd>=0){struct request r={.version=1,.operation=OP_SESSION_CLOSE,.process_group=child,.reserved=0};must(send(fd,&r,sizeof r,MSG_NOSIGNAL)==(ssize_t)sizeof r,"session close accounting");close(fd);}
 return timed_out?124:result;
}
static void load_session_plan(void){
 char tag[32],hash[65];char *b;size_t n;cmdline(tag,hash);b=readfile(P "/seed/plan.json",MAX_PLAN,&n);parse_plan(b,n,hash);distinct_seed_tag(tag);
}
static void image_request(const struct request *r,char out[65536]){
 int f=connect_control_path(RUN "/control.sock");must(send(f,r,sizeof *r,MSG_NOSIGNAL)==(ssize_t)sizeof *r,"follow supervisor request");
 ssize_t n=recv(f,out,65535,MSG_TRUNC);must(n>0&&n<65535,"follow response bound");out[n]=0;close(f);
}
static void follow_identity(struct json *j,const char *schema){
 must(!strcmp(string(j,field(j,0,"schema")),schema)&&!strcmp(string(j,field(j,0,"run_id")),plan.run_id)&&!strcmp(string(j,field(j,0,"plan_sha256")),plan.hash),"follow root authority");
}
static size_t log_decode(const char *s,unsigned char out[16384]){
 static const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";size_t n=strlen(s),used=0;must(n<=21848&&n%4==0,"follow log encoding bound");
 for(size_t i=0;i<n;i+=4){
  const char *a=strchr(alphabet,s[i]),*b=strchr(alphabet,s[i+1]);must(a&&b,"follow log alphabet");unsigned x=(unsigned)(a-alphabet)<<18|(unsigned)(b-alphabet)<<12;
  bool c=s[i+2]!='=',d=s[i+3]!='=';must(c||!d,"follow log padding");if(c){const char *p=strchr(alphabet,s[i+2]);must(p!=NULL,"follow log alphabet");x|=(unsigned)(p-alphabet)<<6;}if(d){const char *p=strchr(alphabet,s[i+3]);must(p!=NULL,"follow log alphabet");x|=(unsigned)(p-alphabet);}
  must((c&&d)||i+4==n,"follow log trailing padding");must(used+1+(unsigned)c+(unsigned)d<=16384,"follow log decoded bound");out[used++]=(unsigned char)(x>>16);if(c)out[used++]=(unsigned char)(x>>8);if(d)out[used++]=(unsigned char)x;must((d||!(x&255))&&(c||!(x&65535)),"follow log canonical padding");
 }
 return used;
}
static int follow_main(void){
 unsigned offsets[2]={0,0};bool complete_seen=false;char out[65536];unsigned char bytes[16384];root_proof();signal(SIGTERM,SIG_DFL);signal(SIGHUP,SIG_DFL);signal(SIGINT,SIG_DFL);
 for(;;){
  bool more=false;
  for(unsigned stream=0;stream<2;stream++){
   struct request r={.version=1,.operation=OP_LOGS,.stream=stream,.offset=offsets[stream]};image_request(&r,out);struct json j=js_parse(out,strlen(out));follow_identity(&j,"palimpsest.vz-root-logs.v1");
   must(!strcmp(string(&j,field(&j,0,"stream")),stream?"stderr":"stdout")&&integer(&j,field(&j,0,"offset"),1024*1024)==offsets[stream],"follow stream offset");
   unsigned next=(unsigned)integer(&j,field(&j,0,"next_offset"),1024*1024),size=(unsigned)integer(&j,field(&j,0,"size"),1024*1024);size_t n=log_decode(string(&j,field(&j,0,"data_base64")),bytes);
   must(next>=offsets[stream]&&next-offsets[stream]==n&&next<=size,"follow exact bytes");if(n)allwrite((int)stream+1,bytes,n);offsets[stream]=next;if(next<size)more=true;js_free(&j);
  }
  struct request r={.version=1,.operation=OP_STATUS};image_request(&r,out);struct json j=js_parse(out,strlen(out));follow_identity(&j,"palimpsest.vz-root-status.v1");int m=field(&j,0,"main");bool exited=!strcmp(string(&j,field(&j,m,"state")),"exited"),complete=j.t[field(&j,m,"output_complete")].kind=='t';
  bool stopped=!strcmp(string(&j,field(&j,0,"phase")),"stop-ready")||!strcmp(string(&j,field(&j,0,"phase")),"powering-off");
  if((!strcmp(plan.restart,"no")&&exited&&complete)||stopped){if(complete_seen&&!more){int code=stopped?143:(int)integer(&j,field(&j,m,"exit_code"),255);js_free(&j);return code;}complete_seen=true;js_free(&j);continue;}
  js_free(&j);if(!more)usleep(100000);
 }
}
static int session_user(const char *original){
 struct json execjson={0};char *decoded=NULL,*args[4097]={0};uint64_t milliseconds=0;bool structured=false,follow=original&&!strcmp(original,"plm-follow-v1");pid_t child;int gate[2],fd;char response[256];
 must(!prctl(PR_SET_CHILD_SUBREAPER,1,0,0,0),"session subreaper");signal(SIGTERM,session_sig);signal(SIGHUP,session_sig);signal(SIGINT,session_sig);
 enter_image();load_session_plan();
 if(original&&!strncmp(original,"plm-exec-v1 ",12)){
  size_t n;int a,k;unsigned count=0;decoded=decode64(original+12,&n);execjson=js_parse(decoded,n);KEYS(&execjson,0,"argv","timeout_ms");a=field(&execjson,0,"argv");must(execjson.t[a].kind=='['&&execjson.t[a].count>0&&execjson.t[a].count<=4096,"exec argv");size_t total=process_context_bytes(&plan.proc);for(k=execjson.t[a].child;k>=0&&k<execjson.t[a].next;k=execjson.t[k].next){args[count++]=string(&execjson,k);total+=strlen(args[count-1])+1;}args[count]=NULL;must(args[0][0]&&total<=256*1024,"exec process bytes");k=field(&execjson,0,"timeout_ms");milliseconds=integer(&execjson,k,600000);must(milliseconds>0,"exec positive effective timeout");structured=true;
 }
 if(follow)must(plan.proc.argc&&plan.proc.argv[0][0],"follow requires canonical main");
 sigset_t saved;must(!pipe2(gate,O_CLOEXEC),"session admission gate");block_payload_signals(&saved);child=fork();must(child>=0,"user session fork");
 if(!child){
  char go;bool tty=isatty(0);close(gate[1]);if(tty){signal(SIGTTOU,SIG_IGN);must(!setpgid(0,0),"user PTY process group");}else must(setsid()>=0,"user session process group");
  must(read(gate[0],&go,1)==1&&go=='G',"session admission");close(gate[0]);if(tty){must(!tcsetpgrp(0,getpgrp()),"user PTY foreground");signal(SIGTTOU,SIG_DFL);}close_other_fds(-1);
  application_enter(false);payload_signals(&saved,!follow);if(follow)_exit(follow_main());if(structured)process_exec(&plan.proc,args);
   {char *env[8]={"PATH=" DEFAULT_PATH,"HOME=/root","USER=root","LOGNAME=root",NULL,NULL,NULL,NULL};const char *term=getenv("TERM");char termbuf[256];if(term){must(strlen(term)<240,"terminal name bound");fmt(termbuf,sizeof termbuf,"TERM=%s",term);env[4]=termbuf;}must(!setgroups(0,NULL)&&!setresgid(0,0,0)&&!setresuid(0,0,0)&&!chdir("/"),"owner-root session context");if(original&&*original){char *a[]={"/bin/sh","-c",(char*)original,NULL};execve(a[0],a,env);}else{char *a[]={"/bin/sh","-i",NULL};execve(a[0],a,env);}bad("original-image admin shell unavailable");}
 }
 close(gate[0]);
 /* The helper has left its jail, so use the actual private socket location. */
 must(!sigprocmask(SIG_SETMASK,&saved,NULL),"restore session supervisor signals");
 {struct sockaddr_un a={0};struct request r={.version=1,.operation=OP_SESSION_OPEN,.process_group=child,.reserved=0};struct timeval t={0};ssize_t n;a.sun_family=AF_UNIX;t.tv_sec=10;fd=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0);strcpy(a.sun_path,RUN "/control.sock");must(fd>=0&&!setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&t,sizeof t)&&!setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&t,sizeof t)&&!connect(fd,(struct sockaddr*)&a,sizeof a),"user session authority");must(send(fd,&r,sizeof r,MSG_NOSIGNAL)==(ssize_t)sizeof r,"user session register");n=recv(fd,response,sizeof response-1,0);must(n>0&&(size_t)n<sizeof response,"user session admission response");response[n]=0;must(!strcmp(response,"{\"accepted\":true}\n"),"user session refused");}
 allwrite(gate[1],"G",1);close(gate[1]);return supervise_session(child,fd,milliseconds);
}

static int jail_script(const char *original,bool lima){
 must(!original||strlen(original)<=360032,"private command bound");
 must(!prctl(PR_SET_CHILD_SUBREAPER,1,0,0,0),"private command subreaper");signal(SIGTERM,session_sig);signal(SIGHUP,session_sig);signal(SIGINT,session_sig);
 pid_t p=fork();must(p>=0,"private command fork");
 if(!p){
  char *command[]={"/bin/sh","-c",(char*)original,NULL},*interactive[]={"/bin/bash","-i",NULL};child_signal_defaults();must(setsid()>=0,"private command process group");close_other_fds(-1);
  if(lima){
   uid_t r,e,s;gid_t gr,ge,gs;
   must(!prctl(PR_SET_KEEPCAPS,0,0,0,0)&&!setgroups(0,NULL)&&!setresgid(LIMA_GID,LIMA_GID,LIMA_GID)&&!setresuid(LIMA_UID,LIMA_UID,LIMA_UID)&&!getresuid(&r,&e,&s)&&!getresgid(&gr,&ge,&gs)&&r==LIMA_UID&&e==LIMA_UID&&s==LIMA_UID&&gr==LIMA_GID&&ge==LIMA_GID&&gs==LIMA_GID&&!chdir(LIMA_HOME),"actual nonroot Lima command");
  }
  char **a=original&&*original?command:interactive;execve(a[0],a,lima?limaenv:cleanenv);_exit(127);
 }
 return supervise_session(p,-1,0);
}
static int lima_session(const char *original){private_suid_context();private_root_identity();return jail_script(original,true);}
static bool trusted_jail_command(const char *path,char resolved[PATH_MAX]){
 must(canonical_path(path),"private sudo command path");
 if(!realpath(path,resolved)){must(errno==ENOENT||errno==ENOTDIR,"private sudo command resolution");return false;}
 must(!strncmp(resolved,"/platform/",10)||!strncmp(resolved,"/bin/",5)||!strncmp(resolved,"/usr/bin/",9)||!strncmp(resolved,"/usr/local/bin/",15),"private sudo command location");
 char walk[PATH_MAX];strcpy(walk,resolved);for(char *p=walk+1;*p;p++)if(*p=='/'){*p=0;private_directory(walk);*p='/';}
 struct stat st;must(!lstat(resolved,&st)&&S_ISREG(st.st_mode)&&st.st_uid==0&&st.st_nlink==1&&!(st.st_mode&022)&&(st.st_mode&0111),"immutable private sudo command");return true;
}
static _Noreturn void exec_private_root(char **args){
 char resolved[PATH_MAX],candidate[PATH_MAX];const char *dirs[]={"/platform","/bin","/usr/bin","/usr/local/bin",NULL};close_other_fds(-1);child_signal_defaults();
 if(strchr(args[0],'/')){must(args[0][0]=='/'&&trusted_jail_command(args[0],resolved),"private sudo executable absent");execve(resolved,args,cleanenv);bad("private sudo exec");}
 must(strlen(args[0])<=NAME_MAX,"private sudo command name");
 for(unsigned i=0;dirs[i];i++){fmt(candidate,sizeof candidate,"%s/%s",dirs[i],args[0]);if(trusted_jail_command(candidate,resolved)){execve(resolved,args,cleanenv);bad("private sudo exec");}}
 bad("private sudo executable absent");
}
static int control_session(const char *original){
 must(original&&strlen(original)<=360032,"control command bound");
 if(!strncmp(original,"plm-control-v1 ",15)){
  size_t n;char *decoded=decode64(original+15,&n),out[65536];struct json j=js_parse(decoded,n);char *op;unsigned request;
  op=string(&j,field(&j,0,"operation"));
  if(!strcmp(op,"logs")){
   KEYS(&j,0,"operation","stream","offset");char *stream=string(&j,field(&j,0,"stream"));must(!strcmp(stream,"stdout")||!strcmp(stream,"stderr"),"log stream");int f=connect_control();struct request r={.version=1,.operation=OP_LOGS,.stream=!strcmp(stream,"stderr"),.offset=(uint32_t)integer(&j,field(&j,0,"offset"),1024*1024)};
   must(send(f,&r,sizeof r,MSG_NOSIGNAL)==(ssize_t)sizeof r,"log request");ssize_t z=recv(f,out,sizeof out-1,MSG_TRUNC);must(z>0&&(size_t)z<sizeof out,"log response bound");out[z]=0;close(f);allwrite(1,out,(size_t)z);js_free(&j);free(decoded);return 0;
  }
  KEYS(&j,0,"operation");if(!strcmp(op,"status"))request=OP_STATUS;else if(!strcmp(op,"handoff"))request=OP_HANDOFF;else if(!strcmp(op,"stop"))request=OP_STOP;else if(!strcmp(op,"resume"))request=OP_RESUME;else if(!strcmp(op,"poweroff"))request=OP_POWEROFF;else bad("unsupported authenticated control operation");
  request_control(request,0,out,sizeof out);allwrite(1,out,strlen(out));js_free(&j);free(decoded);return 0;
 }
 return jail_script(original,false);
}
static int sudo_adapter(int argc,char **argv){
 int i=1;size_t bytes=0;private_suid_context();must(argc>1&&argc<=4096,"private sudo argument count");for(int k=0;k<argc;k++){bytes+=strlen(argv[k])+1;must(bytes<=256*1024,"private sudo argument bytes");}
 while(i<argc&&argv[i][0]=='-'&&argv[i][1]){
  if(!strcmp(argv[i],"--")){i++;break;}
  if(!strncmp(argv[i],"--preserve-env",14)&&(argv[i][14]==0||argv[i][14]=='=')){i++;continue;}
  /* Compatibility flags never authorize inherited caller environment. */
  if(argv[i][1]=='-')bad("unsupported control sudo form");
  /* Combined short flags as used by genuine Lima scripts (e.g. -iu). */
  for(unsigned k=1;argv[i][k];k++){char c=argv[i][k];must(c=='n'||c=='H'||c=='E'||c=='i'||c=='u',"unsupported control sudo form");if(c=='u'){must(argv[i][k+1]==0&&i+1<argc&&(!strcmp(argv[i+1],"root")||!strcmp(argv[i+1],"0")),"control sudo target");i++;break;}}
  i++;
 }
 must(i<argc&&argv[i][0],"control sudo requires command");private_root_identity();exec_private_root(argv+i);
}
struct broker {pid_t pid;};static struct broker brokers[32];
static unsigned broker_count(void){unsigned n=0;for(unsigned i=0;i<32;i++)if(brokers[i].pid)n++;return n;}
static void accept_ssh(int listenfd,bool vsock){
 int f;struct sockaddr_storage peer;socklen_t len=sizeof peer;
 f=accept4(listenfd,(struct sockaddr*)&peer,&len,SOCK_CLOEXEC);if(f<0){must(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR,"SSH accept");return;}
 if(vsock){struct sockaddr_vm *a=(struct sockaddr_vm*)&peer;if(peer.ss_family!=AF_VSOCK||a->svm_cid!=VMADDR_CID_HOST){close(f);return;}}
 if(poweroff_requested||broker_count()>=32){close(f);return;}
 pid_t p=fork();must(p>=0,"SSH broker fork");if(!p){
  child_signal_defaults();
  if(listener>=0)close(listener);
  if(tcp_listener>=0)close(tcp_listener);
  if(vsock_listener>=0)close(vsock_listener);
  for(unsigned i=0;i<MAX_SESSIONS;i++)if(sessions[i].fd>=0)close(sessions[i].fd);
  if(vsock)relay_ssh(f);else dropbear_exec(f);_exit(127);
 }
 close(f);for(unsigned i=0;i<32;i++)if(!brokers[i].pid){brokers[i].pid=p;break;}
}
static int main_output[2]={-1,-1},main_log[2]={-1,-1};static uint64_t output_bytes[2];static bool output_truncated[2];
static void output_open(void){
 const char *names[]={"main.stdout","main.stderr"};for(unsigned i=0;i<2;i++){char path[PATH_MAX];struct stat st;fmt(path,sizeof path,PS "/%s",names[i]);main_log[i]=open(path,O_RDWR|O_APPEND|O_CREAT|O_CLOEXEC|O_NOFOLLOW,0600);must(main_log[i]>=0&&!fstat(main_log[i],&st)&&S_ISREG(st.st_mode)&&st.st_uid==0&&st.st_nlink==1&&(st.st_mode&077)==0&&st.st_size>=0&&st.st_size<=1024*1024,"bounded persistent main log");output_bytes[i]=(uint64_t)st.st_size;fmt(path,sizeof path,PS "/%s.truncated",names[i]);if(!lstat(path,&st)){size_t n;char *b=readfile(path,16,&n);must(n==5&&!memcmp(b,"true\n",5),"persistent log truncation binding");free(b);output_truncated[i]=true;}else must(errno==ENOENT,"log truncation inspection");}
}
static void launch_main(void){
 int pipes[2][2];sigset_t saved;must(handed_off&&tcp_listener<0&&!stopping,"main launch before authenticated handoff");
 if(!plan.proc.argc||!plan.proc.argv[0][0]){main_state="absent";return;}
 main_output_complete=false;
 for(unsigned i=0;i<2;i++)must(!pipe2(pipes[i],O_CLOEXEC),"main output pipe");
 block_payload_signals(&saved);main_pid=fork();must(main_pid>=0,"main process fork");if(!main_pid){
  must(setsid()>=0,"main process group");application_enter(true);payload_signals(&saved,true);for(unsigned i=0;i<2;i++){close(pipes[i][0]);must(dup2(pipes[i][1],(int)i+1)>=0,"main output redirect");close(pipes[i][1]);}
  {int f=open("/dev/null",O_RDONLY|O_CLOEXEC);must(f>=0&&dup2(f,0)>=0,"main stdin");close(f);}close_other_fds(-1);process_exec(&plan.proc,plan.proc.argv);_exit(127);
 }
 must(!sigprocmask(SIG_SETMASK,&saved,NULL),"restore main supervisor signals");
 for(unsigned i=0;i<2;i++){close(pipes[i][1]);if(main_output[i]>=0)close(main_output[i]);main_output[i]=pipes[i][0];must(!fcntl(main_output[i],F_SETFL,O_NONBLOCK),"main output nonblocking");}
 main_state="running";main_exit=-1;main_signal=0;main_started=mono();
}
static void drain_output(unsigned i){
 char b[8192];for(unsigned turn=0;turn<8;turn++){ssize_t n=read(main_output[i],b,sizeof b);if(n<0&&(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR))break;if(n==0){close(main_output[i]);main_output[i]=-1;break;}must(n>0,"main output read");size_t keep=(size_t)n;if(output_bytes[i]>=1024*1024)keep=0;else if(keep>1024*1024-output_bytes[i])keep=(size_t)(1024*1024-output_bytes[i]);if(keep)allwrite(main_log[i],b,keep);output_bytes[i]+=keep;if(keep<(size_t)n&&!output_truncated[i]){output_truncated[i]=true;char p[128];fmt(p,sizeof p,PS "/main.%s.truncated",i?"stderr":"stdout");textfile(p,"true\n",0600);}}
}
static void logs_json(int fd,unsigned stream,unsigned offset){
 unsigned char raw[16384];char encoded[21849],out[23000];struct stat st;size_t used=0;const char *alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
 if(stream>1||offset>1024*1024||fstat(main_log[stream],&st)||offset>(uint64_t)st.st_size){const char *error="{\"accepted\":false,\"error\":\"invalid-log-offset\"}\n";(void)send(fd,error,strlen(error),MSG_NOSIGNAL);return;}
 ssize_t got;do{got=pread(main_log[stream],raw,sizeof raw,offset);}while(got<0&&errno==EINTR);must(got>=0,"persistent log read");
 for(size_t i=0;i<(size_t)got;i+=3){size_t left=(size_t)got-i;unsigned x=(unsigned)raw[i]<<16;if(left>1)x|=(unsigned)raw[i+1]<<8;if(left>2)x|=raw[i+2];encoded[used++]=alphabet[(x>>18)&63];encoded[used++]=alphabet[(x>>12)&63];encoded[used++]=left>1?alphabet[(x>>6)&63]:'=';encoded[used++]=left>2?alphabet[x&63]:'=';}encoded[used]=0;
 fmt(out,sizeof out,"{\"schema\":\"palimpsest.vz-root-logs.v1\",\"run_id\":\"%s\",\"plan_sha256\":\"%s\",\"stream\":\"%s\",\"offset\":%u,\"next_offset\":%u,\"size\":%ju,\"data_base64\":\"%s\",\"truncated\":%s}\n",plan.run_id,plan.hash,stream?"stderr":"stdout",offset,offset+(unsigned)got,(uintmax_t)st.st_size,encoded,output_truncated[stream]?"true":"false");(void)send(fd,out,strlen(out),MSG_NOSIGNAL);
}
static void load_stop_fence(void){
 struct stat st;if(lstat(PS "/stop-fence.json",&st)){must(errno==ENOENT,"stop fence inspection");stop_fenced=false;return;}
 must(S_ISREG(st.st_mode)&&st.st_uid==0&&st.st_nlink==1&&(st.st_mode&077)==0,"private stop fence");
 size_t n;char *b=readfile(PS "/stop-fence.json",8192,&n);struct json j=js_parse(b,n);KEYS(&j,0,"run_id","root_nonce","explicit_stop");
 must(!strcmp(string(&j,field(&j,0,"run_id")),plan.run_id)&&!strcmp(string(&j,field(&j,0,"root_nonce")),plan.nonce)&&j.t[field(&j,0,"explicit_stop")].kind=='t',"stop fence binding");js_free(&j);free(b);stop_fenced=true;
}
static void fence_stop(void){
 if(stopping)return;
 stopping=true;stop_fenced=true;restart_at=0;stop_started=mono();idle_since=0;
 if(tcp_listener>=0){close(tcp_listener);tcp_listener=-1;}
 /* Fence is durable before delivery of the original image StopSignal. */
 {char b[512];fmt(b,sizeof b,"{\"run_id\":\"%s\",\"root_nonce\":\"%s\",\"explicit_stop\":true}\n",plan.run_id,plan.nonce);textfile(PS "/stop-fence.next",b,0600);must(!rename(PS "/stop-fence.next",PS "/stop-fence.json"),"durable stop fence");syncdir(PS);}
 main_stop_once(main_pid,plan.proc.stop);
 for(unsigned i=0;i<MAX_SESSIONS;i++)if(sessions[i].fd>=0&&sessions[i].peer>1)kill(sessions[i].peer,SIGTERM);
}
static void reply(int f,const char *s){(void)send(f,s,strlen(s),MSG_NOSIGNAL);}
static bool is_child_of(pid_t child,pid_t parent){
 char path[128],*b,*right,*end;size_t n;fmt(path,sizeof path,"/proc/%d/stat",(int)child);int f=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(f<0)return false;b=readfd(f,4096,&n);close(f);right=strrchr(b,')');if(!right||right[1]!=' '||right[3]!=' '){free(b);return false;}errno=0;long p=strtol(right+4,&end,10);bool yes=!errno&&end!=right+4&&p==parent;free(b);(void)n;return yes;
}
static void accept_control(void){
 int f=accept4(listener,NULL,NULL,SOCK_CLOEXEC|SOCK_NONBLOCK);struct ucred peer;socklen_t len=sizeof peer;struct request r;ssize_t n;unsigned slot;
 if(f<0){must(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR,"control accept");return;}
 if(getsockopt(f,SOL_SOCKET,SO_PEERCRED,&peer,&len)||peer.uid!=0||peer.pid<=1){close(f);return;}
 /* Accepted peers get a bounded read; one stalled peer never stalls PID1. */
 {struct pollfd p={0};p.fd=f;p.events=POLLIN;if(poll(&p,1,50)<=0){close(f);return;}}
 n=recv(f,&r,sizeof r,MSG_TRUNC);if(n!=(ssize_t)sizeof r||r.version!=1||r.reserved||(r.operation!=OP_LOGS&&(r.stream||r.offset))){reply(f,"{\"accepted\":false,\"error\":\"invalid-request\"}\n");close(f);return;}
 if(plan.expiry&&wall()>=plan.expiry){stop_automatic=true;fence_stop();}
 if(r.operation==OP_LOGS){logs_json(f,r.stream,r.offset);close(f);return;}
 if(r.operation==OP_STATUS){status_json(f);close(f);return;}
 if(r.operation==OP_STOP){if(!stopping)stop_automatic=false;fence_stop();reply(f,"{\"accepted\":true,\"phase\":\"stopping\",\"stopped\":false}\n");close(f);return;}
 if(r.operation==OP_POWEROFF){
  if(!stopping||!stop_ready||stop_failed||poweroff_requested){reply(f,"{\"accepted\":false,\"error\":\"not-stop-ready\"}\n");close(f);return;}
  poweroff_requested=true;poweroff_started=mono();reply(f,"{\"accepted\":true,\"phase\":\"powering-off\",\"stopped\":false}\n");close(f);return;
 }
 if(r.operation==OP_RESUME){
  if(stopping||handed_off||tcp_listener<0){reply(f,"{\"accepted\":false,\"error\":\"not-new-boot\"}\n");close(f);return;}
  load_stop_fence();if(stop_fenced){must(!unlink(PS "/stop-fence.json"),"authenticated resume fence removal");syncdir(PS);stop_fenced=false;}
  reply(f,"{\"accepted\":true,\"phase\":\"awaiting-handoff\"}\n");close(f);return;
 }
 if(r.operation==OP_HANDOFF){
  if(stopping||stop_fenced){reply(f,"{\"accepted\":false,\"error\":\"stop-fenced\"}\n");close(f);return;}
  if(!handed_off){must(tcp_listener>=0,"missing bootstrap handoff");close(tcp_listener);tcp_listener=-1;fatal_safe_poweroff=false;handed_off=true;idle_since=mono();launch_main();}
  reply(f,"{\"accepted\":true,\"bootstrap_tcp_open\":false}\n");close(f);return;
 }
 if(r.operation==OP_SESSION_OPEN&&!stopping&&handed_off&&r.process_group>1&&is_child_of(r.process_group,peer.pid)){
  for(slot=0;slot<MAX_SESSIONS;slot++)if(sessions[slot].fd<0)break;
  if(slot<MAX_SESSIONS){sessions[slot]=(struct session){.fd=f,.peer=peer.pid,.group=r.process_group};idle_since=0;reply(f,"{\"accepted\":true}\n");return;}
 }
 reply(f,"{\"accepted\":false,\"error\":\"not-admitted\"}\n");close(f);
}
static void session_event(unsigned i){
 struct session *s=&sessions[i];struct request r;ssize_t n=recv(s->fd,&r,sizeof r,MSG_DONTWAIT|MSG_TRUNC);
 if(n<0&&(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR))return;
 if(n==(ssize_t)sizeof r&&r.version==1&&r.operation==OP_SESSION_CLOSE&&r.process_group==s->group&&!r.reserved&&!r.stream&&!r.offset){
  if(kill(-s->group,0)==0||errno!=ESRCH)sessions_known=false;
 }else sessions_known=false;
 close(s->fd);s->fd=-1;if(!user_count()&&sessions_known)idle_since=mono();
}
static void reap(void){
 int s;pid_t p;while((p=waitpid(-1,&s,WNOHANG))>0){
  if(p==main_pid){main_signal=WIFSIGNALED(s)?WTERMSIG(s):0;main_exit=WIFEXITED(s)?WEXITSTATUS(s):128+main_signal;main_state="exited";main_pid=0;if(mono()-main_started>=60)restart_delay=1;if(!stopping&&!strcmp(plan.restart,"unless-stopped")){restart_at=mono()+restart_delay;if(restart_delay<60)restart_delay=restart_delay>30?60:restart_delay*2;}}
  if(p==agent_pid){agent_pid=0;if(!stopping)fence_stop();}
  for(unsigned i=0;i<32;i++)if(brokers[i].pid==p)brokers[i].pid=0;
 }
}
static bool other_processes(bool signal_all){
 DIR *d=opendir("/proc");struct dirent *e;bool any=false;must(d!=NULL,"shutdown process enumeration");
 while((e=readdir(d))){char *end;long p;if(!isdigit((unsigned char)e->d_name[0]))continue;p=strtol(e->d_name,&end,10);if(*end||p<=1||p>INT_MAX)continue;
  /* Kernel threads have no userspace executable; only guest userspace quiesces. */
  char path[128];struct stat st;fmt(path,sizeof path,"/proc/%ld/exe",p);if(stat(path,&st))continue;any=true;if(signal_all)kill((pid_t)p,SIGTERM);
 }
 closedir(d);return any;
}
static void orderly_poweroff(void){
 if(stop_failed)return;
 if(!stop_ready){
  bool main_live=main_descendants(false),session_live=cgroup_tasks(APP "/sessions/cgroup.procs",false);
  if(mono()-stop_started>=2&&session_live)cgroup_tasks(APP "/sessions/cgroup.procs",true);
  if(main_pid>0||main_live||session_live||user_count()||!sessions_known||main_output[0]>=0||main_output[1]>=0){
   if(mono()-stop_started>=QUIESCE_SECONDS){stop_failed=true;dprintf(2,"plm-vz: orderly stop failed: application/session quiescence unknown; no force issued\n");}return;
  }
  for(unsigned i=0;i<2;i++)must(!fsync(main_log[i]),"sync final main output");
  syncdir(PS);stop_ready=true;
 }
 if(stop_automatic&&!poweroff_requested){poweroff_requested=true;poweroff_started=mono();}
 if(!poweroff_requested||mono()-poweroff_started<1)return;
 /* Only after the explicit poweroff acknowledgement has left the helper. */
 if(tcp_listener>=0){close(tcp_listener);tcp_listener=-1;}if(vsock_listener>=0){close(vsock_listener);vsock_listener=-1;}
 if(other_processes(true)){if(mono()-poweroff_started>=QUIESCE_SECONDS){stop_failed=true;dprintf(2,"plm-vz: orderly platform quiescence failed; no force issued\n");}return;}
 sync();must(!reboot(RB_POWER_OFF),"normal guest poweroff");
}
static volatile sig_atomic_t pid1_stop;
static void pid1_signal(int sig){if(sig!=SIGCHLD)pid1_stop=1;}
static _Noreturn void init_main(void){
 char tag[32],expected[65],*bytes;size_t n;
 must(getpid()==1&&geteuid()==0,"platform init must be root PID1");umask(077);signal(SIGPIPE,SIG_IGN);
 {struct utsname u;must(!uname(&u)&&!strcmp(u.machine,"aarch64"),"native ARM64 platform");}
 early_mounts();cmdline(tag,expected);mnt(tag,P "/seed","virtiofs",MS_RDONLY|MS_NOSUID|MS_NODEV,NULL);bytes=readfile(P "/seed/plan.json",MAX_PLAN,&n);parse_plan(bytes,n,expected);distinct_seed_tag(tag);
 upper_mount();lowers_mount();root_transition();root_proof();host_shares_mount();identities();verify_hostkey();application_setup();load_stop_fence();jail_setup();lima_cidata();network_setup();
 for(unsigned i=0;i<MAX_SESSIONS;i++)sessions[i].fd=-1;
 listener=unix_listener();tcp_listener=tcp22();vsock_listener=vsock22();output_open();agent_start();lima_ready();
 signal(SIGCHLD,pid1_signal);signal(SIGTERM,pid1_signal);signal(SIGINT,pid1_signal);signal(SIGHUP,pid1_signal);
 for(;;){
  struct pollfd f[3+MAX_SESSIONS+2];unsigned map[MAX_SESSIONS+2],nf=3,nmap=0;
  for(unsigned j=0;j<nf;j++){f[j].events=0;f[j].revents=0;}f[0].fd=listener;f[0].events=POLLIN;f[1].fd=tcp_listener;f[1].events=POLLIN;f[2].fd=vsock_listener;f[2].events=POLLIN;
  for(unsigned i=0;i<MAX_SESSIONS;i++)if(sessions[i].fd>=0){map[nmap++]=i;f[nf].fd=sessions[i].fd;f[nf].events=POLLIN;f[nf].revents=0;nf++;}
  for(unsigned i=0;i<2;i++)if(main_output[i]>=0){map[nmap++]=MAX_SESSIONS+i;f[nf].fd=main_output[i];f[nf].events=POLLIN;f[nf].revents=0;nf++;}
  int rc=poll(f,nf,250);must(rc>=0||errno==EINTR,"PID1 poll");
  if(rc>0){
   if(f[0].revents&POLLIN)accept_control();
   if(tcp_listener>=0&&tcp_listener==f[1].fd&&(f[1].revents&POLLIN))accept_ssh(tcp_listener,false);
   if(vsock_listener>=0&&vsock_listener==f[2].fd&&(f[2].revents&POLLIN))accept_ssh(vsock_listener,true);
   for(unsigned j=3;j<nf;j++)if(f[j].revents&(POLLIN|POLLHUP|POLLERR)){unsigned i=map[j-3];if(i<MAX_SESSIONS)session_event(i);else drain_output(i-MAX_SESSIONS);}
  }
  drain_agent();reap();if(pid1_stop||agent_unhealthy){stop_automatic=true;fence_stop();}
  if(plan.expiry&&wall()>=plan.expiry){stop_automatic=true;fence_stop();}
  if(!stopping&&handed_off&&!strcmp(plan.mode,"dev")&&sessions_known&&!user_count()&&idle_since&&mono()-idle_since>=plan.idle){stop_automatic=true;fence_stop();}
  if(!stopping&&main_pid==0&&main_started)main_descendants(true);
  if(!main_pid&&main_output[0]<0&&main_output[1]<0&&!main_descendants(false))main_output_complete=true;
  if(!stopping&&restart_at&&mono()>=restart_at&&!main_descendants(false)&&main_output[0]<0&&main_output[1]<0){restart_at=0;launch_main();}
  if(stopping)orderly_poweroff();
 }
}
int main(int argc,char **argv){
 const char *name=strrchr(argv[0],'/');name=name?name+1:argv[0];
 if(argc==2&&!strcmp(argv[1],"--version")){puts(PLM_PLATFORM_ABI);return 0;}
 if(!strcmp(name,"sudo"))return sudo_adapter(argc,argv);
 if(argc==3&&!strcmp(argv[1],"session")&&!strcmp(argv[2],"lima"))return lima_session(getenv("SSH_ORIGINAL_COMMAND"));
 must(getuid()==0&&geteuid()==0,"privileged platform invocation requires real root");
 if(argc==3&&!strcmp(argv[1],"dhcp")){dhcp_callback(argv[2]);return 0;}
 if(argc==2&&!strcmp(argv[1],"probe")){char out[65536];request_control(OP_STATUS,0,out,sizeof out);struct json j=js_parse(out,strlen(out));must(!strcmp(string(&j,field(&j,0,"schema")),"palimpsest.vz-root-status.v1")&&integer(&j,field(&j,0,"root_device"),UINT64_MAX)==integer(&j,field(&j,0,"pid1_root_device"),UINT64_MAX)&&integer(&j,field(&j,0,"root_inode"),UINT64_MAX)==integer(&j,field(&j,0,"pid1_root_inode"),UINT64_MAX),"authenticated native root probe");js_free(&j);allwrite(1,out,strlen(out));return 0;}
 if(argc==3&&!strcmp(argv[1],"session")){
  const char *original=getenv("SSH_ORIGINAL_COMMAND");must(getuid()==0&&geteuid()==0,"SSH helper root identity");
  if(!strcmp(argv[2],"control"))return control_session(original);
  if(!strcmp(argv[2],"user"))return session_user(original);
  bad("untrusted session class");
 }
 if(getpid()==1&&argc==1)init_main();
 bad("unsupported platform invocation");return 125;
}
