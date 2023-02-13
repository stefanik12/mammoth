""" Pytorch Distributed utils
    This piece of code was heavily inspired by the equivalent of Fairseq-py
    https://github.com/pytorch/fairseq
"""
import math
import numpy as np
import os
import pickle
import signal
import torch.distributed

from abc import ABC, abstractmethod
from argparse import Namespace
from collections import OrderedDict, namedtuple
from dataclasses import dataclass
from enum import Enum
from itertools import cycle, islice
from pprint import pformat
from typing import Any, Optional, List

from onmt.utils.logging import init_logger, logger
from onmt.utils.misc import set_random_seed


class DeviceContextEnum(Enum):
    CPU = 1
    SINGLE_GPU = 2
    MULTI_GPU = 3


@dataclass
class WorldContext:
    context: DeviceContextEnum
    # Size of the world: total number of nodes, gpus on each node
    n_nodes: int
    gpus_per_node: int

    @property
    def world_size(self):
        """Total number of training GPUs"""
        return self.n_nodes * self.gpus_per_node

    def is_distributed(self):
        """When training is distributed over several devices,
        multiprocessing is used to communicate gradients"""
        return self.context == DeviceContextEnum.MULTI_GPU

    def is_gpu(self):
        """Data tensors must be moved to the GPU for compute"""
        return self.context != DeviceContextEnum.CPU

    def is_master(self):
        """For code that should only run in one process:
        - saving fully shared modules from one device only
        - avoiding log spam when all devices would log the same result
        """
        return not self.is_distributed() or self.global_rank == 0

    def global_to_local(self, node_rank, local_rank):
        assert node_rank is not None
        assert local_rank is not None
        return DeviceContext(
            context=self.context,
            n_nodes=self.n_nodes,
            gpus_per_node=self.gpus_per_node,
            node_rank=node_rank,
            local_rank=local_rank,
        )

    @classmethod
    def from_opt(cls, opt):
        gpus_per_node = len(opt.gpu_ranks)
        world_size = int(opt.world_size) if gpus_per_node > 0 else 0
        multinode = gpus_per_node != world_size
        if world_size <= 0:
            # setting a non-positive world size means use CPU
            device_context_enum = DeviceContextEnum.CPU
            if opt.n_nodes != 1:
                raise ValueError('CPU training is only possible on a single node')
        elif world_size == 1:
            # world size 1 uses GPU, but is not distributed
            device_context_enum = DeviceContextEnum.SINGLE_GPU
            if opt.n_nodes != 1:
                raise ValueError(
                    f'Invalid single-gpu node configuration: '
                    f'n_nodes {opt.n_nodes} gpus_per_node {gpus_per_node} world_size {world_size}'
                )
        else:
            # world size > 1
            if multinode and opt.n_nodes == 1:
                raise ValueError(
                    f'Invalid multi-node configuration: '
                    f'n_nodes {opt.n_nodes} gpus_per_node {gpus_per_node} world_size {world_size}'
                )
            device_context_enum = DeviceContextEnum.MULTI_GPU
        world_context = WorldContext(context=device_context_enum, n_nodes=opt.n_nodes, gpus_per_node=gpus_per_node)
        return world_context


@dataclass
class DeviceContext(WorldContext):
    # Our place in the world
    node_rank: int
    local_rank: int

    @property
    def global_rank(self) -> int:
        return self.gpus_per_node * self.node_rank + self.local_rank

    @property
    def id(self) -> str:
        if self.is_gpu():
            return f'GPU {self.node_rank}:{self.local_rank}'
        else:
            return 'CPU'

    def validate(self, world_context):
        # check that this DeviceContext is consistent with given WorldContext
        assert self.context == world_context.context
        assert self.n_nodes == world_context.n_nodes
        assert self.gpus_per_node == world_context.gpus_per_node
        # check that ranks are within the specified size of the world
        assert 0 <= self.node_rank < self.n_nodes
        if self.is_gpu():
            assert 0 <= self.local_rank < self.gpus_per_node


def multi_init(opt, global_rank):
    dist_init_method = 'tcp://{master_ip}:{master_port}'.format(master_ip=opt.master_ip, master_port=opt.master_port)

    dist_world_size = opt.world_size
    torch.distributed.init_process_group(
        backend=opt.gpu_backend,
        init_method=dist_init_method,
        rank=global_rank,
        world_size=dist_world_size,
    )

    gpu_rank = torch.distributed.get_rank()

    return gpu_rank


def broadcast_tensors(tensors, src=0, group=None):
    for t in tensors:
        if group is None:
            torch.distributed.broadcast(t, src)
        else:
            torch.distributed.broadcast(t, src, group=group)


