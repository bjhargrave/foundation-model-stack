import functools
from typing import Dict, Optional, Union, Tuple, List

import torch

_HANDLED_FUNCTIONS = {}


def _implements(torch_function):
    """Register a torch function override"""

    def decorator(func):
        functools.update_wrapper(func, torch_function)
        _HANDLED_FUNCTIONS[torch_function] = func
        return func

    return decorator


class ExpandableTensor(torch.Tensor):
    """
    This tensor behaves similarly to a java ArrayList along a specified
    dimension. It preallocates space along that dimension, and has an append
    operation that utilizes this space. This can be more efficient than
    performing many consecutive torch.cat operations.

    When preallocated space is exhasted, the internal length is doubled.
    All operations performed on this tensor use a view that truncates the
    un-utilized preallocated space.

    This class overrides and deviates from the contract of `torch.cat` such
    that in some cases the result of a `torch.cat( (expandable, other) )` will
    be an in-place modified `expandable`. This could cause bugs in cases where
    the original tensor is modified in-place.

    Args:
        tensor: the initial values to hold in this tensor.
        dim: the expandable dimension
        preallocate_length: the total amount of space to allocate along
            dimension `dim`
    """

    def __init__(self, tensor, dim=0, preallocate_length=None):
        super().__init__()
        self._dim = dim
        self._dim_length = tensor.shape[dim]
        self._underlying_tensor = tensor
        if preallocate_length is not None and preallocate_length > self._dim_length:
            sizes = list(tensor.size())
            sizes[dim] = preallocate_length
            self._underlying_tensor = torch.empty(
                size=sizes, dtype=tensor.dtype, device=tensor.device
            )
            self._tensor().copy_(tensor)

    def __new__(cls, tensor, dim=0, preallocate_length=None):
        return super().__new__(cls)

    def size(self, dim=None):
        # https://github.com/pytorch/pytorch/issues/111944
        if dim is None:
            return self._tensor().size()
        else:
            return self._tensor().size(dim=dim)

    def _append(self, tensor):
        """
        Returns a tensor equivalent to the result of
        `torch.cat( (self, tensor), dim=self._dim)`, possibly modifying `self`
        in-place to make use of preallocated space.
        """
        dim = self._dim
        expected = list(self._underlying_tensor.size())
        tensor_sizes = list(tensor.size())
        for i in range(len(expected)):
            if i != dim:
                assert expected[i] == tensor_sizes[i]
        if self.size()[dim] + tensor.size()[dim] <= self._underlying_tensor.size()[dim]:
            # copy into tail of _tensor
            view = self._underlying_tensor
            sizes = list(view.size())
            sizes[self._dim] = tensor.size()[dim]
            strides = self._underlying_tensor.stride()
            offset = self._dim_length * strides[self._dim]
            view = view.as_strided(size=sizes, stride=strides, storage_offset=offset)
            view.copy_(tensor)
            result = ExpandableTensor(self._underlying_tensor, dim=self._dim)
            result._dim_length = self._dim_length + tensor.shape[dim]
            return result
        else:
            # create new expandable tensor
            expanded = ExpandableTensor(
                self._tensor(), self._dim, self._underlying_tensor.shape[dim] * 2
            )
            return expanded._append(tensor)

    def _tensor(self):
        """
        Returns a view of the tensor excluding preallocated space
        """
        view = self._underlying_tensor
        sizes = list(view.size())
        sizes[self._dim] = self._dim_length
        view = view.as_strided(size=sizes, stride=view.stride())
        return view

    def __repr__(self):
        return self._tensor().__repr__()

    @_implements(torch.cat)
    def cat(tensors, dim=0, *, out=None):
        if (
                len(tensors)
                and type(tensors[0]) == ExpandableTensor
                and tensors[0]._dim == dim
        ):
            result = tensors[0]
            for tensor in tensors[1:]:
                result = result._append(tensor)
            return result
        else:
            tensors = [
                tensor._tensor() if type(tensor) == ExpandableTensor else tensor
                for tensor in tensors
            ]
            return torch.cat(tensors, dim, out=out)

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}
        if func not in _HANDLED_FUNCTIONS or not all(
                issubclass(t, (torch.Tensor, ExpandableTensor)) for t in types
        ):
            args = [a._tensor() if type(a) == ExpandableTensor else a for a in args]
            return func(*args, **kwargs)
        return _HANDLED_FUNCTIONS[func](*args, **kwargs)

    def __tensor_flatten__(self):
        ctx = {
            "dim": self._dim,
        }

        inner_tensors = ["_underlying_tensor"]
        return inner_tensors, ctx

    @staticmethod
    def __tensor_unflatten__(inner_tensors: Dict, meta, outer_size, outer_stride):
        underlying_tensor = inner_tensors["_underlying_tensor"]
        dim = meta["dim"]

        return ExpandableTensor(underlying_tensor, dim=dim)


