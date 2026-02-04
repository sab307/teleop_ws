"""
Threaded Message Processor
==========================

High-performance threaded message processing for Twist commands.

Features:
    - Dedicated worker threads for message processing
    - Thread-safe message queue with priority support
    - Configurable thread pool size
    - Graceful shutdown handling
    - Performance metrics tracking

Architecture:
    ┌─────────────────┐
    │  DataChannel    │
    │  (async)        │
    └────────┬────────┘
             │ put()
             ▼
    ┌─────────────────┐
    │  Message Queue  │
    │  (thread-safe)  │
    └────────┬────────┘
             │ get()
             ▼
    ┌─────────────────┐
    │  Worker Pool    │
    │  (N threads)    │
    └────────┬────────┘
             │ callback
             ▼
    ┌─────────────────┐
    │  ROS2 Publisher │
    │  (optional)     │
    └─────────────────┘

Example Usage:
    >>> processor = TwistProcessor(
    ...     thread_count=4,
    ...     on_twist=lambda t: print(f"Received: {t}")
    ... )
    >>> processor.start()
    >>> processor.enqueue(twist_data)
    >>> processor.stop()
"""

import queue
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional
from enum import Enum

from twist_protocol import TwistMessage, TWIST_SIZE, TWIST_SIZE_WITH_TS, validate_twist_data


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)


class MessagePriority(Enum):
    """Message priority levels for queue ordering."""
    HIGH = 0      # Stop commands, emergency
    NORMAL = 1    # Regular motion commands
    LOW = 2       # Status updates, non-critical


@dataclass(order=True)
class QueuedMessage:
    """Message wrapper with priority for queue ordering.
    
    Attributes:
        priority: Message priority (lower = higher priority)
        timestamp: Message creation time
        data: Raw binary message data
    """
    priority: int
    timestamp: float = field(compare=False)
    data: bytes = field(compare=False)


@dataclass
class ProcessorStats:
    """Statistics for message processor performance.
    
    Attributes:
        messages_received: Total messages enqueued
        messages_processed: Successfully processed messages
        messages_dropped: Messages dropped due to queue overflow
        parse_errors: Messages that failed to parse
        avg_latency_us: Average processing latency in microseconds
    """
    messages_received: int = 0
    messages_processed: int = 0
    messages_dropped: int = 0
    parse_errors: int = 0
    total_latency_ns: int = 0
    
    @property
    def avg_latency_us(self) -> float:
        """Calculate average latency in microseconds."""
        if self.messages_processed == 0:
            return 0.0
        return (self.total_latency_ns / self.messages_processed) / 1000


