""" LlaMa model with MoEs"""
import warnings
from functools import partial
from typing import Optional, Tuple

import numpy as np
import torch
import torch.nn.functional as F
from config_llamoe import LlaMoEConfig

try:
    import megablocks.ops as ops
    from megablocks.layers.all_to_all import all_to_all
except ImportError:
    warnings.warn("Please install megablocks to use MoEs: `pip install megablocks`")

import stk
from megablocks.layers import weight_parallel as wp
from megablocks.layers.activation_fn import act_fn
from nanotron import distributed as dist
from nanotron.config import ParallelismArgs
from torch import nn


class dMoE(torch.nn.Module):
    def __init__(
        self,
        config: LlaMoEConfig,
        expert_parallel_group: dist.ProcessGroup,
        tp_pg: dist.ProcessGroup,
        parallel_config: Optional[ParallelismArgs],
    ):
        super().__init__()
        self.config = config

        # Token router.
        self.gate = LearnedRouter(config)

        # Expert computation helper.
        self.experts = ParallelDroplessMLP(
            config,
            use_bias=False,
            expert_parallel_group=expert_parallel_group,
            tp_pg=tp_pg,
            parallel_config=parallel_config,
        )

    def forward(self, x: torch.Tensor):
        """
        Args:
            x: input tensor of shape [sequence_length, batch_size, hidden_size]
        """
        # Compute the expert scores and assignments.
        # TODO: support sequence parallelism
        batch_size, sequence_length, _ = x.size()
        x = x.view(-1, self.config.hidden_size)
        scores, expert_weights, top_experts = self.gate(x)

        # Compute the experts.
        x = self.experts(x, scores, expert_weights, top_experts)
        return x.reshape(batch_size, sequence_length, -1)


# Adapted from megablocks.layers.router.LearnedRouter
class LearnedRouter(torch.nn.Module):
    def __init__(self, config: LlaMoEConfig):
        super().__init__()
        self.layer = torch.nn.Linear(config.hidden_size, config.moe_num_experts, bias=False)
        # TODO: initialization
        self.config = config

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        router_logits = self.layer(x)  # (batch * sequence_length, n_experts)
        scores = F.softmax(router_logits, dim=-1, dtype=torch.float32)  # TODO: fuse?

        if self.config.num_experts_per_tok == 1:
            expert_weights, expert_indices = scores.max(dim=-1, keepdim=True)
        else:
            expert_weights, expert_indices = torch.topk(scores, self.config.num_experts_per_tok, dim=-1)

        return scores, expert_weights, expert_indices.int()


