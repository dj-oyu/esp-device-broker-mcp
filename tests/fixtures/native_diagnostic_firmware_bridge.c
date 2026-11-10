/* Host-only shim: link the exact public firmware controller and D1 codec.
 * It supplies lifecycle callbacks, never implements controller/wire logic. */
#include "diagnostic_wire.h"
#include <string.h>
static diagnostic_session session;
static diagnostic_control control;
static diagnostic_wire wire;
static unsigned char output[80], during[80];
static size_t output_size,during_size;
static int failure,works,releases,owner;
static const uint64_t boot=UINT64_C(0xbbbbbbbbbbbbbbbb);
static bool emit(void *ctx,const uint8_t *p,size_t n){
    (void)ctx;if(n>sizeof(output))return false;
    memcpy(output,p,n);output_size=n;return true;
}
static void feed(const uint8_t *p,size_t n){
    if(n<2||p[0]!=0x1d||p[1]!='D')return;
    diagnostic_wire_begin(&wire);
    for(size_t i=2;i<n;i++)(void)diagnostic_wire_feed(&wire,p[i]);
}
static void *acquire(void *ctx){(void)ctx;return failure==1?NULL:&owner;}
static bool drain(void *ctx){(void)ctx;return failure!=2;}
static bool work(void *ctx,void *p){
    (void)ctx;(void)p;works++;
    if(during_size)feed(during,during_size);
    return failure!=3;
}
static bool restore(void *ctx){(void)ctx;return failure!=4;}
static void release(void *ctx,void *p){(void)ctx;(void)p;releases++;}
int bridge_boot(int enabled,uint64_t nonce){
    memset(&session,0,sizeof(session));
    atomic_init(&session.command.state,DIAG_CMD_IDLE);
    works=releases=failure=0;output_size=during_size=0;
    if(!diagnostic_control_init(&control,nonce,enabled!=0,&session))return 0;
    diagnostic_wire_init(&wire,&control,emit,NULL);return 1;
}
int bridge_init(int enabled){return bridge_boot(enabled,boot);}
size_t bridge_feed(const uint8_t *p,size_t n){output_size=0;feed(p,n);return output_size;}
const unsigned char *bridge_output(void){return output;}
void bridge_during(const uint8_t *p,size_t n){
    during_size=n<=sizeof(during)?n:0;
    if(during_size)memcpy(during,p,n);
}
int bridge_pump(int admitted,int fail){
    failure=fail;
    diagnostic_lifecycle_ops ops={NULL,acquire,drain,work,restore,release};
    return diagnostic_control_pump(&control,&ops,admitted!=0);
}
int bridge_work_count(void){return works;}
int bridge_release_count(void){return releases;}
int bridge_busy(void){return diagnostic_control_busy(&control);}
