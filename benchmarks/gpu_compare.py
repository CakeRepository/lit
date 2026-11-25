"""Time full GPU scans of the same mapped snapshots as the CPU benchmark."""

from statistics import median
from time import perf_counter


def benchmark_gpu(names, size, repeats, expected):
    start = perf_counter()
    import numpy as np
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA device and a CUDA build of PyTorch are required")
    # Signed views preserve uint32 bit equality. Copy-on-write mappings keep
    # torch from exposing writable references to the original input files.
    inputs = [
        torch.from_numpy(np.memmap(p, mode="c", dtype=np.int32, shape=(size,)))
        for p in names
    ]
    chunk = min(size, 8_000_000)
    left, right = [torch.empty(chunk, dtype=torch.int32, device="cuda") for _ in inputs]
    mask = torch.empty(chunk, dtype=torch.bool, device="cuda")
    count = torch.zeros((), dtype=torch.int64, device="cuda")
    torch.cuda.synchronize()
    setup = perf_counter() - start
    samples = []
    for trial in range(repeats + 1):
        start = perf_counter()
        count.zero_()
        for offset in range(0, size, chunk):
            length = min(chunk, size - offset)
            for buf, source in zip((left, right), inputs, strict=True):
                buf[:length].copy_(source[offset : offset + length], non_blocking=True)
            torch.ne(left[:length], right[:length], out=mask[:length])
            count.add_(torch.count_nonzero(mask[:length]))
        actual = count.item()  # Synchronizes transfers, kernels and result readback.
        elapsed = perf_counter() - start
        if actual != expected:
            raise AssertionError(f"GPU found {actual} mismatches, expected {expected}")
        if trial:
            samples.append(elapsed)
    return dict(
        device=torch.cuda.get_device_name(),
        setup_seconds=setup,
        mismatches=actual,
        transfer_inclusive_seconds=samples,
        median_seconds=median(samples),
    )
