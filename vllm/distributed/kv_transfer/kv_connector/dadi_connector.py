# SPDX-License-Identifier: Apache-2.0
"""
MooncakeStore Connector for Distributed Machine Learning Inference

The DADIConnector transfers KV caches between prefill vLLM workers
(KV cache producer) and decode vLLM workers (KV cache consumer) using a
database-style KVStore.
"""
import hashlib
from typing import TYPE_CHECKING, List, Tuple, Union

import torch

from vllm import _custom_ops as ops
from vllm.config import VllmConfig
from vllm.distributed.kv_transfer.kv_connector.base import KVConnectorBase
from vllm.logger import init_logger
from vllm.sequence import IntermediateTensors

if TYPE_CHECKING:
    from vllm.worker.model_runner import ModelInputForGPUWithSamplingMetadata

logger = init_logger(__name__)

from safetensors.torch import load as safetensors_load
from safetensors.torch import save as safetensors_save

from dadikvcacheclient import DadiKVCacheClient, KVCacheOption

class DADIConnector(KVConnectorBase):

    def __init__(
        self,
        rank: int,
        local_rank: int,
        config: VllmConfig,
    ):
        self.config = config.kv_transfer_config
        self.tp_size = config.parallel_config.tensor_parallel_size

        self.local_tp_rank = local_rank

        opt = KVCacheOption()
        opt.read_worker_num = 24
        opt.prefetch = 0
        opt.prefetch_worker_num = 16
        opt.prefetch_worker_concurrency = 1
        opt.cache_name = 'DADI_P2PKV_CACHE_1_'
        opt.circuit_options = 'sprocessors=0,work_processors=8'
        opt.enable_barex = False
        self.opt = opt
        self.client = DadiKVCacheClient(uds="/run/p2png/p2png1.sock", opt=self.opt)


    def close(self) -> None:
        """Close the buffer and release resources.
        This method is responsible for cleaning up resources related to the
        connector when it is no longer needed.
        Raises:
            NotImplementedError: This method must be implemented in subclasses.
        """

    def put(
        self,
        key: str,
        value: torch.Tensor,
    ) -> None:
        """Put KVCache to DADI Store"""
        device_id = value.device.index if value.device.type == 'cuda' else -1
        device_tensor = torch.tensor(device_id, dtype=torch.int32)
        value_bytes = safetensors_save({
            "tensor": value,
            "device_id": device_tensor
        })
        try:
            self.client.put(key, value_bytes)
        except TypeError as err:
            logger.error("Failed to put value into DADI Store: %s", err)
            raise TypeError("DADI Store Put Type Error.") from err

    def get(
        self,
        key: str,
    ) -> Optional[torch.Tensor]:
        """Get KVCache from DADI Store"""
        try:
            data = self.client.get(key)
        except TypeError as err:
            logger.error("Failed to get value from DADI Store: %s", err)
            raise TypeError("DADI Store Get Type Error.") from err

        if data:
            loaded_tensors = safetensors_load(data)
            tensor = loaded_tensors["tensor"]
            device_id_tensor = loaded_tensors["device_id"]
            device_id = int(device_id_tensor.item())
            device = torch.device(
                'cuda', device_id) if device_id >= 0 else torch.device('cpu')
            return tensor.to(device)

        return None

    def send_kv_caches_and_hidden_states(
        self,
        model_executable: torch.nn.Module,
        model_input: "ModelInputForGPUWithSamplingMetadata",
        kv_caches: List[torch.Tensor],
        hidden_or_intermediate_states: Union[torch.Tensor,
                                             IntermediateTensors],
    ) -> None:
        input_tokens_tensor = model_input.input_tokens
        seq_lens = model_input.attn_metadata.seq_lens
        slot_mapping_flat = model_input.attn_metadata.slot_mapping.flatten()
        start_layer = model_executable.model.start_layer
        end_layer = model_executable.model.end_layer

        model_config = model_executable.model.config
        num_heads = int(model_config.num_key_value_heads / self.tp_size)
        hidden_size = model_config.hidden_size
        num_attention_heads = model_config.num_attention_heads
        head_size = int(hidden_size / num_attention_heads)

        for idx, slen in enumerate(seq_lens):
            start_pos = sum(seq_lens[:idx])
            end_pos = start_pos + slen

            current_tokens = input_tokens_tensor[start_pos:end_pos]
            store_key_prefix = self.tensor_hash(current_tokens)
            keys, values = [], []

            for layer_id in range(start_layer, end_layer):
                kv_cache = kv_caches[layer_id - start_layer]

                key_cache = kv_cache[0].reshape(-1, num_heads, head_size)
                value_cache = kv_cache[1].reshape(-1, num_heads, head_size)

                current_slot_mapping = slot_mapping_flat[start_pos:end_pos]

                keys.append(key_cache[current_slot_mapping].unsqueeze(0))
                values.append(value_cache[current_slot_mapping].unsqueeze(0))

            keys = torch.cat(keys, dim=0)
            values = torch.cat(values, dim=0)
            kvcache_to_sent = torch.stack((keys, values), dim=0)
            store_kvcache_key = f"{store_key_prefix}_{self.local_tp_rank}"
            self.put(store_kvcache_key, kvcache_to_sent)

            hidden_key = f"{store_key_prefix}_hidden_{self.local_tp_rank}"
            self.put(hidden_key, hidden_or_intermediate_states[start_pos:end_pos])

        logger.debug("[rank%d]: KV send DONE.", torch.distributed.get_rank())

    def recv_kv_caches_and_hidden_states(
        self, model_executable: torch.nn.Module,
        model_input: "ModelInputForGPUWithSamplingMetadata",
        kv_caches: List[torch.Tensor]
    ) -> Tuple[Union[torch.Tensor, IntermediateTensors], bool,
               "ModelInputForGPUWithSamplingMetadata"]:
        bypass_model_exec = True
        input_tokens_tensor = model_input.input_tokens
        seq_lens = model_input.attn_metadata.seq_lens
        num_prefill_tokens = model_input.attn_metadata.num_prefill_tokens
        slot_mapping = model_input.attn_metadata.slot_mapping.flatten()
        start_layer = model_executable.model.start_layer
        end_layer = model_executable.model.end_layer
        hidden_or_intermediate_states_for_one_req = []

        for idx, slen in enumerate(seq_lens):
            start_pos = sum(seq_lens[:idx])
            end_pos = start_pos + slen

            if start_pos >= num_prefill_tokens:
                # This can happen during inflight batching. See:
                # vllm/worker/model_runner.py::_prepare_model_input_tensors:
                # - input_tokens[:num_prefill_tokens] contains prefill tokens.
                # - input_tokens[num_prefill_tokens:] contains decode tokens.
                logger.warning("You should set --enable_chunked_prefill=False "
                               "and --max_num_batched_tokens "
                               "should be equal to max_seq_len_to_capture")
                bypass_model_exec = False
                assert start_pos == num_prefill_tokens
                break

            current_tokens = input_tokens_tensor[start_pos:end_pos]

            # get roi for current seq
            load_key_prefix = self.tensor_hash(current_tokens)
            load_kvcache_key = f"{load_key_prefix}_{self.local_tp_rank}"
            remote_kv = self.get(load_kvcache_key)
            hidden_key = f"{load_key_prefix}_hidden_{self.local_tp_rank}"
            hidden = self.get(hidden_key)

            if remote_kv is None or hidden is None:
                # didn't find any match.
                bypass_model_exec = False
                continue

            num_computed_tokens = current_tokens.shape[0]

            # update the end position based on how many tokens are cached.
            end_pos = start_pos + num_computed_tokens

            # call self.kv_store to get kv layer by layer
            for layer_id in range(start_layer, end_layer):
                layer = model_executable.model.layers[layer_id]
                # get kvcache object
                kv_cache = kv_caches[layer_id - start_layer]
                key_cache, value_cache = kv_cache[0], kv_cache[1]
                # get remote kvcache

                remote_k, remote_v = remote_kv[0][layer_id], remote_kv[1][
                    layer_id]
                # use ops.reshape_and_cache_flash to put kv into kvcache
                ops.reshape_and_cache_flash(
                    remote_k.to(key_cache.device),
                    remote_v.to(value_cache.device),
                    key_cache,
                    value_cache,
                    slot_mapping[start_pos:end_pos],
                    layer.self_attn.attn.kv_cache_dtype,
                    layer.self_attn.attn._k_scale,
                    layer.self_attn.attn._v_scale,
                )

            hidden_or_intermediate_states_for_one_req.append(hidden)

        if not bypass_model_exec:
            logger.warning(
                "[rank%d]: Failed to receive all KVs and hidden "
                "states, redo model forwarding.", torch.distributed.get_rank())
            hidden_or_intermediate_states = None

        else:
            logger.debug(
                "[rank%d]: Successfully received all KVs and hidden "
                "states, skip model forwarding.", torch.distributed.get_rank())
            hidden_or_intermediate_states = torch.cat(
                hidden_or_intermediate_states_for_one_req, dim=0)

        return hidden_or_intermediate_states, bypass_model_exec, model_input

    @staticmethod
    def tensor_hash(tensor: torch.Tensor) -> int:
        """Calculate the hash value of the tensor."""
        tensor_bytes = tensor.clone().detach().cpu().numpy().tobytes()
        hash_object = hashlib.blake2b(tensor_bytes)
        hash_hex = hash_object.hexdigest()
        return int(hash_hex[:16], 16)