def only_ready_reduce_and_rescale_grads(named_parameters, group=None):
    """
    Gradient synch tolerant to missing grads.

    Missing grads occur when some parameters are not trained between two
    gradient synchs, e.g. the embeddings of a low-resource language with low
    sampling weight.

    The algorithm first uses the 'has_grad' attribute set by the forward hook
    'has_grad_hook'. This hook ensures that all parameters of the modules
    selected for use during the current training computation have 'has_grad'
    set to True. This gives the list of parameters that have been trained on
    this device ("ready").

    A bit mask covering the parameters that are ready on this device is
    communicated to the other devices in the group. The bit masks are reduced
    using summation. The sum gives the number of real gradients for that
    parameter, and can be used for normalization.

    If a parameter is ready on any device, all devices communicate a value.
    Devices on which the parameter is ready communicate the actual gradient,
    while devices on which it is not ready communicate a dummy zero tensor
        instead. The sum computed previously is used for normalization.

    Args:
        named_parameters: tuples of (str, Parameter) defining the parameters to consider
        group: torch.distributed communication group
    """
    # Set missing gradients to zero, keeping track of true gradients
    require_grad = [(name, p) for (name, p) in named_parameters if p.requires_grad]
    if not require_grad:
        # Exit early if the component has no parameters that require a gradient
        return
    device = require_grad[0][1].device
    ready_list = []
    for name, p in require_grad:
        if hasattr(p, 'has_grad') and p.has_grad:
            ready_list.append(1.0)
        else:
            ready_list.append(0.0)
            if p.grad is None:
                p.grad = torch.zeros_like(p)

    # Communicate the ready bits, and reduce them using summation.
    # This gives the number of non-dummy gradients participating, for normalization
    logger.warning(f'DEBUG: start of Communicate the ready bits. ready_list = {ready_list}')
    ready_t = torch.tensor(ready_list).to(device)
    if group is None:
        logger.warning('DEBUG: no group, before')
        torch.distributed.all_reduce(ready_t)
        logger.warning('DEBUG: no group, after')
    else:
        logger.warning('DEBUG: with group, before')
        torch.distributed.all_reduce(ready_t, group=group)
        logger.warning('DEBUG: with group, after')
    rescale_denoms = ready_t  # after reduction
    logger.warning(f'DEBUG: after reduction. rescale_denoms = {rescale_denoms}')

    # Omit if all nodes sent a zero ready bit
    grads = [p.grad.data for name, p in require_grad]
    logger.warning(f'DEBUG: grads len A {len(grads)}')
    grads = [grad for (grad, denom) in zip(grads, rescale_denoms) if denom > 0]
    logger.warning(f'DEBUG: grads len B {len(grads)}')
    rescale_denoms = [denom for denom in rescale_denoms if denom > 0]
    logger.warning(f'DEBUG: rescale_denoms len B {len(rescale_denoms)}')
    assert len(grads) == len(rescale_denoms)
    logger.warning('DEBUG: passed assert')
    if len(grads) == 0:
        logger.warning('DEBUG: returning early')
        return
    logger.warning('DEBUG: NOT returning early')

    # If not, then set has_grad also on devices that did not train the parameter themselves.
    # They now have a grad that they received from the other devices.
    for name, p in require_grad:
        p.has_grad = True
    logger.warning('DEBUG: everything now has has_grad')

    # All devices communicate either a real gradient or a dummy zeros of the same size
    # Can not use rescale_denom, as each grad may have its own denominator
    all_reduce_and_rescale_tensors(grads, rescale_denom=1, group=group)
    logger.warning('DEBUG: after allreduce. grads len {len(grads)}')

    # Normalize using the previously computed values
    for grad, denom in zip(grads, rescale_denoms):
        if denom > 0:
            grad.div_(denom)
    logger.warning('DEBUG: after allreduce, end of function')
    # Note: p.has_grad is reused in the optimizer to prevent the untrained components from being stepped


