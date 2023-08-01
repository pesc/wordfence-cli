import os
import queue
from ctypes import c_bool, c_uint
from enum import IntEnum
from multiprocessing import Queue, Process, Pool, Value
from dataclasses import dataclass
from typing import Set

from . import matcher
from .exceptions import ScanningException
from .matcher import Matcher, RegexMatcher
from ..util import timing
from ..intel.signatures import SignatureSet

MAX_PENDING_FILES = 1000  # Arbitrary limit
MAX_PENDING_RESULTS = 100
QUEUE_READ_TIMEOUT = 0
DEFAULT_CHUNK_SIZE = 1024 * 1024


class ScanConfigurationException(ScanningException):
    pass


@dataclass
class Options:
    paths: Set[str]
    signatures: SignatureSet
    threads: int = 1
    chunk_size: int = DEFAULT_CHUNK_SIZE


class Status(IntEnum):
    LOCATING_FILES = 0
    PROCESSING_FILES = 1
    COMPLETE = 2
    FAILED = 3


class FileLocator:

    def __init__(self, path: str, queue: Queue):
        self.path = path
        self.queue = queue
        self.located_count = 0

    def search_directory(self, path: str):
        try:
            contents = os.scandir(path)
            for item in contents:
                if item.is_dir():
                    yield from self.search_directory(item.path)
                elif item.is_file():
                    self.located_count += 1
                    yield item.path
        except OSError as os_error:
            raise ScanningException('Directory search failed') from os_error

    def locate(self):
        # TODO: Handle links and prevent loops
        real_path = os.path.realpath(self.path)
        if os.path.isdir(real_path):
            for path in self.search_directory(real_path):
                self.queue.put(path)
        else:
            self.queue.put(real_path)


class FileLocatorProcess(Process):

    def __init__(
                self,
                input_queue_size: int = 10,
                output_queue_size: int = MAX_PENDING_FILES
            ):
        self._input_queue = Queue(input_queue_size)
        self.output_queue = Queue(output_queue_size)
        super().__init__(name='file-locator')

    def add_path(self, path: str):
        self._input_queue.put(path)

    def finalize_paths(self):
        self._input_queue.put(None)

    def get_next_file(self):
        return self.output_queue.get()

    def run(self):
        try:
            while (path := self._input_queue.get()) is not None:
                locator = FileLocator(path, self.output_queue)
                locator.locate()
            self.output_queue.put(None)
        except ScanningException as exception:
            self.output_queue.put(exception)


class ScanEventType(IntEnum):
    COMPLETED = 0
    FILE_QUEUE_EMPTIED = 1
    FILE_PROCESSED = 2
    EXCEPTION = 3
    FATAL_EXCEPTION = 4


class ScanEvent:

    # TODO: Define custom (more compact) pickle serialization format for this
    # class as a potential performance improvement

    def __init__(self, worker_index: int, type: int, data=None):
        self.worker_index = worker_index
        self.type = type
        self.data = data


class ScanWorker(Process):

    def __init__(
                self,
                index: int,
                status: Value,
                work_queue: Queue,
                result_queue: Queue,
                matcher: Matcher,
                chunk_size: int = DEFAULT_CHUNK_SIZE
            ):
        self.index = index
        self._status = status
        self._work_queue = work_queue
        self._result_queue = result_queue
        self._matcher = matcher
        self._chunk_size = chunk_size
        self._working = True
        self.complete = Value(c_bool, False)
        super().__init__(name=self._generate_name())

    def _generate_name(self) -> str:
        return 'worker-' + str(self.index)

    def work(self):
        self._working = True
        print('Worker Started: ' + str(os.getpid()))
        while self._working:
            try:
                item = self._work_queue.get(timeout=QUEUE_READ_TIMEOUT)
                if item is None:
                    self._put_event(ScanEventType.FILE_QUEUE_EMPTIED)
                    self._complete()
                elif isinstance(item, BaseException):
                    self._put_event(
                            ScanEventType.FATAL_EXCEPTION,
                            {'exception': item}
                        )
                else:
                    self._process_file(item)
            except queue.Empty:
                if self._status.value == Status.PROCESSING_FILES:
                    self._complete()

    def _put_event(self, event_type: ScanEventType, data: dict = {}) -> None:
        self._result_queue.put(ScanEvent(self.index, event_type, data))

    def _complete(self):
        self._working = False
        self.complete.value = True
        self._put_event(ScanEventType.COMPLETED)

    def is_complete(self) -> bool:
        return self.complete.value

    def _process_file(self, path: str):
        try:
            with open(path, mode='rb') as file:
                context = self._matcher.create_context()
                length = 0
                while (chunk := file.read(self._chunk_size)):
                    length += len(chunk)
                    context.process_chunk(chunk)
                matches = context.get_matches()
                self._put_event(
                        ScanEventType.FILE_PROCESSED,
                        {'path': path, 'length': length, 'matches': matches}
                    )
        except OSError as error:
            self._put_event(ScanEventType.EXCEPTION, {'exception': error})

    def run(self):
        self.work()