class PagedTensor(torch.Tensor):

    def __init__(self, kv_heads: int, head_size: int, num_blocks: int, is_key: bool, block_size: int = 16,
                 device: Optional[Union[str, torch.device]] = "cuda", dtype: torch.dtype = torch.float32, *args,
                 **kwargs):
        super().__init__(*args, **kwargs)
        # TODO: is_key name change
        # TODO: flexible sizing

        element_size = torch.tensor([], dtype=dtype).element_size()
        x = block_size // element_size
        if is_key:
            block_shape = (
                kv_heads,
                head_size // x,
                block_size,
                x
            )
        else:
            block_shape = (
                kv_heads,
                head_size,
                block_size
            )
        self.kv_heads = kv_heads
        self.head_size = head_size
        self.num_blocks = num_blocks
        self.is_key = is_key
        self.block_size = block_size
        self._device = device
        self.blob = torch.empty(
            size=(num_blocks, *block_shape),
            dtype=dtype,
            device=device
        )

        self.ref_counts = torch.zeros(num_blocks, dtype=torch.int32, device=device)
        self.context_lengths = torch.empty(0, dtype=torch.int32, device=device)
        self.block_mapping = torch.empty(0, 0, dtype=torch.int32, device=device)

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}
        if func not in _HANDLED_FUNCTIONS or not all(
                issubclass(t, (torch.Tensor, PagedTensor)) for t in types
        ):
            return NotImplemented
        return _HANDLED_FUNCTIONS[func](*args, **kwargs)

    def _allocate_blocks(self, num_blocks) -> torch.Tensor:
        # TODO: keep num_blocks on gpu
        first_available_blocks = (self.ref_counts == 0).nonzero()[0:int(num_blocks.item())]
        self.ref_counts[first_available_blocks] = 1
        return first_available_blocks

    def __repr__(self):
        return f"PagedTensor(context_lengths={self.context_lengths}, block_mapping={self.block_mapping.tolist()})"

    @_implements(torch.cat)
    def cat(tensors, *, dim=0, out=None):

        # if we are calling with dim=0, a new sequence is being added to the batch
        if dim == 0:


            # if the second tensor is not a paged tensor, we are not doing a merge in which case we need to just add
            # sequences to the paged tensor
            if not isinstance(tensors[1], PagedTensor):
                l_tensors = tensors[1]

                # only allow empty for now
                assert l_tensors.size(1) == 0

                # TODO: will we even support tensor[0] being non paged_tensor, is that possible???
                paged_tensor: PagedTensor = tensors[0]

                # TODO: This needs to be right padded
                paged_tensor.block_mapping = torch.cat((paged_tensor.block_mapping, torch.empty(l_tensors.size(0), 0)), dim=0)

                paged_tensor.context_lengths = torch.cat((paged_tensor.context_lengths,torch.zeros(l_tensors.size(0))))
            else:
                pass
        # decode step
        elif dim == 1:
            l_tensors = tensors[1].unbind()  # TODO: assume nested

            # TODO: will we even support tensor[0] being non paged_tensor, is that possible???
            paged_tensor: PagedTensor = tensors[0]

            # for now it's a list
            # TODO: this is a list just to get things working
            context_lengths = torch.tensor([l.size(0) for l in l_tensors], dtype=torch.int32, device=paged_tensor._device)

            # batch should match
            assert len(l_tensors) == paged_tensor.block_mapping.size(0)

            prev_context_lengths = paged_tensor.context_lengths
            total_context_lengths = prev_context_lengths + context_lengths

            prev_num_blocks = prev_context_lengths // paged_tensor.block_size
            remainders_tensor = prev_num_blocks % paged_tensor.block_size
            prev_num_blocks += remainders_tensor.sign()

            total_num_blocks = total_context_lengths // paged_tensor.block_size
            remainders_tensor = total_context_lengths % paged_tensor.block_size
            total_num_blocks += remainders_tensor.sign()

            n_blocks_to_add = (total_num_blocks - prev_num_blocks).int()

            blocks_to_add = paged_tensor._allocate_blocks(torch.sum(n_blocks_to_add))
            print("bla")

            import torch.nn.functional as f

            # # INPUTS
            #
            # block_mapping = [
            #     [12, 13, 14, 15, 16],
            #     [1, 2, 3, 0, 0],
            #     [4, 5, 0, 0, 0],
            #     [6, 7, 8, 9, 0],
            #     [11, 0, 0, 0, 0],
            # ]
            # block_mapping = torch.tensor(block_mapping)
            # print("Block mapping:")
            # print(block_mapping)
            # print()
            #
            # n_blocks_to_add = torch.tensor([0, 2, 1, 0, 3])
            # print("N_blocks_to_add:")
            # print(n_blocks_to_add)
            # print()
            #
            # blocks_to_add = torch.arange(100, 100 + sum(n_blocks_to_add))
            # print("New block indices:")
            # print(blocks_to_add)
            # print()
            #
            # print("--------")
            # print()

            # CALCULATE ROW INDICES

            # At what new-block indices do we enter a new row?
            rowbreak_thresh = n_blocks_to_add.cumsum(0)
            # A pseudo-arange representing new block indices (stays on gpu)
            newblock_inds = torch.ones_like(blocks_to_add).cumsum(0).unsqueeze(0).sub(1)
            # What row break thresholds does each index position qualify for
            below_thresh = newblock_inds < rowbreak_thresh.unsqueeze(1)
            print("Matrix of row candidacy:")
            print(below_thresh)
            print()

            # The first qualifying break represents the assigned sequence
            row_inds = below_thresh.int().argmax(0)
            print("Row indices:")
            print(row_inds)
            print()

            # CALCULATE COLUMN INDICES

            col_offsets = paged_tensor.block_mapping.count_nonzero(1)
            print("Col offsets per row:")
            print(col_offsets)
            print()

            # Does each new value belong to the same row as the prior one?
            row_match = row_inds.roll(1) == row_inds
            # Count the total number of row agreements
            col_increments = row_match.int().cumsum(0)
            # Subtract the number of agreements from prior rows to get within-row increments
            col_increments -= f.pad(n_blocks_to_add.sub(1).clamp(min=0).cumsum(0), (1, 0))[:-1][row_inds]
            # Add the column offset for each row
            col_inds = col_increments + col_offsets[row_inds]
            print("Col inds:")
            print(col_inds)
            print()

            # UPDATE BLOCK MAPPING

            paged_tensor.block_mapping[row_inds, col_inds] = blocks_to_add
            print("Updated block mapping:")
            print(paged_tensor.block_mapping)

            # for i, context_length in enumerate(context_lengths):
            #     block_group = paged_tensor.block_mapping[i]
            #     prev_context_length = paged_tensor.context_lengths[i]
            #     total_context_length = prev_context_length + context_length
            #     total_num_blocks = total_context_length // paged_tensor.block_size
            #     if total_context_length % paged_tensor.block_size != 0:
            #         total_num_blocks += 1
            #
            #     blocks_to_add = total_num_blocks - block_group.size(0)
            #     for _ in range(blocks_to_add):
            #         block_group = torch.cat((block_group, paged_tensor._allocate_block()), dim=0)
            #     paged_tensor.context_lengths[i].add_(context_length)
            #     paged_tensor.block_mapping[i] = block_group

        else:
            raise ValueError("PagedTensor only supports dimensions 0 and 1 for dim parameter")

        return paged_tensor