def all_reduce_and_rescale_tensors(tensors, rescale_denom, group=None, buffer_size=10485760):
    """
    All-reduce and rescale tensors in chunks of the specified size.

    Args:
        tensors: list of Tensors to all-reduce
        rescale_denom: denominator for rescaling summed Tensors
        buffer_size: all-reduce chunk size in bytes
    """
    # buffer size in bytes, determine equiv. # of elements based on data type
    buffer_t = tensors[0].new(math.ceil(buffer_size / tensors[0].element_size())).zero_()
    buffer = []

    def all_reduce_buffer():
        # copy tensors into buffer_t
        offset = 0
        for t in buffer:
            numel = t.numel()
            buffer_t[offset:offset + numel].copy_(t.view(-1))
            offset += numel

        # all-reduce and rescale
        if group is None:
            torch.distributed.all_reduce(buffer_t[:offset])
        else:
            torch.distributed.all_reduce(buffer_t[:offset], group=group)
        buffer_t.div_(rescale_denom)

        # copy all-reduced buffer back into tensors
        offset = 0
        for t in buffer:
            numel = t.numel()
            t.view(-1).copy_(buffer_t[offset:offset + numel])
            offset += numel

    filled = 0
    for t in tensors:
        sz = t.numel() * t.element_size()
        if sz > buffer_size:
            # tensor is bigger than buffer, all-reduce and rescale directly
            if group is None:
                torch.distributed.all_reduce(t)
            else:
                torch.distributed.all_reduce(t, group=group)
            t.div_(rescale_denom)
        elif filled + sz > buffer_size:
            # buffer is full, all-reduce and replace buffer with grad
            all_reduce_buffer()
            buffer = [t]
            filled = sz
        else:
            # add tensor to buffer
            buffer.append(t)
            filled += sz

    if len(buffer) > 0:
        all_reduce_buffer()


def all_gather_list(data, max_size=4096):
    """Gathers arbitrary data from all nodes into a list."""
    world_size = torch.distributed.get_world_size()
    if not hasattr(all_gather_list, '_in_buffer') or max_size != all_gather_list._in_buffer.size():
        all_gather_list._in_buffer = torch.cuda.ByteTensor(max_size)
        all_gather_list._out_buffers = [torch.cuda.ByteTensor(max_size) for i in range(world_size)]
    in_buffer = all_gather_list._in_buffer
    out_buffers = all_gather_list._out_buffers

    enc = pickle.dumps(data)
    enc_size = len(enc)
    if enc_size + 2 > max_size:
        raise ValueError('encoded data exceeds max_size: {}'.format(enc_size + 2))
    assert max_size < 255 * 256
    in_buffer[0] = enc_size // 255  # this encoding works for max_size < 65k
    in_buffer[1] = enc_size % 255
    in_buffer[2:enc_size + 2] = torch.ByteTensor(list(enc))

    torch.distributed.all_gather(out_buffers, in_buffer.cuda())

    results = []
    for i in range(world_size):
        out_buffer = out_buffers[i]
        size = (255 * out_buffer[0].item()) + out_buffer[1].item()

        bytes_list = bytes(out_buffer[2:size + 2].tolist())
        result = pickle.loads(bytes_list)
        results.append(result)
    return results


class ErrorHandler(object):
    """A class that listens for exceptions in children processes and propagates
    the tracebacks to the parent process."""

    def __init__(self, error_queue):
        """init error handler"""
        import signal
        import threading

        self.error_queue = error_queue
        self.children_pids = []
        self.error_thread = threading.Thread(target=self.error_listener, daemon=True)
        self.error_thread.start()
        signal.signal(signal.SIGUSR1, self.signal_handler)

    def add_child(self, pid):
        """error handler"""
        self.children_pids.append(pid)

    def error_listener(self):
        """error listener"""
        (rank, original_trace) = self.error_queue.get()
        self.error_queue.put((rank, original_trace))
        os.kill(os.getpid(), signal.SIGUSR1)

    def signal_handler(self, signalnum, stackframe):
        """signal handler"""
        for pid in self.children_pids:
            os.kill(pid, signal.SIGINT)  # kill children processes
        (rank, original_trace) = self.error_queue.get()
        msg = """\n\n-- Tracebacks above this line can probably
                 be ignored --\n\n"""
        msg += original_trace
        raise Exception(msg)


def batch_producer(generator_to_serve, queue, semaphore, opt, device_id):
    """Produce batches to `queues` from `generator_to_serve`."""
    log_level = "INFO" if opt.verbose or device_id == 0 else "WARNING"
    init_logger(opt.log_file, log_level=log_level)
    set_random_seed(opt.seed, False)
    logger.info("BATCH PRODUCER")
    logger.info(generator_to_serve)

    for batch, metadata, communication_batch_id in generator_to_serve:
        semaphore.acquire()
        # Move batch to correspond device_id when consumer iterate
        # hack to dodge unpicklable `dict_keys`
        # batch.fields = list(batch.fields)
        queue.put((batch, metadata, communication_batch_id))