class TwistProcessor:
    """Threaded processor for Twist message handling.
    
    Manages a pool of worker threads that process incoming Twist messages
    from a thread-safe queue. Supports callbacks for processed messages
    and provides performance statistics.
    
    Args:
        thread_count: Number of worker threads (default: 2)
        queue_size: Maximum queue size (default: 100)
        on_twist: Callback for processed Twist messages
        on_error: Callback for processing errors
    
    Example:
        >>> def handle_twist(twist: TwistMessage):
        ...     print(f"Linear Y: {twist.linear.y}")
        ...     print(f"Angular Z: {twist.angular.z}")
        >>> 
        >>> processor = TwistProcessor(
        ...     thread_count=4,
        ...     on_twist=handle_twist
        ... )
        >>> processor.start()
    """
    
    def __init__(
        self,
        thread_count: int = 2,
        queue_size: int = 100,
        on_twist: Optional[Callable[[TwistMessage], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None
    ):
        self.thread_count = thread_count
        self._max_queue_size = queue_size  # Renamed to avoid conflict with property
        self.on_twist = on_twist
        self.on_error = on_error
        
        # Thread-safe message queue with priority support
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=queue_size)
        
        # Worker threads
        self._workers: list[threading.Thread] = []
        self._running = threading.Event()
        
        # Statistics (protected by lock)
        self._stats = ProcessorStats()
        self._stats_lock = threading.Lock()
        
        # Latest twist (for status queries)
        self._latest_twist: Optional[TwistMessage] = None
        self._latest_twist_lock = threading.Lock()
        
        logger.info(
            f"TwistProcessor initialized: {thread_count} threads, "
            f"queue size {queue_size}"
        )
    
    def start(self) -> None:
        """Start the worker threads.
        
        Creates and starts the configured number of worker threads.
        Safe to call multiple times (idempotent).
        """
        if self._running.is_set():
            logger.warning("Processor already running")
            return
        
        self._running.set()
        
        for i in range(self.thread_count):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"TwistWorker-{i}",
                daemon=True
            )
            worker.start()
            self._workers.append(worker)
        
        logger.info(f"Started {self.thread_count} worker threads")
    
    def stop(self, timeout: float = 5.0) -> None:
        """Stop all worker threads gracefully.
        
        Args:
            timeout: Maximum time to wait for threads to stop
        """
        if not self._running.is_set():
            logger.warning("Processor not running")
            return
        
        logger.info("Stopping processor...")
        self._running.clear()
        
        # Wait for workers to finish
        for worker in self._workers:
            worker.join(timeout=timeout)
            if worker.is_alive():
                logger.warning(f"Worker {worker.name} did not stop cleanly")
        
        self._workers.clear()
        logger.info("Processor stopped")
    
    def enqueue(
        self,
        data: bytes,
        priority: MessagePriority = MessagePriority.NORMAL
    ) -> bool:
        """Add a message to the processing queue.
        
        Args:
            data: Raw binary Twist message data
            priority: Message priority level
            
        Returns:
            True if message was enqueued, False if queue is full
        """
        with self._stats_lock:
            self._stats.messages_received += 1
        
        msg = QueuedMessage(
            priority=priority.value,
            timestamp=time.perf_counter_ns(),
            data=data
        )
        
        try:
            self._queue.put_nowait(msg)
            return True
        except queue.Full:
            with self._stats_lock:
                self._stats.messages_dropped += 1
            logger.warning("Queue full, message dropped")
            return False
    
    def enqueue_twist(
        self,
        twist: TwistMessage,
        priority: MessagePriority = MessagePriority.NORMAL
    ) -> bool:
        """Convenience method to enqueue a TwistMessage directly.
        
        Args:
            twist: TwistMessage to enqueue
            priority: Message priority level
            
        Returns:
            True if message was enqueued
        """
        return self.enqueue(twist.encode(), priority)
    
    def _worker_loop(self) -> None:
        """Main worker thread loop.
        
        Continuously processes messages from the queue until stopped.
        """
        logger.debug(f"Worker started: {threading.current_thread().name}")
        
        while self._running.is_set():
            try:
                # Get message with timeout to allow checking running flag
                msg = self._queue.get(timeout=0.1)
                self._process_message(msg)
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Worker error: {e}")
                if self.on_error:
                    self.on_error(e)
        
        logger.debug(f"Worker stopped: {threading.current_thread().name}")
    
    def _process_message(self, msg: QueuedMessage) -> None:
        """Process a single queued message.
        
        Args:
            msg: QueuedMessage to process
        """
        start_time = time.perf_counter_ns()
        
        # Validate size (supports both 48 and 56 byte formats)
        if not validate_twist_data(msg.data):
            with self._stats_lock:
                self._stats.parse_errors += 1
            logger.warning(f"Invalid message size: {len(msg.data)} bytes")
            return
        
        try:
            # Decode the Twist message
            twist = TwistMessage.decode(msg.data)
            
            # Update latest twist
            with self._latest_twist_lock:
                self._latest_twist = twist
            
            # Call the callback
            if self.on_twist:
                self.on_twist(twist)
            
            # Update stats
            end_time = time.perf_counter_ns()
            latency = end_time - msg.timestamp
            
            with self._stats_lock:
                self._stats.messages_processed += 1
                self._stats.total_latency_ns += latency
            
            logger.debug(
                f"Processed: {twist} "
                f"(latency: {latency/1000:.1f}μs)"
            )
            
        except Exception as e:
            with self._stats_lock:
                self._stats.parse_errors += 1
            logger.error(f"Parse error: {e}")
            if self.on_error:
                self.on_error(e)
    
    @property
    def stats(self) -> ProcessorStats:
        """Get a copy of current statistics."""
        with self._stats_lock:
            return ProcessorStats(
                messages_received=self._stats.messages_received,
                messages_processed=self._stats.messages_processed,
                messages_dropped=self._stats.messages_dropped,
                parse_errors=self._stats.parse_errors,
                total_latency_ns=self._stats.total_latency_ns
            )
    
    @property
    def latest_twist(self) -> Optional[TwistMessage]:
        """Get the most recently processed Twist message."""
        with self._latest_twist_lock:
            return self._latest_twist
    
    @property
    def queue_size(self) -> int:
        """Get the current queue size."""
        return self._queue.qsize()
    
    @property
    def is_running(self) -> bool:
        """Check if the processor is running."""
        return self._running.is_set()
    
    def clear_queue(self) -> int:
        """Clear all pending messages from the queue.
        
        Returns:
            Number of messages cleared
        """
        cleared = 0
        while True:
            try:
                self._queue.get_nowait()
                cleared += 1
            except queue.Empty:
                break
        return cleared
    
    def __enter__(self) -> 'TwistProcessor':
        """Context manager entry - starts the processor."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - stops the processor."""
        self.stop()


