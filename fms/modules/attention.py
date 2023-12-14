import math
from typing import Optional, Tuple, List

import torch
from torch import nn
from torch.distributed.distributed_c10d import ProcessGroup
from torch.nn import functional as F
from fms import distributed

from fms.distributed.tensorparallel import (
    apply_colwise_tp,
    apply_rowwise_tp,
    copy_to_tensor_model_parallel_region,
    reduce_from_tensor_model_parallel_region,
)
from fms.modules.positions import PositionEncoder


class MultiHeadAttention(nn.Module):
    """
    Performs multi-headed self- or cross-attention, with optional attention masking.
    ...
    Args
    ----
    emb_dim : int
        Latent dimensionality of input and output tensors.
    emb_kq : int
        Latent dimensionality of each head in key and query projections (attention dimension).
    emb_v : int
        Latent dimensionality of each head in value projection (mixing dimension).
    nheads : int
        Number of attention heads.
    p_dropout : float|None
        Dropout probability. Must be in range [0,1]. If 0 or None, dropout will not be used.
    use_bias : bool
        Include bias terms in fully-connected sublayers?
    factorable_emb: Optional[Callable]
        Function that computes factorable embeddings (like RoPE). It is mutually exclusive with
        additive biases on forward() passed as rel_pos_bias
    """

    def __init__(
        self,
        emb_dim,
        emb_kq,
        emb_v,
        nheads,
        kvheads,
        p_dropout=None,
        use_bias=False,
        position_encoder: Optional[PositionEncoder] = None,
        gain=1,
    ):
        super(MultiHeadAttention, self).__init__()
        self.nheads = nheads
        self.kvheads = kvheads
        self.emb_dim = emb_dim
        self.emb_kq_per_head = emb_kq
        self.emb_v_per_head = emb_v
        self.p_dropout = p_dropout if p_dropout is not None else 0.0
        self.use_bias = use_bias
        self.query = nn.Linear(
            self.emb_dim, self.nheads * self.emb_kq_per_head, bias=use_bias
        )
        self.key = nn.Linear(
            self.emb_dim, self.kvheads * self.emb_kq_per_head, bias=use_bias
        )
        self.value = nn.Linear(
            self.emb_dim, self.kvheads * self.emb_v_per_head, bias=use_bias
        )
        self.dense = nn.Linear(
            self.nheads * self.emb_v_per_head, self.emb_dim, bias=use_bias
        )
        if self.p_dropout:
            self.attn_dropout = nn.Dropout(self.p_dropout)
        self.position_encoder = position_encoder
        # Avoiding graph breaks
        self.previous_flash: bool = torch.backends.cuda.flash_sdp_enabled()
        self.previous_mem_efficient: bool = (
            torch.backends.cuda.mem_efficient_sdp_enabled()
        )
        self.previous_math: bool = torch.backends.cuda.math_sdp_enabled()
        self.head_size = emb_dim // nheads
        self.head_mapping = torch.repeat_interleave(
            torch.arange(
                self.kvheads, dtype=torch.int32, device=self.query.weight.device
            ),
            self.nheads // self.kvheads,
        )
        self.reset_params(gain)

    def reset_params(self, gain=1):
        # Ensure softmax inputs are standard normal
        for layer in ["query", "key"]:
            nn.init.trunc_normal_(
                getattr(self, layer).weight, mean=0.0, std=self.emb_dim**-0.5
            )
        # Ensure projection layers have same scale (for normalized-step dataloaders like
        # AdamW / Sophia), and maintain input norm up to attention remix, in expectation
        for layer in ["value", "dense"]:
            nn.init.trunc_normal_(
                getattr(self, layer).weight,
                mean=0.0,
                std=(gain / (self.emb_dim * self.nheads * self.emb_v_per_head) ** 0.5)
                ** 0.5,
            )  # Using explicit terms instead of numel to account for eventual MQA addition
        if self.use_bias:
            for layer in ["query", "key", "value", "dense"]:
                getattr(self, layer).bias.data.zero_()

    def forward(
        self,
        q,
        k,
        v,
        mask=None,
        position_ids=None,
        attn_algorithm=None,
        past_key_value_state=None,
        use_cache=False,
        cache_metadata=None,
        is_self=True,
        is_causal_mask=False,
    ):
        """
        past_key_value_state: tuple
            the cache to be used in attention of the form (<self/cross>_key, <self/cross>_value)
        position_ids: Optional[torch.LongTensor]
            The position of each of the tokens encoded in q and k. Used for RoPE embeddings
        use_cache: bool
            if True, the kv states for self/cross attention will be saved, otherwise they will not be saved
        is_self: bool
            if True, this will perform self attention, otherwise this will perform cross attention. Note: This will
            only be used in the case that use_cache=True. This may be removed in future

        Returns
        -------
        tensor or tuple
            If use_cache=False, only the hidden state will be returned as a tensor. If use_cache=True, a tuple will be
            returned in the form (hidden_state, cache) where hidden_state is a tensor and cache is of the form specified
            in past_key_value_state
        """

        # q, k, v: batch_size x seq_len x emb_dim
        # mask: batch_size x seq_len x seq_len
        batch_size, q_len, _ = q.size()
        kv_len = k.size(1)
        if use_cache:
            cache_type = cache_metadata.get("type", "default")
            is_generating = cache_metadata.get("is_generating", False)
            # todo: we are making an assumption here that the user provided a position_offset
            if position_ids is None:
                position_ids = cache_metadata.get("position_offset", None)

        # split emb_dim as nheads*emb_dim_per_head
        # b x h x qlen x ds
        queries = self.query(q).view(
            batch_size, q_len, self.nheads, self.emb_kq_per_head
        )
        queries = queries.transpose(2, 1)  # / (self.emb_kq_per_head**(1/4))

        # if this is self attention, we always recompute
        # cross attention only gets computed when a cache does not exist
        # if we dont have the cache yet, we need to compute
        # d x (h x ds)
        # b x kvlen x d
        # b x kvlen x h x ds
        # b x h x kvlen x ds
        if is_self or past_key_value_state is None:
            keys = self.key(k).view(
                batch_size, kv_len, self.kvheads, self.emb_kq_per_head
            )
            keys = keys.transpose(2, 1)  # / (self.emb_kq_per_head**(1/4))

            values = self.value(v).view(
                batch_size, kv_len, self.kvheads, self.emb_v_per_head
            )
            values = values.transpose(2, 1)  # compatible with QK.T

            # You want to apply rotary embeddings pre-cache
            if self.position_encoder is not None:
                queries, keys = self.position_encoder.adjusted_qk(
                    queries,
                    keys,
                    position_ids,
                    use_cache,
                )

        # store the values in kv-cache
        if use_cache:
            # we need to use low level kernels for storing paged attention
            if cache_type == "paged_attention":
                key_to_cache = keys.transpose(2, 1).reshape(
                    -1, self.kvheads, self.head_size
                )
                value_to_cache = values.transpose(2, 1).reshape(
                    -1, self.kvheads, self.head_size
                )
                past_key_value_state = torch.ops.paged_attention.reshape_and_cache(
                    key_to_cache,
                    value_to_cache,
                    past_key_value_state[0],
                    past_key_value_state[1],
                    cache_metadata["slot_mapping"],
                )
            # fall back to simple torch.cat
            else:
                if past_key_value_state is not None:
                    if is_self:
                        keys = torch.cat((past_key_value_state[0], keys), dim=2)
                        values = torch.cat((past_key_value_state[1], values), dim=2)
                    else:
                        keys = past_key_value_state[0]
                        values = past_key_value_state[1]

        # use the special paged_attention call if use_cache=True, its type is paged_attention, and it is generating
        if use_cache and cache_type == "paged_attention" and is_generating:
            queries = queries.transpose(2, 1).reshape(-1, self.nheads, self.head_size)

            # 5x4
            # -> 20x1
            # [ [1,2,3], [4,5,6] ]
            # [ [1,2,3], [1,2,3], [4,5,6], [4,5,6] ]
            # [ 5, 10 ]
            # [ 5, 4, 3, 10, 9, 8]
            # Pre-allocate the output tensor.
            attn = torch.empty_like(queries)
            context_lengths = cache_metadata["context_lengths"]
            context_lengths = context_lengths.unsqueeze(1).expand(-1, q_len)
            context_lengths = context_lengths.sub(context_lengths.sign().cumsum(1).flip([1]).sub(1)).int()
            block_tables = cache_metadata["block_tables"].repeat_interleave(q_len, dim=0)
            attn = torch.ops.paged_attention.paged_attention_v1(
                attn,
                # num_sequences x num_heads x head_size
                queries,
                past_key_value_state[0],
                past_key_value_state[1],
                self.head_mapping,
                (self.emb_dim // self.nheads) ** -0.5,
                block_tables,
                context_lengths,
                cache_metadata["block_size"],
                cache_metadata["max_sequence_length"],
                None,
            )
            # 20x1
            # -> 5x4
            attn = attn.view(batch_size, q_len, self.nheads * self.emb_v_per_head)
        # otherwise we always fall back into SDPA as this is either a prompt or it is a single contiguous cache
        else:
            # Merge rel pos bias and mask into single float mask
            if mask is not None:
                # Our expected mask format is bs x q_len x k_len, so to make it broadcastable
                # we need to create the nheads dimension
                while len(mask.size()) != 4:  # expects bs (x nheads) x q_len x kv_len
                    mask = mask.unsqueeze(1)

            if self.position_encoder is not None:
                attn_mask = self.position_encoder.adjusted_mask(
                    mask, queries, keys, position_ids, use_cache
                )
            else:
                attn_mask = mask

            # Expand kv so black-box attn will work
            expansion = self.nheads // self.kvheads
            # k/v: b h l d
            if expansion != 1:
                keys_e = (
                    keys.unsqueeze(2).expand(-1, -1, expansion, -1, -1).flatten(1, 2)
                )
                values_e = (
                    values.unsqueeze(2).expand(-1, -1, expansion, -1, -1).flatten(1, 2)
                )
            else:
                keys_e = keys
                values_e = values

            if attn_algorithm:
                # Pick which fused attn kernels will run.
                use_flash = attn_algorithm == "flash"
                use_mem_efficient = attn_algorithm == "mem"
                use_math = attn_algorithm == "math"

                torch.backends.cuda.enable_flash_sdp(use_flash)
                torch.backends.cuda.enable_mem_efficient_sdp(use_mem_efficient)
                torch.backends.cuda.enable_math_sdp(use_math)

            attn = F.scaled_dot_product_attention(
                queries,
                keys_e,
                values_e,
                attn_mask=attn_mask,
                dropout_p=self.p_dropout if self.training else 0.0,
                is_causal=is_causal_mask,
            )

            if attn_algorithm:
                torch.backends.cuda.enable_flash_sdp(self.previous_flash)
                torch.backends.cuda.enable_mem_efficient_sdp(
                    self.previous_mem_efficient
                )
                torch.backends.cuda.enable_math_sdp(self.previous_math)

            # attn: bs x seq_len x nheads*emb_v_per_head
            # attn: b x h x qlen x ds
            # attn after permute: b x qlen x h x ds
            # b x qlen x (d)
            attn = (
                attn.transpose(2, 1)
                .contiguous()
                .view(batch_size, q_len, self.nheads * self.emb_v_per_head)
            )
        out = self.dense(attn)

        # if use_cache=True, we return the hidden_state as well as the kv cache
        if use_cache:
            if cache_type == "paged_attention":
                keys, values = past_key_value_state

            return out, (keys, values)
        else:
            return out


class TPMultiHeadAttention(MultiHeadAttention):
    """
    Performs multi-headed self- or cross-attention, with optional attention masking.
    This subclass adds support for Tensor Parallel
    ...
    Args
    ----
    Check MultiHeadAttention for up-to-date docs

    world_size: int
        the number of processes running this model in TP
    rank: int
        the index of this process wrt to the rest running the model in TP
    """

    def __init__(
        self,
        emb_dim,
        emb_kq,
        emb_v,
        nheads,
        kvheads,
        p_dropout=None,
        use_bias=False,
        position_encoder: Optional[PositionEncoder] = None,
        gain=1,
        group: Optional[ProcessGroup] = None,
    ):
        assert torch.distributed.is_initialized()

        rank, world_size = distributed.rank_and_world(group)
        assert (
            nheads % world_size == 0
        ), "The number of heads must be divisible by world size"
        super(TPMultiHeadAttention, self).__init__(
            emb_dim,
            emb_kq,
            emb_v,
            nheads // world_size,
            (kvheads // world_size) if kvheads > 1 else kvheads,
            p_dropout,
            use_bias,
            position_encoder,
            gain,
        )

        self.rank = rank
        self.world_size = world_size
        self.head_size = self.head_size // world_size

    @staticmethod
    def import_module(
        mha: MultiHeadAttention, group: ProcessGroup
    ) -> "TPMultiHeadAttention":
        tp_mha = TPMultiHeadAttention(
            emb_dim=mha.emb_dim,
            emb_kq=mha.emb_kq_per_head,
            emb_v=mha.emb_v_per_head,
            nheads=mha.nheads,
            kvheads=mha.kvheads,
            p_dropout=mha.p_dropout,
            use_bias=mha.use_bias,
            position_encoder=mha.position_encoder,
            group=group,
        )
        return tp_mha

    def import_weights(self, mha: MultiHeadAttention):
        apply_colwise_tp(self.query, mha.query, self.world_size, self.rank)
        if self.kvheads == 1:
            with torch.no_grad():
                self.key.weight.copy_(mha.key.weight)
                self.value.weight.copy_(mha.value.weight)
                if self.use_bias:
                    self.key.bias.copy_(mha.key.bias)
                    self.value.bias.copy_(mha.value.bias)
        else:
            apply_colwise_tp(self.key, mha.key, self.world_size, self.rank)
            apply_colwise_tp(self.value, mha.value, self.world_size, self.rank)
        apply_rowwise_tp(self.dense, mha.dense, self.world_size, self.rank)

    def forward(
        self,
        q,
        k,
        v,
        mask=None,
        position_ids=None,
        attn_algorithm=None,
        past_key_value_state=None,
        use_cache=False,
        cache_metadata=None,
        is_self=True,
        is_causal_mask=False,
    ):
        """
        Check MultiHeadAttention for up-to-date arguments and docs
        """

        q_par = copy_to_tensor_model_parallel_region(q)
        k_par = copy_to_tensor_model_parallel_region(k)
        v_par = copy_to_tensor_model_parallel_region(v)
        # rel_pos_bias_par = copy_to_tensor_model_parallel_region(rel_pos_bias)

        out_par = MultiHeadAttention.forward(
            self,
            q_par,
            k_par,
            v_par,
            mask,
            position_ids,
            attn_algorithm,
            past_key_value_state,
            use_cache,
            cache_metadata,
            is_self,
            is_causal_mask,
        )

        # if use_cache=True, we return the hidden_state as well as the kv cache.
        # We only reduce the output, and keep the cache thread-local
        if use_cache:
            out = reduce_from_tensor_model_parallel_region(out_par[0])
            return out, out_par[1]
        else:
            out = reduce_from_tensor_model_parallel_region(out_par)
            return out
