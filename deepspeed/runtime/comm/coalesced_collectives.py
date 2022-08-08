"""batched collective operations for overhead amortization and better
bandwidth utilization"""

import math
from typing import List

import torch
from torch import Tensor
import torch.distributed
from torch.distributed import ProcessGroup
import torch.nn.functional

from deepspeed.utils import instrument_w_nvtx
from deepspeed.utils.logging import logger

if hasattr(torch.distributed, "_reduce_scatter_base"):
    def torch_reduce_scatter_fn(input_tensor: Tensor, output_tensor: Tensor, group):
        instrument_w_nvtx(torch.distributed._reduce_scatter_base)(
            output_tensor,
            input_tensor,
            group=group,
        )
else:
    logger.warning(
        "unable to find torch.distributed._reduce_scatter_base. will fall back to "
        "torch.distributed.reduce_scatter which will result in suboptimal performance. "
        "please consider upgrading your pytorch installation.")
    def torch_reduce_scatter_fn(input_tensor: Tensor, output_tensor: Tensor, group):
        input_tensor_lst = list(
            torch.chunk(input_tensor,
                        torch.distributed.get_world_size(group)))
        # ASCEND AVOID
        new_input_tensor_lst = [x.clone() for x in input_tensor_lst]
        new_output_tensor = output_tensor.clone()
        instrument_w_nvtx(torch.distributed.reduce_scatter)(
            new_output_tensor,
            new_input_tensor_lst,
            group=group,
        )
        output_tensor.copy_(new_output_tensor)


@instrument_w_nvtx
@torch.no_grad()
def reduce_scatter_coalesced(
    tensors: List[Tensor],
    group: ProcessGroup = None,
) -> List[Tensor]:
    """simultaneously reduce-scatter a list of tensors - this can be done more
    efficiently than individual reduce scatter calls

    TODO. see if PyTorch team wants a c++ version of this for ProcessGroupNCCL
    """
    this_rank = torch.distributed.get_rank(group)
    world_sz = torch.distributed.get_world_size(group)

    partition_lst_for_each_tensor = [None] * len(tensors)
    for tensor_idx, tensor in enumerate(tensors):
        flattened_tensor = tensor.view(-1)
        chunk_sz = math.ceil(tensor.numel() / world_sz)
        partition_lst_for_each_tensor[tensor_idx] = [
            flattened_tensor[rank * chunk_sz:rank * chunk_sz + chunk_sz]
            for rank in range(0,
                              world_sz)
        ]

    padded_partition_sz_for_each_tensor = tuple(
        math.ceil(t.numel() / world_sz) for t in tensors)

    if len(tensors) == 1 and tensors[0].numel() % world_sz == 0:
        # if there's only one tensor being reduced and we don't need to pad
        # we have an opportunity to avoid a memory allocation
        tensor_partition_flat_buffer = tensors[0].view(-1)
    else:
        # interleave tensor partitions such that the correct reduced partitions of each tensor
        # end up at each rank
        tensor_partitions_lst_with_padding = []
        for rank in range(world_sz):
            for tensor_idx in range(len(tensors)):
                # add tensor content
                tensor_chunk = partition_lst_for_each_tensor[tensor_idx][rank]
                tensor_partitions_lst_with_padding.append(tensor_chunk)

                # add padding if necessary
                padding_sz = padded_partition_sz_for_each_tensor[
                    tensor_idx] - tensor_chunk.numel()
                if padding_sz > 0:
                    tensor_partitions_lst_with_padding.append(
                        torch.empty(padding_sz,
                                    dtype=tensor_chunk.dtype,
                                    device=tensor_chunk.device))

        tensor_partition_flat_buffer = instrument_w_nvtx(
            torch.cat)(tensor_partitions_lst_with_padding)

    tensor_partition_flat_buffer.div_(world_sz)  # pre-divide
    tensor_partition_buffer_for_each_rank: List[Tensor] = torch.chunk(
        tensor_partition_flat_buffer,
        world_sz)

    # batched reduce-scatter call
    torch_reduce_scatter_fn(tensor_partition_flat_buffer,
                            tensor_partition_buffer_for_each_rank[this_rank],
                            group)

    # reverse procedure of the interleaving done previously, done on the
    # result of the batched reduce-scatter
    output_lst: List[Tensor] = [None] * len(tensors)
    offset = 0
    for tensor_idx in range(len(tensors)):
        output_lst[tensor_idx] = tensor_partition_buffer_for_each_rank[this_rank].narrow(
            0,
            offset,
            partition_lst_for_each_tensor[tensor_idx][this_rank].numel())

        offset += padded_partition_sz_for_each_tensor[tensor_idx]

    return output_lst