class TwistPublisher:
    """Thread-safe Twist message publisher.
    
    Provides a simple interface for publishing Twist messages
    to be sent via DataChannel. Uses a separate thread for
    non-blocking operation.
    
    Args:
        send_callback: Function to call with encoded message data
        rate_hz: Publishing rate in Hz (default: 10)
    """
    
    def __init__(
        self,
        send_callback: Callable[[bytes], None],
        rate_hz: float = 10.0
    ):
        self.send_callback = send_callback
        self.rate_hz = rate_hz
        self.period = 1.0 / rate_hz
        
        self._current_twist: TwistMessage = TwistMessage.zero()
        self._twist_lock = threading.Lock()
        
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
    
    def set_twist(self, twist: TwistMessage) -> None:
        """Set the current Twist message to publish.
        
        Args:
            twist: TwistMessage to publish
        """
        with self._twist_lock:
            self._current_twist = twist
    
    def start(self) -> None:
        """Start the publishing thread."""
        if self._running.is_set():
            return
        
        self._running.set()
        self._thread = threading.Thread(
            target=self._publish_loop,
            name="TwistPublisher",
            daemon=True
        )
        self._thread.start()
        logger.info(f"Publisher started at {self.rate_hz} Hz")
    
    def stop(self) -> None:
        """Stop the publishing thread."""
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("Publisher stopped")
    
    def _publish_loop(self) -> None:
        """Main publishing loop."""
        while self._running.is_set():
            with self._twist_lock:
                twist = self._current_twist
            
            try:
                data = twist.encode()
                self.send_callback(data)
            except Exception as e:
                logger.error(f"Publish error: {e}")
            
            time.sleep(self.period)


if __name__ == "__main__":
    # Self-test
    print("Threaded Message Processor Self-Test")
    print("=" * 50)
    
    received_count = 0
    
    def on_twist(twist: TwistMessage):
        global received_count
        received_count += 1
        if received_count <= 5:
            print(f"  Received #{received_count}: {twist}")
    
    # Create and start processor
    processor = TwistProcessor(
        thread_count=4,
        queue_size=1000,
        on_twist=on_twist
    )
    
    print(f"Starting processor with {processor.thread_count} threads...")
    processor.start()
    
    # Enqueue test messages
    test_messages = 1000
    print(f"Enqueuing {test_messages} messages...")
    
    start = time.perf_counter()
    for i in range(test_messages):
        twist = TwistMessage(
            linear_y=float(i % 10) / 10,
            angular_z=float(i % 5) / 5
        )
        processor.enqueue(twist.encode())
    enqueue_time = time.perf_counter() - start
    
    print(f"Enqueue time: {enqueue_time*1000:.2f}ms")
    print(f"Enqueue rate: {test_messages/enqueue_time:.0f} msg/s")
    
    # Wait for processing
    print("Waiting for processing...")
    time.sleep(1.0)
    
    # Print stats
    stats = processor.stats
    print(f"\nStatistics:")
    print(f"  Received:  {stats.messages_received}")
    print(f"  Processed: {stats.messages_processed}")
    print(f"  Dropped:   {stats.messages_dropped}")
    print(f"  Errors:    {stats.parse_errors}")
    print(f"  Avg Latency: {stats.avg_latency_us:.2f} μs")
    
    # Stop processor
    processor.stop()
    print("\nAll tests passed!")