import threading
from contextlib import contextmanager

import logging
from functools import wraps, lru_cache
from typing import Dict, List, Optional, Collection

from parla import array
from parla.array import ArrayType
from . import device
from .device import *
from .environments import EnvironmentComponentInstance, TaskEnvironment, EnvironmentComponentDescriptor

import numpy

logger = logging.getLogger(__name__)

try:
    import cupy
    import cupy.cuda
except (ImportError, AttributeError):
    import inspect
    # Ignore the exception if the stack includes the doc generator
    if all("sphinx" not in f.filename for f in inspect.getouterframes(inspect.currentframe())):
        raise
    cupy = None

__all__ = ["gpu", "GPUComponent", "MultiGPUComponent"]


def _wrap_for_device(ctx: "_GPUDevice", f):
    @wraps(f)
    def ff(*args, **kwds):
        with ctx._device_context():
            return f(*args, **kwds)
    return ff


class _DeviceCUPy:
    def __init__(self, ctx):
        self._ctx = ctx

    def __getattr__(self, item):
        v = getattr(cupy, item)
        if callable(v):
            return _wrap_for_device(self._ctx, v)
        return v

class _GPUMemory(Memory):
    @property
    @lru_cache(None)
    def np(self):
        return _DeviceCUPy(self.device)

    def asarray_async(self, src):
        if isinstance(src, cupy.ndarray) and src.device.id == self.device.index:
            return src
        if not (src.flags['C_CONTIGUOUS'] or src.flags['F_CONTIGUOUS']):
            raise NotImplementedError('Only contiguous arrays are currently supported for gpu-gpu transfers.')
        dst = cupy.empty_like(src)
        dst.data.copy_from_device_async(src.data, src.nbytes)
        return dst

    def __call__(self, target):
        # TODO Several threads could share the same device object.
        #      It causes data race and CUDA context is incorrectly set.
        #      For now, this remove assumes that one device is always
        #      assigned to one task.
        # FIXME This code breaks the semantics since a different device
        #       could copy data on the current device to a remote device.
        #with self.device._device_context():
        with cupy.cuda.Device(self.device.index):
            if isinstance(target, numpy.ndarray):
                logger.debug("Moving data: CPU => %r", cupy.cuda.Device())
                return cupy.asarray(target)
            elif isinstance(target, cupy.ndarray) and \
                 cupy.cuda.Device() != getattr(target, "device", None):
                logger.debug("Moving data: %r => %r",
                             getattr(target, "device", None), cupy.cuda.Device())
                return self.asarray_async(target)
            else:
                return target


class _GPUDevice(Device):
    @property
    @lru_cache(None)
    def resources(self) -> Dict[str, float]:
        dev = cupy.cuda.Device(self.index)
        free, total = dev.mem_info
        attrs = dev.attributes
        return dict(threads=attrs["MultiProcessorCount"] * attrs["MaxThreadsPerMultiProcessor"], memory=total, vcus=1)

    @property
    def default_components(self) -> Collection["EnvironmentComponentDescriptor"]:
        return [GPUComponent()]

    @contextmanager
    def _device_context(self):
        with self.cupy_device:
            yield

    @property
    def cupy_device(self):
        return cupy.cuda.Device(self.index)

    @lru_cache(None)
    def memory(self, kind: MemoryKind = None):
        return _GPUMemory(self, kind)

    def __repr__(self):
        return "<CUDA {}>".format(self.index)


class _GPUArchitecture(Architecture):
    _devices: List[_GPUDevice]

    def __init__(self, name, id):
        super().__init__(name, id)
        devices = []
        if not cupy:
            self._devices = []
            return
        for device_id in range(2**16):
            cupy_device = cupy.cuda.Device(device_id)
            try:
                cupy_device.compute_capability
            except cupy.cuda.runtime.CUDARuntimeError:
                break
            assert cupy_device.id == device_id
            devices.append(self(cupy_device.id))
        self._devices = devices

    @property
    def devices(self):
        return self._devices

    def __call__(self, index, *args, **kwds):
        return _GPUDevice(self, index, *args, **kwds)


gpu = _GPUArchitecture("GPU", "gpu")
gpu.__doc__ = """The `~parla.device.Architecture` for CUDA GPUs.

>>> gpu(0)
"""

device._register_architecture("gpu", gpu)


class _CuPyArrayType(ArrayType):
    def can_assign_from(self, a, b):
        # TODO: We should be able to do direct copies from numpy to cupy arrays, but it doesn't seem to be working.
        # return isinstance(b, (cupy.ndarray, numpy.ndarray))
        return isinstance(b, cupy.ndarray)

    def get_memory(self, a):
        return gpu(a.device.id).memory()

    def get_array_module(self, a):
        return cupy.get_array_module(a)


if cupy:
    array._register_array_type(cupy.ndarray, _CuPyArrayType())

# Integration with parla.environments