# Adapted from megablocks.layers.mlp.ParallelDroplessMLP
class ParallelDroplessMLP(torch.nn.Module):
    def __init__(
        self,
        config: LlaMoEConfig,
        use_bias: bool,
        expert_parallel_group: dist.ProcessGroup,
        tp_pg: dist.ProcessGroup,
        parallel_config: Optional[ParallelismArgs],
    ):
        super().__init__()
        self.config = config
        self.use_bias = use_bias

        self.expert_pg_size = expert_parallel_group.size()
        self.expert_parallel_group = expert_parallel_group

        self.hidden_sharding_degree = self.expert_pg_size // min(self.expert_pg_size, self.config.moe_num_experts)
        self.experts_per_rank = self.config.moe_num_experts // min(self.expert_pg_size, self.config.moe_num_experts)

        self.num_experts = config.moe_num_experts
        self.num_experts_per_tok = self.config.num_experts_per_tok

        # Calculate the number of bits needed to represent the expert indices
        # so that we can pass it to radix sort.
        self.sort_end_bit = max(int(np.ceil(np.log2(self.num_experts))), 1)

        if use_bias:
            self.bias = torch.nn.Parameter(torch.empty(config.hidden_size))  # TODO: init

        # Select the forward function for the operating mode.
        self.forward_fn = self.parallel_forward_once if self.expert_pg_size > 1 else self.forward_once

        self.blocking = 128
        self.mlp = MLP(config=config, parallel_config=parallel_config, tp_pg=tp_pg)

        max_column_index = (self.config.intermediate_size * self.num_experts) // self.blocking
        self.transpose_sort_end_bit = max(int(np.ceil(np.log2(max_column_index))), 1)

    def indices_and_bins(self, top_expert):
        # Sort the expert ids to produce the scatter/gather
        # indices for the permutation.
        top_expert = top_expert.int()
        bin_ids, indices = ops.sort(top_expert, self.sort_end_bit)
        tokens_per_expert = ops.histogram(top_expert, self.num_experts)

        # Calculate the bin bounds for the sorted tokens.
        bins = inclusive_cumsum(tokens_per_expert, 0)
        return indices, bin_ids, bins, tokens_per_expert

    def indices_and_padded_bins(self, top_experts):
        # Sort the expert ids to produce the scatter/gather
        # indices for the permutation.
        bin_ids, indices = ops.sort(top_experts, self.sort_end_bit)

        # Histogram the expert ids to identify the number of
        # tokens routed to each expert.
        tokens_per_expert = ops.histogram(top_experts, self.num_experts)

        # Round the token counts up to the block size used in
        # the matrix muliplications. Caculate the starting
        # position of each bin.
        padded_tokens_per_expert = ops.round_up(tokens_per_expert, self.blocking)
        padded_bins = inclusive_cumsum(padded_tokens_per_expert, 0)

        # Calculate the bin bounds for the sorted tokens.
        bins = inclusive_cumsum(tokens_per_expert, 0)
        return indices, bin_ids, bins, padded_bins, tokens_per_expert

    def forward_once(self, x, expert_weights, top_experts):  # TODO: sparse
        with torch.no_grad():
            (
                indices,
                bin_ids,
                bins,
                padded_bins,
                tokens_per_expert,
            ) = self.indices_and_padded_bins(top_experts)

        # Route the tokens for MoE computation.
        x = ops.padded_gather(x, indices, bin_ids, bins, padded_bins, self.num_experts_per_tok)

        with torch.no_grad():
            topo = self.topology(x, padded_bins)

        x = self.mlp(x, topo)  # TODO: exp_pg=1 and num_experts=2 means the experts will get same data.

        # Un-route the data for the MoE output.
        x = ops.padded_scatter(
            x,
            indices,
            bin_ids,
            expert_weights,
            bins,
            padded_bins,
            self.num_experts_per_tok,
            -1,
        )
        return x, tokens_per_expert

    def parallel_forward_once(self, x, expert_weights, top_experts):
        with torch.no_grad():
            indices, bin_ids, bins, tokens_per_expert = self.indices_and_bins(top_experts)
            repeated_tokens_per_expert = ops.repeat(tokens_per_expert, (self.hidden_sharding_degree,))
            parallel_tokens_per_expert = torch.empty_like(repeated_tokens_per_expert)
            tpe_handle = torch.distributed.all_to_all_single(
                parallel_tokens_per_expert,
                repeated_tokens_per_expert,
                group=self.expert_parallel_group,
                async_op=True,
            )

        x = ops.gather(x, indices, bin_ids, bins, self.num_experts_per_tok)

        # Compute the number of tokens that will be received from each
        # device and permute the input data across the devices.
        with torch.no_grad():
            tpe_handle.wait()

            # Reshape to [expert_pg_size, num_experts_per_rank].
            repeated_tokens_per_expert = repeated_tokens_per_expert.view(self.expert_pg_size, self.experts_per_rank)
            parallel_tokens_per_expert = parallel_tokens_per_expert.view(self.expert_pg_size, self.experts_per_rank)

            send_counts = repeated_tokens_per_expert.cpu().sum(dim=-1)
            parallel_tokens_per_expert_cpu = parallel_tokens_per_expert.cpu()
            recv_counts = parallel_tokens_per_expert_cpu.sum(dim=-1)

            # Convert the send/recv counts to lists.
            send_counts = send_counts.tolist()
            recv_counts = recv_counts.tolist()
            tokens_received = sum(recv_counts)

        x = ops.repeat(x, (self.hidden_sharding_degree, 1))

        # Start the cross-device permutation asynchronously so we can
        # overlap communication with computation.
        parallel_x, parallel_x_handle = all_to_all(
            x, recv_counts, send_counts, self.expert_parallel_group, async_op=True
        )

        with torch.no_grad():
            replicate_bins = inclusive_cumsum(parallel_tokens_per_expert.flatten(), 0)

            # Construct the expert indices for the permuted tokens.
            parallel_top_expert = torch.remainder(
                torch.arange(
                    self.num_experts * self.hidden_sharding_degree,
                    dtype=torch.int32,
                    device=indices.device,
                ),
                self.experts_per_rank,
            )
            parallel_top_expert = ops.replicate(
                parallel_top_expert.unsqueeze(dim=0), replicate_bins, tokens_received
            ).flatten()

            parallel_bin_ids, parallel_indices = ops.sort(parallel_top_expert, self.sort_end_bit)

            # Calculate the bins boundaries from the token counts.
            parallel_tokens_per_expert = parallel_tokens_per_expert.sum(dim=0, dtype=torch.int)
            parallel_bins = inclusive_cumsum(parallel_tokens_per_expert, 0)

        # Locally permute the tokens and perform the expert computation.
        # Block to make sure that the cross-device permutation is complete.
        parallel_x_handle.wait()
        parallel_x = self.permute_and_compute(
            parallel_x,
            parallel_tokens_per_expert,
            parallel_indices,
            parallel_bin_ids,
            None,  # expert_weights
            parallel_bins,
            num_experts_per_tok=self.num_experts_per_tok,
        )

        # Un-permute the tokens across the devices.
        x, _ = all_to_all(parallel_x, send_counts, recv_counts, self.expert_parallel_group)

        # Reduce along the hidden sharding to get the final outputs.
        shape = (self.hidden_sharding_degree, -1, self.config.hidden_size)
        x = ops.sum(x.view(shape), dim=0)

        # Un-permute locally to setup for the next series of operations.
        x = ops.scatter(
            x,
            indices,
            bin_ids,
            expert_weights,
            bins,
            self.num_experts_per_tok,
        )
        return x, tokens_per_expert.flatten()

    def forward(self, x, scores, expert_weights, top_experts):
        """
        Args:
            x: input tensor of shape [sequence_length, batch_size, hidden_size]
            scores: tensor of shape [sequence_length * batch_size, n_experts]
            expert_weights: tensor of shape [sequence_length * batch_size, num_experts_per_tok]
            top_experts: tensor of shape [sequence_length * batch_size, num_experts_per_tok]
        """
        # Compute the experts.
        x, tokens_per_expert = self.forward_fn(x, expert_weights.flatten(), top_experts.flatten())

        if self.use_bias:
            return x + self.bias
        return x

    def permute_and_compute(
        self,
        x,
        tokens_per_expert,
        indices,
        bin_ids,
        expert_weights,
        bins,
        num_experts_per_tok,
    ):
        # Round the token counts up to the block size used in the matrix
        # multiplication. Calculate the starting position of each bin.
        padded_tokens_per_expert = ops.round_up(tokens_per_expert, self.blocking)
        padded_bins = inclusive_cumsum(padded_tokens_per_expert, 0)

        # Route the tokens for MoE computation.
        x = ops.padded_gather(x, indices, bin_ids, bins, padded_bins, num_experts_per_tok)

        # Perform the expert computation.
        with torch.no_grad():
            topo = self.topology(x, padded_bins)
        x = self.mlp(x, topo)

        # Un-route the data for the MoE output.
        return ops.padded_scatter(x, indices, bin_ids, expert_weights, bins, padded_bins, num_experts_per_tok)

    def sparse_transpose(self, size, row_indices, column_indices, offsets):
        block_columns = size[1] // self.blocking
        _, gather_indices = ops.sort(column_indices.int(), self.transpose_sort_end_bit)
        column_indices_t = row_indices.gather(0, gather_indices.long())
        block_offsets_t = gather_indices.int()

        zero = torch.zeros((1,), dtype=torch.int32, device=row_indices.device)
        nnz_per_column = ops.histogram(column_indices, block_columns)
        nnz_per_column = ops.inclusive_cumsum(nnz_per_column, 0)
        offsets_t = torch.cat([zero, nnz_per_column])
        return column_indices_t, offsets_t, block_offsets_t

    def topology(self, x, padded_bins):
        padded_tokens, _ = x.size()
        assert padded_tokens % self.blocking == 0
        assert self.config.intermediate_size % self.blocking == 0

        # Offsets for the sparse matrix. All rows have the
        # same number of nonzero blocks dictated by the
        # dimensionality of a single expert.
        block_rows = padded_tokens // self.blocking
        blocks_per_row = self.config.intermediate_size // self.blocking
        offsets = torch.arange(0, block_rows * blocks_per_row + 1, blocks_per_row, dtype=torch.int32, device=x.device)

        # Indices for the sparse matrix. The indices for
        # the intermediate matrix are dynamic depending
        # on the mapping of tokens to experts.
        column_indices = ops.topology(padded_bins, self.blocking, block_rows, blocks_per_row)

        # TODO(tgale): This is unused. Remove the need for this in stk.
        # For now, use meta init to save the device memory.
        data = torch.empty(column_indices.numel(), self.blocking, self.blocking, dtype=x.dtype, device="meta")
        shape = (padded_tokens, self.config.intermediate_size * self.experts_per_rank)
        row_indices = stk.ops.row_indices(shape, data, offsets, column_indices)
        column_indices_t, offsets_t, block_offsets_t = self.sparse_transpose(
            shape, row_indices, column_indices, offsets
        )
        return stk.Matrix(
            shape, data, row_indices, column_indices, offsets, column_indices_t, offsets_t, block_offsets_t
        )


