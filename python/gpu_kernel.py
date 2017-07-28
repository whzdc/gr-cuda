#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 
# Copyright 2017 Deepwave Digital Inc.
# 
# This is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
# 
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this software; see the file COPYING.  If not, write to
# the Free Software Foundation, Inc., 51 Franklin Street,
# Boston, MA 02110-1301, USA.
# 

import numpy
from gnuradio import gr
import pycuda.compiler
import pycuda.driver

# Helper class to take strings of the format {X, Y, Z} and convert them to
# X, Y, and Z variables. This allows us to simply pass in a string from GRC
# instead of multiple ints for block and grid dimensions. Note that there is a
# special case of "Auto" for the input string, which signals to automatically
# scale these dimensions based on the size of the input data.
class gpu_dims():
  def __init__(self, dimensions):
    if ("auto" in (dimensions.lower())):
      self.auto = True
      self.x = 1
      self.y = 1
      self.z = 1
    else:
      self.auto = False
      # Remove the "{" and "}"
      xyz_str = dimensions[(dimensions.find("{") + 1) : (dimensions.find("}"))]
      # Next, split on the comma and space
      xyz_list = xyz_str.split(", ")
      # Finally, assign the member variables, and check for validity
      self.x = int(xyz_list[0])
      self.y = int(xyz_list[1])
      self.z = int(xyz_list[2])
      if ((self.x < 1) or (self.y < 1) or (self.z < 1)):
        raise ValueError("GPU Dimensions Must be Greater than Zero!")

  def dims(self):
    return (self.x, self.y, self.z)

class gpu_kernel(gr.sync_block):
  """Block that executes a CUDA kernel found within its Python source code. Built from a Sync Block since the kernel in this case has a 1:1 relationship between input and output.  All CUDA resources (i.e., device context, compiled kernel code, pointers to device memory, etc.) are managed within this block.

    Args:
      device_num: CUDA device number (0 if only one GPU installed). You can verify the device number by running the CUDA utility "deviceQuery".
      io_type: Data type to perform processing on. Since the kernel takes in floats, this value can either be "Float" for 32-bit floating point samples or "Complex", which are two 32-bit floating point samples back-to-back (one representing the real component and the other representing the imaginary component).
      vlen: Length of the input vector. Allows input and output data to be grouped into a MxN (i.e., M vectors of N samples each) array for easier processing. For CUDA applications, it is preferred that the vlen match the maximum number of threads per block for the GPU in order to simplify block and grid size computation.
      data_size: The number of bytes of data we expect for each call of the work() function. Used to initially allocate device memory. If more memory is required, the work() function will do a reallocation before proceeding.
      block_dims: CUDA block dimensions passed in as a string. Generally follows the convention "{X, Y, Z}", but "Auto" can also be passed in, which will set the block size based on the size of the input data.
      grid_dims: same a block_dims, except for grid dimensions.
  """
  def __init__(self, device_num, io_type, vlen, data_size, block_dims, grid_dims):
    gr.sync_block.__init__(self,
      name="gpu_kernel",
      in_sig=[(io_type, vlen)],
      out_sig=[(io_type, vlen)])
    # Initialize PyCUDA stuff...
    pycuda.driver.init()
    device = pycuda.driver.Device(device_num)
    self.context = device.make_context()
    # Build the kernel here.  Alternatively, we could have compiled the kernel
    # beforehand with nvcc, and simply passed in a path to the executable code.
    compiled_cuda = pycuda.compiler.compile("""
      // Simple kernel that takes every input and divides by two
      __global__ void divide_by_two(float* const in, float* const out) {
        // Find the part of the array we are processing, assuming a 3D grid of
        // 3D blocks, which is most certainly overkill for what we are doing.
        // That said, this represents a general case where both block dimensions
        // and grid dimensions are each specified in 3D.
        const int blockId =
          blockIdx.x + (blockIdx.y * gridDim.x)
          + (gridDim.x * gridDim.y * blockIdx.z);
        const int threadId =
          (blockId * (blockDim.x * blockDim.y * blockDim.z))
          + (threadIdx.z * (blockDim.x * blockDim.y))
          + (threadIdx.y * blockDim.x) + threadIdx.x;
        out[threadId] = in[threadId] / 2;
      }
    """)
    module = pycuda.driver.module_from_buffer(compiled_cuda)
    self.kernel = module.get_function("divide_by_two").prepare(["P", "P"])
    self.block_dims = gpu_dims(block_dims)
    self.grid_dims = gpu_dims(grid_dims)
    self.gpu_malloc(data_size)
    self.context.pop()

  def gpu_malloc(self, num_bytes):
    self.gpu_memory_size = num_bytes
    self.gpu_input = pycuda.driver.mem_alloc(self.gpu_memory_size)
    self.gpu_output = pycuda.driver.mem_alloc(self.gpu_memory_size)

  def gpu_realloc(self, num_bytes):
    del self.gpu_input
    del self.gpu_output
    self.gpu_malloc(num_bytes)

  def work(self, input_items, output_items):
    in0 = input_items[0]
    out = output_items[0]
    self.context.push()
    if (in0.nbytes > self.gpu_memory_size):
      print "Warning: Not Enough GPU Memory Allocated. Reallocating..."
      print "-> Received %d Vectors, Each with %d Samples." \
        % (in0.shape[0], in0.shape[1])
      print "-> Required Space: %d Bytes" % in0.nbytes
      print "-> Allocated Space: %d Bytes" % self.gpu_memory_size
      self.gpu_realloc(in0.nbytes)
    if (self.block_dims.auto):
      self.block_dims.x = in0.shape[1]
      # If we are doing complex data, we need to account for the fact that we
      # now have two floats for every single data point
      if (in0.dtype == numpy.complex64):
        self.block_dims.x = self.block_dims.x * 2
    if (self.grid_dims.auto):
      # Note, we do not use the block dimensions here since we can only launch
      # so many threads per block. As a result, it is "safer" to launch a bunch
      # of blocks...
      self.grid_dims.x = in0.shape[0]
    pycuda.driver.memcpy_htod(self.gpu_input, in0)
    self.kernel.prepared_call(
      self.grid_dims.dims(),
      self.block_dims.dims(),
      self.gpu_input,
      self.gpu_output)
    pycuda.driver.memcpy_dtoh(out, self.gpu_output)
    self.context.pop()
    return len(output_items[0])