class _GPUStacksLocal(threading.local):
    _stream_stack: List[cupy.cuda.Stream]
    _device_stack: List[cupy.cuda.Device]

    def __init__(self):
        super(_GPUStacksLocal, self).__init__()
        self._stream_stack = []
        self._device_stack = []

    def push_stream(self, stream):
        self._stream_stack.append(stream)

    def pop_stream(self) -> cupy.cuda.Stream:
        return self._stream_stack.pop()

    def push_device(self, dev):
        self._device_stack.append(dev)

    def pop_device(self) -> cupy.cuda.Device:
        return self._device_stack.pop()

    @property
    def stream(self):
        if self._stream_stack:
            return self._stream_stack[-1]
        else:
            return None
    @property
    def device(self):
        if self._device_stack:
            return self._device_stack[-1]
        else:
            return None


class GPUComponentInstance(EnvironmentComponentInstance):
    _stack: _GPUStacksLocal
    gpus: List[_GPUDevice]

    def __init__(self, descriptor: "GPUComponent", env: TaskEnvironment):
        super().__init__(descriptor)
        self.gpus = [d for d in env.placement if isinstance(d, _GPUDevice)]
        assert len(self.gpus) == 1
        self.gpu = self.gpus[0]
        # Use a stack per thread per GPU component just in case.
        self._stack = _GPUStacksLocal()

    def _make_stream(self):
        with self.gpu.cupy_device:
            return cupy.cuda.Stream(null=False, non_blocking=True)

    def __enter__(self):
        _gpu_locals._gpus = self.gpus
        dev = self.gpu.cupy_device
        dev.__enter__()
        self._stack.push_device(dev)
        stream = self._make_stream()
        stream.__enter__()
        self._stack.push_stream(stream)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        dev = self._stack.device
        stream = self._stack.stream
        try:
            stream.synchronize()
            stream.__exit__(exc_type, exc_val, exc_tb)
            _gpu_locals._gpus = None
            ret = dev.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self._stack.pop_stream()
            self._stack.pop_device()
        return ret

    def initialize_thread(self) -> None:
        for gpu in self.gpus:
            # Trigger cuBLAS/etc. initialization for this GPU in this thread.
            with cupy.cuda.Device(gpu.index) as device:
                a = cupy.asarray([2.])
                cupy.cuda.get_current_stream().synchronize()
                with cupy.cuda.Stream(False, True) as stream:
                    cupy.asnumpy(cupy.sqrt(a))
                    device.cublas_handle
                    device.cusolver_handle
                    device.cusolver_sp_handle
                    device.cusparse_handle
                    stream.synchronize()
                    device.synchronize()

class GPUComponent(EnvironmentComponentDescriptor):
    """A single GPU CUDA component which configures the environment to use the specific GPU using a single
    non-blocking stream

    """

    def combine(self, other):
        assert isinstance(other, GPUComponent)
        return self

    def __call__(self, env: TaskEnvironment) -> GPUComponentInstance:
        return GPUComponentInstance(self, env)


class _GPULocals(threading.local):
    _gpus: Optional[Collection[_GPUDevice]]

    def __init__(self):
        super(_GPULocals, self).__init__()
        self._gpus = None

    @property
    def gpus(self):
        if self._gpus:
            return self._gpus
        else:
            raise RuntimeError("No GPUs configured for this context")

_gpu_locals = _GPULocals()

def get_gpus() -> Collection[Device]:
    return _gpu_locals.gpus


class MultiGPUComponentInstance(EnvironmentComponentInstance):
    gpus: List[_GPUDevice]

    def __init__(self, descriptor: "MultiGPUComponent", env: TaskEnvironment):
        super().__init__(descriptor)
        self.gpus = [d for d in env.placement if isinstance(d, _GPUDevice)]
        assert self.gpus

    def __enter__(self):
        assert _gpu_locals._gpus is None
        _gpu_locals._gpus = self.gpus
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        assert _gpu_locals._gpus == self.gpus
        _gpu_locals._gpus = None
        return False

    def initialize_thread(self) -> None:
        for gpu in self.gpus:
            # Trigger cuBLAS/etc. initialization for this GPU in this thread.
            with cupy.cuda.Device(gpu.index) as device:
                a = cupy.asarray([2.])
                cupy.cuda.get_current_stream().synchronize()
                with cupy.cuda.Stream(False, True) as stream:
                    cupy.asnumpy(cupy.sqrt(a))
                    device.cublas_handle
                    device.cusolver_handle
                    device.cusolver_sp_handle
                    device.cusparse_handle
                    stream.synchronize()
                    device.synchronize()


class MultiGPUComponent(EnvironmentComponentDescriptor):
    """A multi-GPU CUDA component which exposes the GPUs to the task via `get_gpus`.

    The task code is responsible for selecting and using the GPUs and any associated streams.
    """

    def combine(self, other):
        assert isinstance(other, MultiGPUComponent)
        return self

    def __call__(self, env: TaskEnvironment) -> MultiGPUComponentInstance:
        return MultiGPUComponentInstance(self, env)