def consumer(process_fn, opt, device_context, error_queue, batch_queue, semaphore, task_queue_manager):
    """Run `process_fn` on `device_id` with data from `batch_queue`."""
    try:
        logger.info(
            f'global_rank {device_context.global_rank} '
            f'node_rank {device_context.node_rank} '
            f'local_rank {device_context.local_rank}'
        )
        logger.info(f'opt.gpu_ranks {opt.gpu_ranks}')
        multi_init(opt, device_context.global_rank)
        # error_queue not passed (is this intentional?)
        process_fn(
            opt,
            device_context=device_context,
            batch_queue=batch_queue,
            semaphore=semaphore,
            task_queue_manager=task_queue_manager,
        )

    except KeyboardInterrupt:
        pass  # killed by parent, do nothing
    except Exception:
        # propagate exception to parent process, keeping original traceback
        import traceback

        error_queue.put((opt.gpu_ranks[device_context.node_rank], traceback.format_exc()))


class TaskDistributionStrategy(ABC):
    @abstractmethod
    def __init__(self, my_corpus_ids: List[str], **kwargs):
        pass

    @classmethod
    @abstractmethod
    def from_opt(cls, my_corpus_ids: List[str], opt: dict):
        pass

    @abstractmethod
    def sample_corpus_ids(self, n_samples: int, communication_batch_id: int) -> List[str]:
        pass


class WeightedSamplingTaskDistributionStrategy(TaskDistributionStrategy):
    """
    Schedules tasks by sampling with replacement from a categorical distribution.
    The probabilities are found by normalizing the weights of all valid tasks (corpora).
    Valid tasks are those that are present on this device, and have already reached
    their curriculum starting point "introduce_at_training_step".
    """

    def __init__(
        self,
        my_corpus_ids: List[str],
        my_weights: List[float],
        my_introduce_at_training_step: List[int]
    ):
        self.my_corpus_ids = my_corpus_ids
        self.my_weights = my_weights
        self.my_introduce_at_training_step = my_introduce_at_training_step

        # Sanity check of weights and curriculum
        assert len(self.my_corpus_ids) == len(self.my_weights)
        assert len(self.my_corpus_ids) == len(self.my_introduce_at_training_step)
        if len(self.my_corpus_ids) == 0:
            raise ValueError('No corpora on device')
        if sum(my_weights) <= 0:
            raise ValueError('Can not set "weight" of all corpora on a device to zero')
        if all(x > 0 for x in my_introduce_at_training_step):
            raise ValueError('Can not set "introduce_at_training_step" of all corpora on a device to nonzero')
        if all(weight == 0 or start > 0 for (weight, start) in zip(my_weights, my_introduce_at_training_step)):
            raise ValueError('Invalid curriculum: no corpus is ready to start in the first step')

    @classmethod
    def from_opt(cls, my_corpus_ids: List[str], opt: dict):
        my_weights = [opt.data[corpus_id]['weight'] for corpus_id in my_corpus_ids]
        my_introduce_at_training_step = [
            opt.data[corpus_id]['introduce_at_training_step'] for corpus_id in my_corpus_ids
        ]
        return cls(my_corpus_ids, my_weights, my_introduce_at_training_step)

    def sample_corpus_ids(
        self,
        n_samples: int,
        communication_batch_id: int,
    ):
        weights = [
            weight if introduce_at_training_step <= communication_batch_id else 0
            for (corpus_id, weight, introduce_at_training_step) in zip(
                self.my_corpus_ids, self.my_weights, self.my_introduce_at_training_step
            )
        ]
        sum_w = sum(weights)
        assert sum_w > 0
        p = [weight / sum_w for weight in weights]
        # sampling with replacement from weighted corpora (language pairs)
        sampled_corpus_ids = np.random.choice(self.my_corpus_ids, size=n_samples, p=p)
        return sampled_corpus_ids


class RoundRobinTaskDistributionStrategy(TaskDistributionStrategy):
    """
    Schedules tasks (corpora) in a round-robin fashion.
    Yields a communication batch of n_samples at a time.
    When reaching the end of the list of tasks, starts over from the beginning.
    """

    def __init__(self, my_corpus_ids: List[str]):
        self.infinite_corpus_ids = cycle(my_corpus_ids)

    @classmethod
    def from_opt(cls, my_corpus_ids: List[str], opt: dict):
        return cls(my_corpus_ids)

    def sample_corpus_ids(
        self,
        n_samples: int,
        communication_batch_id: int,
    ):
        return list(islice(self.infinite_corpus_ids, n_samples))


TASK_DISTRIBUTION_STRATEGIES = {
    'weighted_sampling': WeightedSamplingTaskDistributionStrategy,
    'roundrobin': RoundRobinTaskDistributionStrategy,
}

DatasetMetadata = namedtuple(
    'DatasetMetadata',
    'src_lang tgt_lang encoder_id decoder_id corpus_id encoder_adapter_ids decoder_adapter_ids'
)