if __name__ == "__main__":
    pt = PagedTensor(8, 64, 100, True, device="cpu")

    # warm up sequences
    pt = torch.cat((pt, torch.empty(8, 0)), dim=0)
    print(pt.block_mapping)
    print(pt.context_lengths)

    pt2 = torch.cat((pt, torch.rand(8, 8)), dim=1)


    #
    # position_ids = torch.tensor([
    #     [0 for _ in range(5)] + [i for i in range(15)],
    #     [0 for _ in range(8)] + [i for i in range(12)],
    #     [0 for _ in range(10)] + [i for i in range(10)],
    #     [i for i in range(20)],
    #     [0 for _ in range(5)] + [i for i in range(15)],
    #     [0 for _ in range(5)] + [i for i in range(15)],
    #     [0 for _ in range(5)] + [i for i in range(15)],
    #     [i for i in range(20)],
    # ], dtype=torch.long, device="cpu")
    # pt = torch.cat((pt, key), dim=1, position_ids=position_ids)
    # print(pt.block_mapping)
    # print(pt.context_lengths)
    # print(pt.ref_counts)
    #
    # # warmup
    # # warm up sequences
    # pt = torch.cat((pt, torch.empty(8, 0)), dim=0)
    #
    # # MHA
    #
    # # key_computed is a nested tensor that is jagged
    # # if not a nested tensor, then assume largest length
    # # calls paged attention store kernel with key_computed
    # key_cache = torch.cat((key_cache, key_computed), dim=1)