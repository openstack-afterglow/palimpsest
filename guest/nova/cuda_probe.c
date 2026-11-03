#define _POSIX_C_SOURCE 200809L

#include <dlfcn.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

/* The CUDA Driver API ABI, without a CUDA SDK or a build-time driver dependency. */
typedef int CUresult;
typedef int CUdevice;
typedef unsigned long long CUdeviceptr;
typedef struct CUctx_st *CUcontext;
typedef struct CUmod_st *CUmodule;
typedef struct CUfunc_st *CUfunction;
typedef struct CUstream_st *CUstream;
typedef struct { unsigned char bytes[16]; } CUuuid;
typedef struct cublasContext *CublasHandle;

typedef struct {
    CUresult (*cuInit)(unsigned int);
    CUresult (*cuDeviceGetCount)(int *);
    CUresult (*cuDeviceGet)(CUdevice *, int);
    CUresult (*cuDeviceGetUuid_v2)(CUuuid *, CUdevice);
    CUresult (*cuDeviceGetName)(char *, int, CUdevice);
    CUresult (*cuCtxCreate_v2)(CUcontext *, unsigned int, CUdevice);
    CUresult (*cuCtxDestroy_v2)(CUcontext);
    CUresult (*cuMemAlloc_v2)(CUdeviceptr *, size_t);
    CUresult (*cuMemFree_v2)(CUdeviceptr);
    CUresult (*cuMemcpyHtoD_v2)(CUdeviceptr, const void *, size_t);
    CUresult (*cuMemcpyDtoH_v2)(void *, CUdeviceptr, size_t);
    CUresult (*cuModuleLoadData)(CUmodule *, const void *);
    CUresult (*cuModuleUnload)(CUmodule);
    CUresult (*cuModuleGetFunction)(CUfunction *, CUmodule, const char *);
    CUresult (*cuLaunchKernel)(CUfunction, unsigned int, unsigned int, unsigned int,
                              unsigned int, unsigned int, unsigned int,
                              unsigned int, CUstream, void **, void **);
    CUresult (*cuCtxSynchronize)(void);
} CudaApi;

typedef struct {
    int (*cudaRuntimeGetVersion)(int *);
    int (*cudaGetDeviceCount)(int *);
    int (*cublasCreate_v2)(CublasHandle *);
    int (*cublasSgemm_v2)(CublasHandle, int, int, int, int, int,
                          const float *, const float *, int, const float *, int,
                          const float *, float *, int);
    int (*cublasDestroy_v2)(CublasHandle);
} ImageCudaApi;

/* Exactly one block of nine threads; every product is computed on the GPU. */
static const char multiply_ptx[] =
    ".version 6.0\n"
    ".target sm_52\n"
    ".address_size 64\n"
    ".visible .entry multiply_u32(\n"
    "    .param .u64 input_a, .param .u64 input_b, .param .u64 output_c)\n"
    "{\n"
    "    .reg .b32 %r<5>;\n"
    "    .reg .b64 %rd<8>;\n"
    "    ld.param.u64 %rd1, [input_a];\n"
    "    ld.param.u64 %rd2, [input_b];\n"
    "    ld.param.u64 %rd3, [output_c];\n"
    "    mov.u32 %r1, %tid.x;\n"
    "    mul.wide.u32 %rd4, %r1, 4;\n"
    "    add.u64 %rd5, %rd1, %rd4;\n"
    "    add.u64 %rd6, %rd2, %rd4;\n"
    "    add.u64 %rd7, %rd3, %rd4;\n"
    "    ld.global.u32 %r2, [%rd5];\n"
    "    ld.global.u32 %r3, [%rd6];\n"
    "    mul.lo.u32 %r4, %r2, %r3;\n"
    "    st.global.u32 [%rd7], %r4;\n"
    "    ret;\n"
    "}\n";

static void timeout_handler(int signal_number)
{
    static const char message[] = "{\"success\":false,\"error\":\"CUDA probe timed out\"}\n";
    (void)signal_number;
    ssize_t written = write(STDOUT_FILENO, message, sizeof(message) - 1);
    (void)written;
    _exit(124);
}