@dataclass
class TaskSpecs():
    node_rank: int
    local_rank: int
    src_lang: str
    tgt_lang: str
    encoder_id: List[str]
    decoder_id: List[str]
    corpus_id: str
    weight: int
    corpus_opt: dict
    src_vocab: Any  # FIXME: type
    tgt_vocab: Any
    encoder_adapter_ids: List[str]
    decoder_adapter_ids: List[str]

    def get_serializable_metadata(self):
        """
        TaskSpecs contains objects that should not be serialized
        and sent over the multiprocessing message queue.
        The DatasetMetadata namedtuple can be serialized.
        """
        return DatasetMetadata(
            src_lang=self.src_lang,
            tgt_lang=self.tgt_lang,
            encoder_id=self.encoder_id,
            decoder_id=self.decoder_id,
            corpus_id=self.corpus_id,
            encoder_adapter_ids=self.encoder_adapter_ids,
            decoder_adapter_ids=self.decoder_adapter_ids,
        )


def get_adapter_ids(opt, corpus_opt, side):
    if 'adapters' not in opt or 'adapters' not in corpus_opt:
        return []
    global_adapters_opt = opt.adapters.get(side, None)
    corpus_adapter_opt = corpus_opt['adapters'].get(side, None)
    if not global_adapters_opt or not corpus_adapter_opt:
        return []
    result = []
    for adapter_group, sub_id in corpus_adapter_opt:
        layer_stack_index = global_adapters_opt[adapter_group]['layer_stack_index']
        result.append((layer_stack_index, adapter_group, sub_id))
    return result