class ScanMetrics:

    def __init__(self, worker_count: int):
        self.counts = self._initialize_int_metric(worker_count)
        self.bytes = self._initialize_int_metric(worker_count)

    def _initialize_int_metric(self, worker_count: int):
        return [0] * worker_count

    def record_result(self, worker_index: int, length: int):
        self.counts[worker_index] += 1
        self.bytes[worker_index] += length

    def _aggregate_int_metric(self, metric: list) -> int:
        total = 0
        for value in metric:
            total += value
        return total

    def get_total_count(self) -> int:
        return self._aggregate_int_metric(self.counts)

    def get_total_bytes(self) -> int:
        return self._aggregate_int_metric(self.bytes)


class ScanWorkerPool:

    def __init__(
                self,
                size: int,
                work_queue: Queue,
                matcher: Matcher,
                metrics: ScanMetrics,
                chunk_size: int = DEFAULT_CHUNK_SIZE
            ):
        self.size = size
        self._matcher = matcher
        self._work_queue = work_queue
        self.metrics = metrics
        self._chunk_size = chunk_size
        self._started = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()

    def start(self):
        if self._started:
            raise ScanningException('Worker pool has already been started')
        self._status = Value(c_uint, Status.LOCATING_FILES)
        self._result_queue = Queue(MAX_PENDING_RESULTS)
        self._workers = []
        for i in range(self.size):
            worker = ScanWorker(
                    i,
                    self._status,
                    self._work_queue,
                    self._result_queue,
                    self._matcher,
                    self._chunk_size
                )
            worker.start()
            self._workers.append(worker)
        self._started = True

    def _assert_started(self):
        if not self._started:
            raise ScanningException('Worker pool has not been started')

    def stop(self):
        self._assert_started()
        for worker in self._workers:
            worker.join()

    def terminate(self):
        self._assert_started()
        for worker in self._workers:
            worker.terminate()

    def is_complete(self) -> bool:
        self._assert_started()
        for worker in self._workers:
            if not worker.is_complete():
                return False
        return True

    def await_results(self):
        self._assert_started()
        while True:
            event = self._result_queue.get()
            if event is None:
                print('All workers complete and all results processed...')
                return
            elif event.type == ScanEventType.COMPLETED:
                print('Worker completed ' + str(event.worker_index))
                if self.is_complete():
                    self._result_queue.put(None)
            elif event.type == ScanEventType.FILE_PROCESSED:
                self.metrics.record_result(
                        event.worker_index,
                        event.data['length']
                    )
                matches = event.data['matches']
                if len(matches):
                    print('File at ' + event.data['path'] + ' has matches')
                    for signature_id, state in matches.items():
                        print(
                                event.data['path'] +
                                ' matched signature ' +
                                str(signature_id)
                            )
            elif event.type == ScanEventType.FILE_QUEUE_EMPTIED:
                self._status.value = Status.PROCESSING_FILES
            elif event.type == ScanEventType.EXCEPTION:
                print(
                        'Exception occurred while processing file: ' +
                        str(event.data['exception'])
                    )
            elif event.type == ScanEventType.FATAL_EXCEPTION:
                self._status.value = Status.FAILED
                self.terminate()
                raise event.data['exception']

    def is_failed(self) -> bool:
        return self._status.value == Status.FAILED


class Scanner:

    def __init__(self, options: Options):
        self.options = options
        self.processed = 0
        self.bytes_read = 0
        self.failed = 0

    def _handle_worker_error(self, error: Exception):
        self.failed += 1
        raise error

    def _initialize_worker(
                self,
                status: Value,
                work_queue: Queue,
                result_queue: Queue
            ):
        worker = ScanWorker(status, work_queue, result_queue)
        worker.work()

    def scan(self):
        """Run a scan"""
        if len(self.options.paths) == 0:
            raise ScanConfigurationException(
                    'At least one scan path must be specified'
                )
        timer = timing.Timer()
        file_locator_process = FileLocatorProcess()
        file_locator_process.start()
        for path in self.options.paths:
            file_locator_process.add_path(path)
        file_locator_process.finalize_paths()
        worker_count = self.options.threads
        print("Using " + str(worker_count) + " workers")
        matcher = RegexMatcher(self.options.signatures)
        metrics = ScanMetrics(worker_count)
        with ScanWorkerPool(
                    worker_count,
                    file_locator_process.output_queue,
                    matcher,
                    metrics,
                    self.options.chunk_size
                ) as worker_pool:
            worker_pool.await_results()
        timer.stop()
        print(
                "Processed " +
                str(metrics.get_total_count()) +
                " files containing " +
                str(metrics.get_total_bytes()) +
                " bytes in " +
                str(timer.get_elapsed()) +
                " seconds"
            )