class ScaleGradient(torch.autograd.Function):
    @staticmethod
    @torch.cuda.amp.custom_fwd
    def forward(ctx, x, scale):
        ctx.scale = scale
        return x

    @staticmethod
    @torch.cuda.amp.custom_bwd
    def backward(ctx, grad):
        return grad * ctx.scale, None


scale_gradient = ScaleGradient.apply


class ExpertParallel(nn.Module):
    """
    ExpertParallel serves to scale the gradients of the expert weights because unlike DP the gradients are not averaged across the expert parallel group.
    """

    def __init__(self, module, expert_parallel_size: int):
        super().__init__()
        self.module = module
        self.expert_parallel_size = expert_parallel_size

    def forward(self, *args, **kwargs):
        self.scale_gradients()
        return self.module(*args, **kwargs)

    def scale_gradients(self):
        scale_gradient(self.module, 1 / self.expert_parallel_size)


class MLP(nn.Module):
    def __init__(
        self,
        config: LlaMoEConfig,
        parallel_config: Optional[ParallelismArgs],
        tp_pg: dist.ProcessGroup,
    ):
        super().__init__()

        self.expert_pg_size = parallel_config.expert_parallel_size if parallel_config is not None else 1
        self.experts_per_rank = config.moe_num_experts // min(self.expert_pg_size, config.moe_num_experts)
        self.tp_pg = tp_pg

        self.w1 = ExpertParallel(
            nn.Linear(
                config.hidden_size, config.intermediate_size * self.experts_per_rank // tp_pg.size(), bias=False
            ),
            expert_parallel_size=self.expert_pg_size,
        )
        self.w2 = ExpertParallel(
            nn.Linear(
                config.hidden_size, config.intermediate_size * self.experts_per_rank // tp_pg.size(), bias=False
            ),
            expert_parallel_size=self.expert_pg_size,
        )

        if self.tp_pg.size() == 1:
            # transpose self.w1.module.weight
            self.w1.module.weight.data = self.w1.module.weight.data.T.contiguous()

        # TODO @nouamane: jit
        self.act = partial(F.gelu, approximate="tanh")
        self.sdd = partial(wp.sdd_nt, group=self.tp_pg) if self.tp_pg.size() > 1 else stk.ops.sdd
        self.dsd = partial(wp.dsd_nn, group=self.tp_pg) if self.tp_pg.size() > 1 else stk.ops.dsd

    def forward(self, x, topo):
        # Compute the MLP.
        self.w1.scale_gradients(), self.w2.scale_gradients()
        x = self.sdd(x.contiguous(), self.w1.module.weight, topo)
        activation_fn_out = act_fn(x, self.act)
        return self.dsd(activation_fn_out, self.w2.module.weight)


def inclusive_cumsum(x, dim):
    scalar = ops.inclusive_cumsum(x, dim)
    return scalar.view(1) if not len(scalar.size()) else scalar