class TaskQueueManager:
    def __init__(
        self,
        tasks: List[TaskSpecs],
        tasks_per_communication_batch: int,
        world_context: WorldContext,
        device_context: Optional[DeviceContext] = None,
        components_to_gpus=None,
        components_to_groups=None,
        task_distribution_strategy: Optional[TaskDistributionStrategy] = None,
        uses_adapters: bool = False,
    ):
        """
        Schedules tasks (language pairs) to devices.
        Has the responsibility for all resources that need to be
        consistently assigned to nodes and GPUs.
        This includes data, parameters, and vocabularies.

        `local_rank` is the local rank of the GPU on this node.
        When `node_rank` and `local_rank` are given, the methods return only
        the items needed in the specified process.
        When set to None, all items are returned.
        """
        self.tasks = tasks
        self.tasks_per_communication_batch = tasks_per_communication_batch
        self.task_distribution_strategy = task_distribution_strategy
        self.world_context = world_context
        self.device_context = device_context
        self.uses_adapters = uses_adapters

        if self.world_context and self.device_context:
            logger.info(f'in task_queue_manager: node_rank {self.node_rank} local_rank {self.local_rank}')
            self.device_context.validate(self.world_context)

        self.components_to_gpus = components_to_gpus
        self.components_to_groups = components_to_groups

    @property
    def gpus_per_node(self):
        return self.world_context.gpus_per_node

    @property
    def n_nodes(self):
        return self.world_context.n_nodes

    @property
    def node_rank(self):
        if not self.device_context:
            raise Exception('Trying to get node_rank of global TQM')
        return self.device_context.node_rank

    @property
    def local_rank(self):
        if not self.device_context:
            raise Exception('Trying to get local_rank of global TQM')
        return self.device_context.local_rank

    @classmethod
    def from_opt(cls, opt: Namespace, world_context: WorldContext):
        n_tasks = len(opt.data)

        if world_context.is_distributed():
            if any(task.get('node_gpu', None) is not None for task in opt.data.values()):
                node_gpu = [tuple(int(y) for y in task['node_gpu'].split(':', 1)) for task in opt.data.values()]
            else:
                # When --node_gpu is not set, assume an assigment that fills gpus in rank order
                node_gpu = cls._default_node_gpu(n_tasks, world_context.n_nodes, world_context.gpus_per_node)
        else:
            node_gpu = [(0, 0)] * n_tasks

        enc_sharing_group = [task.get('enc_sharing_group', None) for task in opt.data.values()]
        dec_sharing_group = [task.get('dec_sharing_group', None) for task in opt.data.values()]
        if any(x is not None for x in enc_sharing_group):
            assert all(len(enc_ids) == len(opt.enc_layers) for enc_ids in enc_sharing_group)
        else:
            # if no encoder sharing groups are defined,
            # it is assumed that there is only one encoder stack and it is language specific
            if not len(opt.enc_layers) == 1:
                raise Exception('With more than one encoder stack, you must explictly define enc_sharing_group')
        if any(x is not None for x in dec_sharing_group):
            assert all(len(dec_ids) == len(opt.dec_layers) for dec_ids in dec_sharing_group)
        else:
            # if no decoder sharing groups are defined,
            # it is assumed that there is only one decoder stack and it is language specific
            if not len(opt.dec_layers) == 1:
                raise Exception('With more than one decoder stack, you must explictly define dec_sharing_group')

        corpus_ids = opt.data.keys()

        tasks = []
        uses_adapters = False
        for (
            (node_rank, local_rank),
            corpus_id
        ) in zip(
            node_gpu,
            corpus_ids
        ):
            corpus_opt = opt.data[corpus_id]
            src_lang, tgt_lang = corpus_opt['src_tgt'].split('-', 1)
            encoder_id = corpus_opt.get('enc_sharing_group', [src_lang])
            decoder_id = corpus_opt.get('dec_sharing_group', [tgt_lang])
            weight = corpus_opt.get('weight', 1.0)
            if 'adapters' in corpus_opt:
                encoder_adapter_ids = get_adapter_ids(opt, corpus_opt, 'encoder')
                decoder_adapter_ids = get_adapter_ids(opt, corpus_opt, 'decoder')
                uses_adapters = True
            else:
                encoder_adapter_ids = None
                decoder_adapter_ids = None
            task = TaskSpecs(
                node_rank=node_rank,
                local_rank=local_rank,
                src_lang=src_lang,
                tgt_lang=tgt_lang,
                encoder_id=encoder_id,
                decoder_id=decoder_id,
                corpus_id=corpus_id,
                weight=weight,
                corpus_opt=corpus_opt,
                src_vocab=None,
                tgt_vocab=None,
                encoder_adapter_ids=encoder_adapter_ids,
                decoder_adapter_ids=decoder_adapter_ids,
            )
            tasks.append(task)
        return cls(
            tasks,
            world_context=world_context,
            tasks_per_communication_batch=opt.accum_count,
            uses_adapters=uses_adapters,
        )

    def global_to_local(self, node_rank, local_rank, opt):
        assert node_rank is not None
        assert local_rank is not None
        task_distribution_strategy = self._get_strategy(node_rank=node_rank, local_rank=local_rank, opt=opt)
        device_context = self.world_context.global_to_local(node_rank, local_rank)
        return self.__class__(
            self.tasks,
            tasks_per_communication_batch=self.tasks_per_communication_batch,
            world_context=self.world_context,
            device_context=device_context,
            components_to_gpus=self.components_to_gpus,
            components_to_groups=self.components_to_groups,
            task_distribution_strategy=task_distribution_strategy,
            uses_adapters=self.uses_adapters,
        )

    def _get_strategy(self, node_rank, local_rank, opt):
        assert node_rank is not None
        assert local_rank is not None
        # Global TQM does not have a task distribution strategy, but the local ones do
        my_corpus_ids = [task.corpus_id for task in self._tasks_on_device(node_rank, local_rank)]
        try:
            strategy = TASK_DISTRIBUTION_STRATEGIES[opt.task_distribution_strategy].from_opt(
                my_corpus_ids=my_corpus_ids,
                opt=opt,
            )
            return strategy
        except Exception as e:
            raise Exception(
                f'Exception when creating task distribution strategy on {node_rank}:{local_rank} {e}'
            )

    def __repr__(self):
        kwargs = ',\n '.join(
            f'{key}={pformat(self.__getattribute__(key))}'
            for key in [
                'tasks',
                'gpus_per_node',
                'n_nodes',
                'node_rank',
                'local_rank',
                'task_distribution_strategy',
                'uses_adapters',
            ]
        )
        return f'{self.__class__.__name__}(\n{kwargs}\n)'

    def _tasks_on_device(self, node_rank, local_rank):
        return [task for task in self.tasks if (task.node_rank, task.local_rank) == (node_rank, local_rank)]

    def get_tasks(self):
        if not self.device_context:
            # global mode: return all
            return self.tasks
        else:
            return self._tasks_on_device(self.node_rank, self.local_rank)

    @staticmethod
    def _default_node_gpu(n_tasks, n_nodes, gpus_per_node):
        def yield_each_gpu():
            for node_rank in range(n_nodes):
                for local_rank in range(gpus_per_node):
                    yield (node_rank, local_rank)

        # yield GPUs in rank order, repeat as necessary
        return list(islice(cycle(yield_each_gpu()), n_tasks))

    def create_all_distributed_groups(
        self,
        new_group_func=torch.distributed.new_group,
    ):
        if not self.world_context.is_distributed():
            self.components_to_gpus = dict()
            self.components_to_groups = dict()
            return self.components_to_groups

        # Single OrderedDict contains all components.
        # Keys are tuples of strings.
        # The length of the key varies depending on the component:
        # ('encoder', layer_stack_index, encoder_id)
        # ('decoder', layer_stack_index, decoder_id)
        # ('src_emb', lang)
        # ('tgt_emb', lang)
        # ('encoder_adapters', layer_stack_index, encoder_id, adapter_group, sub_id)
        # ('decoder_adapters', layer_stack_index, decoder_id, adapter_group, sub_id)
        self.components_to_gpus = OrderedDict()

        for node_rank in range(self.n_nodes):
            for local_rank in range(self.gpus_per_node):
                global_rank = node_rank * self.gpus_per_node + local_rank
                tasks = self._tasks_on_device(node_rank, local_rank)

                for task in tasks:
                    keys = [
                        ('src_emb', task.src_lang),
                        ('tgt_emb', task.tgt_lang),
                    ]
                    for layer_stack_index, encoder_id in enumerate(task.encoder_id):
                        keys.append(('encoder', layer_stack_index, encoder_id))
                    for layer_stack_index, decoder_id in enumerate(task.decoder_id):
                        keys.append(('decoder', layer_stack_index, decoder_id))
                    for key in keys:
                        # Using setdefault to treat OrderedDict as defaultdict
                        self.components_to_gpus.setdefault(key, set()).add(global_rank)

                    if task.encoder_adapter_ids:
                        for layer_stack_index, adapter_group, sub_id in task.encoder_adapter_ids:
                            encoder_id = task.encoder_id[layer_stack_index]
                            key = ('encoder_adapters', layer_stack_index, encoder_id, adapter_group, sub_id)
                            self.components_to_gpus.setdefault(key, set()).add(global_rank)
                    if task.decoder_adapter_ids:
                        for layer_stack_index, adapter_group, sub_id in task.decoder_adapter_ids:
                            decoder_id = task.decoder_id[layer_stack_index]
                            key = ('decoder_adapters', layer_stack_index, decoder_id, adapter_group, sub_id)
                            self.components_to_gpus.setdefault(key, set()).add(global_rank)

        # Structured, each component in a separate OrderedDict
        self.components_to_groups = {
            component_type: OrderedDict() for component_type
            in ('encoder', 'decoder', 'src_emb', 'tgt_emb')
        }
        if self.uses_adapters:
            self.components_to_groups['encoder_adapters'] = OrderedDict()
            self.components_to_groups['decoder_adapters'] = OrderedDict()
        for key, global_ranks in self.components_to_gpus.items():
            if len(global_ranks) < 2:
                # only create a process group if the component is on 2 or more gpus
                continue
            sorted_global_ranks = list(sorted(global_ranks))
            min_rank = sorted_global_ranks[0]
            group_tpl = (min_rank, new_group_func(sorted_global_ranks))
            component_type = key[0]
            component_id = key[1:]
            self.components_to_groups.setdefault(component_type, OrderedDict())[component_id] = group_tpl

        return self.components_to_groups

    @property
    def global_rank(self):
        assert self.node_rank is not None
        assert self.local_rank is not None
        return self.node_rank * self.gpus_per_node + self.local_rank

    def get_distributed_groups(
        self,
        new_group_func=torch.distributed.new_group,
    ):
        """
        Returns pairs of (component_id, process_group).
        Only components present on this GPU are returned.
        The pairs are returned in a consistent order across GPUs.
        """
        if self.components_to_groups is None:
            self.create_all_distributed_groups(new_group_func)
        logger.info(f'components_to_groups: {self.components_to_groups}')

        my_distributed_groups = {
            'encoder': OrderedDict(),
            'decoder': OrderedDict(),
            'src_emb': OrderedDict(),
            'tgt_emb': OrderedDict(),
            'encoder_adapters': OrderedDict(),
            'decoder_adapters': OrderedDict(),
        }

        if self.global_rank is None:
            # Training on CPU, or called on global TaskQueueManager
            for component_type, components in self.components_to_groups.items():
                my_distributed_groups[component_type] = components

        global_rank = self.global_rank

        for key, global_ranks in self.components_to_gpus.items():
            if global_rank not in global_ranks:
                # omit groups that are not on this device
                continue
            component_type = key[0]
            component_id = key[1:]
            if component_id not in self.components_to_groups[component_type]:
                # omit components on a single device
                logger.info(f'{component_type} {component_id} is on a single device')
                continue
            my_distributed_groups[component_type][component_id] = \
                self.components_to_groups[component_type][component_id]

        return my_distributed_groups

    def get_grouped_components(self, model):
        """
        Returns nested dict of component_type -> component_id -> nn.Module.
        Only components present on this GPU are returned.
        Unlike get_distributed_groups, this method also returns components on a single device,
        and it does not retrieve communication groups.
        """
        if self.components_to_groups is None:
            raise Exception('Must call get_distributed_groups first')

        my_grouped_components = {
            'encoder': OrderedDict(),
            'decoder': OrderedDict(),
            'src_emb': OrderedDict(),
            'tgt_emb': OrderedDict(),
            'encoder_adapters': OrderedDict(),
            'decoder_adapters': OrderedDict(),
        }

        if not self.world_context.is_distributed():
            tasks = self.tasks
        else:
            tasks = self.get_tasks()

        for task in tasks:
            # loop over my tasks, getting all the relevant module ids and modules
            my_grouped_components['src_emb'][task.src_lang] = model.encoder.embeddings[f'embeddings_{task.src_lang}']
            my_grouped_components['tgt_emb'][task.tgt_lang] = model.decoder.embeddings[f'embeddings_{task.tgt_lang}']
            for layer_stack_index, encoder_id in enumerate(task.encoder_id):
                component = model.encoder.get_submodule(layer_stack_index, encoder_id)
                my_grouped_components['encoder'][(layer_stack_index, encoder_id)] = component
            for layer_stack_index, decoder_id in enumerate(task.decoder_id):
                component = model.decoder.get_submodule(layer_stack_index, decoder_id)
                my_grouped_components['decoder'][(layer_stack_index, decoder_id)] = component
            if task.encoder_adapter_ids:
                for layer_stack_index, adapter_group, sub_id in task.encoder_adapter_ids:
                    encoder_id = task.encoder_id[layer_stack_index]
                    key = (layer_stack_index, encoder_id, adapter_group, sub_id)
                    component = model.encoder.get_submodule(
                        layer_stack_index, encoder_id
                    ).get_adapter(adapter_group, sub_id)
                    my_grouped_components['encoder_adapters'][key] = component
            if task.decoder_adapter_ids:
                for layer_stack_index, adapter_group, sub_id in task.decoder_adapter_ids:
                    decoder_id = task.decoder_id[layer_stack_index]
                    key = (layer_stack_index, decoder_id, adapter_group, sub_id)
                    component = model.decoder.get_submodule(
                        layer_stack_index, decoder_id
                    ).get_adapter(adapter_group, sub_id)
                    my_grouped_components['decoder_adapters'][key] = component

        return my_grouped_components

    # TODO: soon deprecated by #18 Data pipeline refactoring
    def get_fields(self, side: str, fields_dict):
        """Returns a list of tuples: (side, lang, component_id, fields)."""
        raise RuntimeError

    # FIXME: merge with below
    def get_vocabularies(self, opt: Namespace, side: str):
        result = []
        for task in self.get_tasks():
            lang = self.src_lang if side == 'src' else self.tgt_lang
            vocab_path = opt.__getattribute__(f'{side}_vocab')[lang]
            result.append((lang, vocab_path))
        return result

    def get_vocabs(self, side: str, vocabs_dict):
        """Returns a list of tuples: (side, lang, component_id, vocabs).
        side:           Either 'src' or 'tgt'.
        lang:           The language code. Vocabularies are language specific.
        component_id:   None
        vocabs_dict:    The actual vocabs.
        """
        seen = set()
        result = []
        component_id = None     # for hysterical raisins
        for task in self.get_tasks():
            if side == 'src':
                lang = task.src_lang
            else:
                lang = task.tgt_lang
            if not (side, lang, component_id) in seen:
                result.append((side, lang, component_id, vocabs_dict[(side, lang)]))
            seen.add((side, lang, component_id))
        return result

    def sample_corpus_ids(self, communication_batch_id: int):
        return self.task_distribution_strategy.sample_corpus_ids(
            self.tasks_per_communication_batch,
            communication_batch_id,
        )

    def get_encoders(self, layer_stack_index: int):
        my_encoder_ids = [task.encoder_id[layer_stack_index] for task in self.get_tasks()]
        return my_encoder_ids

    def get_decoders(self, layer_stack_index: int):
        my_decoder_ids = [task.decoder_id[layer_stack_index] for task in self.get_tasks()]
        return my_decoder_ids

    def get_src_langs(self):
        return [task.src_lang for task in self.get_tasks()]

    def get_tgt_langs(self):
        return [task.tgt_lang for task in self.get_tasks()]

    def get_generators(self):
        return [task.tgt_lang for task in self.get_tasks()]

    def get_langs(self, side):
        if side == 'src':
            return [task.src_lang for task in self.get_tasks()]
        elif side == 'tgt':
            return [task.tgt_lang for task in self.get_tasks()]
        else:
            raise ValueError(f'side "{side}" not in {{src, tgt}}')