/* Preserve valid UTF-8; reject malformed names instead of emitting invalid JSON. */
static int escape_name(const char *name, char *escaped)
{
    static const char hex[] = "0123456789abcdef";
    const unsigned char *input = (const unsigned char *)name;
    char *output = escaped;
    while (*input != 0) {
        unsigned char value = *input++;
        if (value == '"' || value == '\\') {
            *output++ = '\\';
            *output++ = (char)value;
        } else if (value < 0x20 || value == 0x7f) {
            *output++ = '\\';
            *output++ = 'u';
            *output++ = '0';
            *output++ = '0';
            *output++ = hex[value >> 4];
            *output++ = hex[value & 15];
        } else if (value >= 0x80) {
            const unsigned char *start = input - 1;
            uint32_t codepoint;
            unsigned int remaining;
            uint32_t minimum;
            if (value >= 0xc2 && value <= 0xdf) {
                codepoint = value & 0x1f;
                remaining = 1;
                minimum = 0x80;
            } else if (value >= 0xe0 && value <= 0xef) {
                codepoint = value & 0x0f;
                remaining = 2;
                minimum = 0x800;
            } else if (value >= 0xf0 && value <= 0xf4) {
                codepoint = value & 0x07;
                remaining = 3;
                minimum = 0x10000;
            } else {
                return 0;
            }
            for (unsigned int index = 0; index < remaining; ++index) {
                if ((*input & 0xc0) != 0x80) return 0;
                codepoint = (codepoint << 6) | (*input++ & 0x3f);
            }
            if (codepoint < minimum || codepoint > 0x10ffff ||
                (codepoint >= 0xd800 && codepoint <= 0xdfff)) return 0;
            while (start != input) *output++ = (char)*start++;
        } else {
            *output++ = (char)value;
        }
    }
    *output = '\0';
    return 1;
}

#define LOAD_FROM(library, table, name) do { \
    void *symbol = dlsym(library, #name); \
    _Static_assert(sizeof(table.name) == sizeof(symbol), "POSIX function pointer ABI required"); \
    if (symbol == NULL) goto cleanup; \
    memcpy(&table.name, &symbol, sizeof(table.name)); \
} while (0)

#define LOAD_SYMBOL(name) LOAD_FROM(driver, api, name)

#define CUDA_CALL(call) do { if ((call) != 0) goto cleanup; } while (0)

