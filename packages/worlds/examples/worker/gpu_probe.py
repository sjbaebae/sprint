"""Verify a CUDA allocation, device memset and round trip without Python packages."""

import ctypes

cuda = ctypes.CDLL("libcuda.so.1")
context = ctypes.c_void_p()
device = ctypes.c_int()
address = ctypes.c_uint64()
size = ctypes.c_size_t(16)


def check(code):
    if code:
        raise RuntimeError(f"CUDA driver error {code}")


check(cuda.cuInit(0))
check(cuda.cuDeviceGet(ctypes.byref(device), 0))
check(cuda.cuCtxCreate_v2(ctypes.byref(context), 0, device))
try:
    check(cuda.cuMemAlloc_v2(ctypes.byref(address), size))
    try:
        check(cuda.cuMemsetD8_v2(address, 7, size))
        output = (ctypes.c_ubyte * 16)()
        check(cuda.cuMemcpyDtoH_v2(output, address, size))
        if bytes(output) != bytes([7] * 16):
            raise RuntimeError("CUDA device memory round trip failed")
        print("CUDA device memory round trip passed")
    finally:
        check(cuda.cuMemFree_v2(address))
finally:
    check(cuda.cuCtxDestroy_v2(context))
