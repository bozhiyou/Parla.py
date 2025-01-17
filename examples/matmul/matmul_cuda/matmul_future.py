import numpy as np
import cupy as cp
import time 

import sys
from parla import Parla, get_all_devices
from parla.array import copy, clone_here
from parla.cpu import cpu
from parla.cuda import gpu
from parla.function_decorators import specialized
from parla.ldevice import LDeviceSequenceBlocked
from parla.tasks import spawn, TaskSpace, CompletedTaskSpace

from mult.core import gemm, handle
import concurrent.futures 

ngpus = 4


# set up two n x n arrays to multiply together.
# n is chosen so that all three can be
# stored within the memory of a single GPU
# so that strong scaling numbers make sense.
n = 20000
np.random.seed(0)
a_cpu = np.random.rand(n, n).astype(np.float32)
b_cpu = np.random.rand(n, n).astype(np.float32)
# Partition the two arrays and set up the
# partitioned array where the result will be stored.
# This could also be done using a parla mapper object.
a_part = []
b_part = []
c_part = []
block_size = n // ngpus + 1
for i in range(ngpus):
    with cp.cuda.Device(i):
        a_part.append(cp.asarray(a_cpu[i * block_size : (i + 1) * block_size], order='F'))
        b_part.append(cp.asarray(b_cpu[:, i * block_size : (i + 1) * block_size], order='F'))
        c_part.append(cp.zeros((a_part[-1].shape[0], b_part[-1].shape[1]), dtype=np.float32, order='F'))
        cp.cuda.Device().synchronize()

print("NGPU", ngpus)

#gemm(a_part[0], b_part[0], c_part[0], 0)
#gemm(a_part[2], b_part[2], c_part[2], 2)
handle(0)
handle(2)
handle(3)
handle(1)
start = time.perf_counter()

def worker(inp):
    i, a_block, b_block, c_block = inp
    with cp.cuda.Device(i):
        #print(i, "Shape:", a_block.shape, b_block.shape)
        start_t = time.time()
        gemm(a_block, b_block, c_block, i)
        #print(i, "C:", c_block)
        #print(i, "Py: ", a_block @ b_block)
        end_t = time.time()
        print(i, " : ", end_t - start_t, flush=True)
        cp.cuda.Device().synchronize()
    return 

start = time.perf_counter()

with concurrent.futures.ThreadPoolExecutor() as executor:
    futures = [executor.submit(worker, (i, a_part[i], b_part[i], c_part[i])) for i in range(ngpus) ]
    results = [f.result() for f in futures]

end = time.perf_counter()

print('-----')

start = time.perf_counter()

with concurrent.futures.ThreadPoolExecutor() as executor:
    futures = [executor.submit(worker, (i, a_part[i], b_part[i], c_part[i])) for i in range(ngpus) ]
    results = [f.result() for f in futures]

end = time.perf_counter()


print("Total Time:", end - start, flush=True)