int main(void)
{
    CudaApi api = {0};
    ImageCudaApi image_api = {0};
    void *driver = NULL;
    void *runtime = NULL, *cublas_lt = NULL, *cublas = NULL;
    CublasHandle cublas_handle = NULL;
    CUcontext context = NULL;
    CUmodule module = NULL;
    CUfunction function = NULL;
    CUdevice device = 0;
    CUuuid uuid = {{0}};
    CUdeviceptr device_a = 0, device_b = 0, device_c = 0;
    uint32_t host_a[9], host_b[9], host_c[9] = {0};
    static const uint32_t expected[9] = {2, 6, 12, 20, 30, 42, 56, 72, 90};
    uint32_t checksum = 0;
    float gemm_a[9], gemm_b[9], gemm_c[9] = {0};
    static const float gemm_expected[9] = {90, 114, 138, 54, 69, 84, 18, 24, 30};
    float gemm_checksum = 0;
    const float alpha = 1, beta = 0;
    int runtime_version = 0, runtime_count = 0;
    char device_name[256] = {0};
    char escaped_name[6 * sizeof(device_name) + 1];
    char uuid_text[41];
    const char *error = "CUDA probe failed";
    int count = 0, success = 0;
    struct sigaction action;
    void *parameters[] = {&device_a, &device_b, &device_c};

    _Static_assert(sizeof(uint32_t) == 4, "32-bit GPU values required");
    _Static_assert(sizeof(CUdeviceptr) == 8, "64-bit CUDA pointers required");
    _Static_assert(sizeof(float) == sizeof(uint32_t), "reused GPU buffers require 32-bit floats");
    memset(&action, 0, sizeof(action));
    action.sa_handler = timeout_handler;
    if (sigemptyset(&action.sa_mask) != 0 || sigaction(SIGALRM, &action, NULL) != 0)
        goto cleanup;
    alarm(30);

    driver = dlopen("libcuda.so.1", RTLD_NOW | RTLD_LOCAL);
    if (driver == NULL) {
        error = "CUDA driver unavailable";
        goto cleanup;
    }
    runtime = dlopen("/usr/local/cuda/lib64/libcudart.so.12", RTLD_NOW | RTLD_LOCAL);
    if (runtime == NULL) {
        error = "CUDA image runtime unavailable";
        goto cleanup;
    }
    cublas_lt = dlopen("/usr/local/cuda/lib64/libcublasLt.so.12", RTLD_NOW | RTLD_LOCAL);
    if (cublas_lt == NULL) {
        error = "CUDA image cuBLASLt unavailable";
        goto cleanup;
    }
    cublas = dlopen("/usr/local/cuda/lib64/libcublas.so.12", RTLD_NOW | RTLD_LOCAL);
    if (cublas == NULL) {
        error = "CUDA image cuBLAS unavailable";
        goto cleanup;
    }
    LOAD_SYMBOL(cuInit);
    LOAD_SYMBOL(cuDeviceGetCount);
    LOAD_SYMBOL(cuDeviceGet);
    LOAD_SYMBOL(cuDeviceGetUuid_v2);
    LOAD_SYMBOL(cuDeviceGetName);
    LOAD_SYMBOL(cuCtxCreate_v2);
    LOAD_SYMBOL(cuCtxDestroy_v2);
    LOAD_SYMBOL(cuMemAlloc_v2);
    LOAD_SYMBOL(cuMemFree_v2);
    LOAD_SYMBOL(cuMemcpyHtoD_v2);
    LOAD_SYMBOL(cuMemcpyDtoH_v2);
    LOAD_SYMBOL(cuModuleLoadData);
    LOAD_SYMBOL(cuModuleUnload);
    LOAD_SYMBOL(cuModuleGetFunction);
    LOAD_SYMBOL(cuLaunchKernel);
    LOAD_SYMBOL(cuCtxSynchronize);
    LOAD_FROM(runtime, image_api, cudaRuntimeGetVersion);
    LOAD_FROM(runtime, image_api, cudaGetDeviceCount);
    LOAD_FROM(cublas, image_api, cublasCreate_v2);
    LOAD_FROM(cublas, image_api, cublasSgemm_v2);
    LOAD_FROM(cublas, image_api, cublasDestroy_v2);

    CUDA_CALL(api.cuInit(0));
    CUDA_CALL(api.cuDeviceGetCount(&count));
    if (count < 1) goto cleanup;
    CUDA_CALL(api.cuDeviceGet(&device, 0));
    CUDA_CALL(api.cuDeviceGetUuid_v2(&uuid, device));
    CUDA_CALL(api.cuDeviceGetName(device_name, (int)sizeof(device_name), device));
    if (device_name[0] == '\0' || memchr(device_name, '\0', sizeof(device_name)) == NULL)
        goto cleanup;
    CUDA_CALL(api.cuCtxCreate_v2(&context, 0, device));
    CUDA_CALL(image_api.cudaRuntimeGetVersion(&runtime_version));
    if (runtime_version != 12020) goto cleanup;
    CUDA_CALL(image_api.cudaGetDeviceCount(&runtime_count));
    if (runtime_count < 1) goto cleanup;
    CUDA_CALL(api.cuMemAlloc_v2(&device_a, sizeof(host_a)));
    CUDA_CALL(api.cuMemAlloc_v2(&device_b, sizeof(host_b)));
    CUDA_CALL(api.cuMemAlloc_v2(&device_c, sizeof(host_c)));
    for (size_t index = 0; index < 9; ++index) {
        host_a[index] = (uint32_t)index + 1;
        host_b[index] = (uint32_t)index + 2;
    }
    CUDA_CALL(api.cuMemcpyHtoD_v2(device_a, host_a, sizeof(host_a)));
    CUDA_CALL(api.cuMemcpyHtoD_v2(device_b, host_b, sizeof(host_b)));
    CUDA_CALL(api.cuModuleLoadData(&module, multiply_ptx));
    CUDA_CALL(api.cuModuleGetFunction(&function, module, "multiply_u32"));
    CUDA_CALL(api.cuLaunchKernel(function, 1, 1, 1, 9, 1, 1, 0, NULL, parameters, NULL));
    CUDA_CALL(api.cuCtxSynchronize());
    CUDA_CALL(api.cuMemcpyDtoH_v2(host_c, device_c, sizeof(host_c)));
    for (size_t index = 0; index < 9; ++index) {
        if (host_c[index] != expected[index]) goto cleanup;
        checksum += host_c[index];
    }
    if (checksum != 330) goto cleanup;

    /* The image's original cuBLAS operates on the same context and three buffers. */
    for (size_t index = 0; index < 9; ++index) {
        gemm_a[index] = (float)(index + 1);
        gemm_b[index] = (float)(9 - index);
    }
    CUDA_CALL(api.cuMemcpyHtoD_v2(device_a, gemm_a, sizeof(gemm_a)));
    CUDA_CALL(api.cuMemcpyHtoD_v2(device_b, gemm_b, sizeof(gemm_b)));
    CUDA_CALL(image_api.cublasCreate_v2(&cublas_handle));
    /* CUBLAS_OP_N == 0; all matrices are 3x3 column-major, leading dimension 3. */
    CUDA_CALL(image_api.cublasSgemm_v2(cublas_handle, 0, 0, 3, 3, 3,
                                      &alpha, (const float *)(uintptr_t)device_a, 3,
                                      (const float *)(uintptr_t)device_b, 3,
                                      &beta, (float *)(uintptr_t)device_c, 3));
    CUDA_CALL(api.cuCtxSynchronize());
    CUDA_CALL(api.cuMemcpyDtoH_v2(gemm_c, device_c, sizeof(gemm_c)));
    for (size_t index = 0; index < 9; ++index) {
        if (gemm_c[index] != gemm_expected[index]) goto cleanup;
        gemm_checksum += gemm_c[index];
    }
    if (gemm_checksum != 621) goto cleanup;
    if (!escape_name(device_name, escaped_name)) goto cleanup;
    success = 1;

cleanup:
    /* Keep the alarm armed through best-effort cleanup of partial acquisitions. */
    if (cublas_handle != NULL && image_api.cublasDestroy_v2 != NULL)
        (void)image_api.cublasDestroy_v2(cublas_handle);
    if (module != NULL && api.cuModuleUnload != NULL) (void)api.cuModuleUnload(module);
    if (device_c != 0 && api.cuMemFree_v2 != NULL) (void)api.cuMemFree_v2(device_c);
    if (device_b != 0 && api.cuMemFree_v2 != NULL) (void)api.cuMemFree_v2(device_b);
    if (device_a != 0 && api.cuMemFree_v2 != NULL) (void)api.cuMemFree_v2(device_a);
    if (context != NULL && api.cuCtxDestroy_v2 != NULL) (void)api.cuCtxDestroy_v2(context);
    if (cublas != NULL) (void)dlclose(cublas);
    if (cublas_lt != NULL) (void)dlclose(cublas_lt);
    if (runtime != NULL) (void)dlclose(runtime);
    if (driver != NULL) (void)dlclose(driver);
    if (!success) {
        if (printf("{\"success\":false,\"error\":\"%s\"}\n", error) < 0 || fflush(stdout) != 0)
            return 1;
        return 1;
    }

    (void)snprintf(uuid_text, sizeof(uuid_text),
                   "GPU-%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                   (unsigned int)uuid.bytes[0], (unsigned int)uuid.bytes[1],
                   (unsigned int)uuid.bytes[2], (unsigned int)uuid.bytes[3],
                   (unsigned int)uuid.bytes[4], (unsigned int)uuid.bytes[5],
                   (unsigned int)uuid.bytes[6], (unsigned int)uuid.bytes[7],
                   (unsigned int)uuid.bytes[8], (unsigned int)uuid.bytes[9],
                   (unsigned int)uuid.bytes[10], (unsigned int)uuid.bytes[11],
                   (unsigned int)uuid.bytes[12], (unsigned int)uuid.bytes[13],
                   (unsigned int)uuid.bytes[14], (unsigned int)uuid.bytes[15]);
    if (printf("{\"success\":true,\"device_index\":0,\"device_uuid\":\"%s\","
               "\"device_name\":\"%s\",\"results\":[%u,%u,%u,%u,%u,%u,%u,%u,%u],"
               "\"checksum\":%u,\"runtime_version\":%d,"
               "\"cublas_results\":[%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f],"
               "\"cublas_checksum\":%.0f}\n", uuid_text, escaped_name,
               (unsigned int)host_c[0], (unsigned int)host_c[1], (unsigned int)host_c[2],
               (unsigned int)host_c[3], (unsigned int)host_c[4], (unsigned int)host_c[5],
               (unsigned int)host_c[6], (unsigned int)host_c[7], (unsigned int)host_c[8],
               (unsigned int)checksum, runtime_version,
               (double)gemm_c[0], (double)gemm_c[1], (double)gemm_c[2],
               (double)gemm_c[3], (double)gemm_c[4], (double)gemm_c[5],
               (double)gemm_c[6], (double)gemm_c[7], (double)gemm_c[8],
               (double)gemm_checksum) < 0 || fflush(stdout) != 0)
        return 1;
    alarm(0);
    return 0;
}
