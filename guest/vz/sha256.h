/* First-party SHA-256, FIPS 180-4. No external crypto ABI. */
#ifndef PLM_VZ_SHA256_H
#define PLM_VZ_SHA256_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>
struct sha256 { uint32_t h[8]; uint64_t bytes; unsigned used; unsigned char b[64]; };
static uint32_t ror32(uint32_t x,unsigned n){return (x>>n)|(x<<(32-n));}
static void sha_block(struct sha256 *s,const unsigned char *p){
 static const uint32_t k[64]={0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
 uint32_t w[64],a,b,c,d,e,f,g,h,t,u; unsigned i;
 for(i=0;i<16;i++)w[i]=((uint32_t)p[4*i]<<24)|((uint32_t)p[4*i+1]<<16)|((uint32_t)p[4*i+2]<<8)|p[4*i+3];
 for(i=16;i<64;i++){uint32_t x=w[i-15],y=w[i-2];w[i]=w[i-16]+(ror32(x,7)^ror32(x,18)^(x>>3))+w[i-7]+(ror32(y,17)^ror32(y,19)^(y>>10));}
 a=s->h[0];b=s->h[1];c=s->h[2];d=s->h[3];e=s->h[4];f=s->h[5];g=s->h[6];h=s->h[7];
 for(i=0;i<64;i++){t=h+(ror32(e,6)^ror32(e,11)^ror32(e,25))+((e&f)^(~e&g))+k[i]+w[i];u=(ror32(a,2)^ror32(a,13)^ror32(a,22))+((a&b)^(a&c)^(b&c));h=g;g=f;f=e;e=d+t;d=c;c=b;b=a;a=t+u;}
 s->h[0]+=a;s->h[1]+=b;s->h[2]+=c;s->h[3]+=d;s->h[4]+=e;s->h[5]+=f;s->h[6]+=g;s->h[7]+=h;
}
static void sha_init(struct sha256 *s){static const uint32_t h[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};memcpy(s->h,h,sizeof h);s->bytes=0;s->used=0;}
static void sha_add(struct sha256 *s,const void *v,size_t n){const unsigned char *p=v;s->bytes+=n;while(n){size_t z=64-s->used;if(z>n)z=n;memcpy(s->b+s->used,p,z);s->used+=(unsigned)z;p+=z;n-=z;if(s->used==64){sha_block(s,s->b);s->used=0;}}}
static void sha_hex(struct sha256 *s,char out[65]){uint64_t bits=s->bytes*8;unsigned char pad[128]={0x80};size_t z=s->used<56?56-s->used:120-s->used;unsigned i;sha_add(s,pad,z);for(i=0;i<8;i++)pad[i]=(unsigned char)(bits>>(56-8*i));sha_add(s,pad,8);for(i=0;i<32;i++){unsigned x=(s->h[i/4]>>(24-8*(i%4)))&255;out[2*i]="0123456789abcdef"[x>>4];out[2*i+1]="0123456789abcdef"[x&15];}out[64]=0;}
#endif
