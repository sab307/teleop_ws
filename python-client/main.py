#!/usr/bin/env python3
"""
WebRTC/WebSocket Twist Client - Main Entry Point
=================================================

Client for receiving ROS2 Twist messages via WebRTC DataChannel or WebSocket
and optionally publishing them to a ROS2 topic.

Features:
    - WebRTC DataChannel OR WebSocket communication (selectable)
    - Threaded message processing
    - Configurable ROS2 topic name
    - Latency tracking and display
    - Emergency stop handling
    - Ping/pong keepalive for WebSocket

Usage:
    python main.py [options]

Examples:
    # WebRTC mode (default)
    python main.py
    
    # WebSocket mode
    python main.py --websocket
    
    # With ROS2 topic
    python main.py --topic /turtle1/cmd_vel
    
    # Full example with WebSocket
    python main.py --websocket --relay-url ws://localhost:8080 --topic /cmd_vel
"""

import asyncio
import argparse
import logging
import signal
import sys
import time
from typing import Optional, Callable
from collections import deque

from twist_protocol import TwistMessage, current_time_ms
from threaded_processor import TwistProcessor, MessagePriority
from webrtc_client import WebRTCClient, ConnectionState, ClientConfig
from websocket_client import WSDataClient, WSConnectionState


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TwistClient")


# Try to import ROS2 (optional)
ROS2_AVAILABLE = False
try:
    import rclpy
    from rclpy.node import Node
    from geometry_msgs.msg import Twist
    ROS2_AVAILABLE = True
    logger.info("ROS2 (rclpy) available")
except ImportError:
    logger.info("ROS2 (rclpy) not available - running without ROS2 publishing")


class LatencyTracker:
    """Tracks message latency statistics."""
    
    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self._samples: deque = deque(maxlen=window_size)
        self._total_count = 0
    
    def add_sample(self, latency_ms: float) -> None:
        """Add a latency sample."""
        if latency_ms >= 0:
            self._samples.append(latency_ms)
            self._total_count += 1
    
    @property
    def average(self) -> float:
        """Get average latency in ms."""
        if not self._samples:
            return 0.0
        return sum(self._samples) / len(self._samples)
    
    @property
    def min(self) -> float:
        """Get minimum latency in ms."""
        return min(self._samples) if self._samples else 0.0
    
    @property
    def max(self) -> float:
        """Get maximum latency in ms."""
        return max(self._samples) if self._samples else 0.0
    
    @property
    def count(self) -> int:
        """Get total sample count."""
        return self._total_count
    
    def __str__(self) -> str:
        if not self._samples:
            return "No samples"
        return f"avg={self.average:.1f}ms, min={self.min:.1f}ms, max={self.max:.1f}ms"


class ROS2Publisher:
    """Optional ROS2 Twist publisher."""
    
    def __init__(self, topic_name: str):
        self.topic_name = topic_name
        self._node = None
        self._publisher = None
        self._initialized = False
        
    def initialize(self) -> bool:
        """Initialize ROS2 node and publisher."""
        if not ROS2_AVAILABLE:
            logger.warning("ROS2 not available, cannot initialize publisher")
            return False
        
        try:
            if not rclpy.ok():
                rclpy.init()
            
            self._node = rclpy.create_node('webrtc_twist_bridge')
            self._publisher = self._node.create_publisher(Twist, self.topic_name, 10)
            self._initialized = True
            
            logger.info(f"ROS2 publisher initialized on topic: {self.topic_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize ROS2: {e}")
            return False
    
    def publish(self, twist_msg: TwistMessage) -> bool:
        """Publish a TwistMessage to ROS2."""
        if not self._initialized or not self._publisher:
            return False
        
        try:
            ros_twist = Twist()
            ros_twist.linear.x = twist_msg.linear.x
            ros_twist.linear.y = twist_msg.linear.y
            ros_twist.linear.z = twist_msg.linear.z
            ros_twist.angular.x = twist_msg.angular.x
            ros_twist.angular.y = twist_msg.angular.y
            ros_twist.angular.z = twist_msg.angular.z
            
            self._publisher.publish(ros_twist)
            return True
            
        except Exception as e:
            logger.error(f"Failed to publish: {e}")
            return False
    
    def shutdown(self) -> None:
        """Shutdown ROS2 node."""
        if self._node:
            self._node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        self._initialized = False


class TwistClient:
    """Integrated WebRTC/WebSocket Twist message client with optional ROS2 publishing."""
    
    def __init__(
        self,
        relay_url: str = "http://localhost:8080",
        thread_count: int = 2,
        topic_name: Optional[str] = None,
        use_websocket: bool = False,
        on_twist: Optional[Callable[[TwistMessage], None]] = None
    ):
        self.relay_url = relay_url
        self.thread_count = thread_count
        self.topic_name = topic_name
        self.use_websocket = use_websocket
        self._user_callback = on_twist
        
        # Latency tracking
        self.latency_tracker = LatencyTracker()
        
        # ROS2 publisher (optional)
        self._ros2_publisher: Optional[ROS2Publisher] = None
        if topic_name:
            self._ros2_publisher = ROS2Publisher(topic_name)
        
        # Communication client (WebRTC or WebSocket)
        self._webrtc: Optional[WebRTCClient] = None
        self._websocket: Optional[WSDataClient] = None
        
        if use_websocket:
            # Convert HTTP URL to WebSocket URL
            ws_url = relay_url.replace("http://", "ws://").replace("https://", "wss://")
            if not ws_url.endswith("/ws/data"):
                ws_url = ws_url.rstrip("/") + "/ws/data"
            self._websocket = WSDataClient(ws_url, peer_type="python")
        else:
            config = ClientConfig(relay_url=relay_url, peer_type="python")
            self._webrtc = WebRTCClient(config=config)
        
        # Threaded processor
        self._processor = TwistProcessor(
            thread_count=thread_count,
            on_twist=self._handle_twist,
            on_error=self._handle_error
        )
        
        # State
        self._running = False
        self._message_count = 0
        self._last_twist: Optional[TwistMessage] = None
        self._start_time: Optional[float] = None
        
        # Setup callbacks
        if self._webrtc:
            self._webrtc.on_message = self._on_message
            self._webrtc.on_state_change = self._on_webrtc_state_change
            self._webrtc.on_error = self._handle_error
        
        if self._websocket:
            self._websocket.on_message = self._on_message
            self._websocket.on_state_change = self._on_ws_state_change
            self._websocket.on_error = self._handle_error
        
        mode = "WebSocket" if use_websocket else "WebRTC"
        logger.info(f"TwistClient initialized ({mode} mode):")
        logger.info(f"  Relay URL: {relay_url}")
        logger.info(f"  Threads: {thread_count}")
        logger.info(f"  ROS2 Topic: {topic_name or 'disabled'}")
    
    def _handle_twist(self, twist: TwistMessage) -> None:
        """Handle processed Twist message."""
        self._message_count += 1
        self._last_twist = twist
        
        # Track latency with clock skew adjustment
        latency = -1
        if twist.timestamp > 0:
            # Adjust for clock skew if using WebSocket
            if self._websocket:
                adjusted_ts = self._websocket.adjust_timestamp(twist.timestamp)
                latency = current_time_ms() - adjusted_ts
            else:
                latency = twist.get_latency_ms()
            
            # Only track valid latency measurements (>= 1ms)
            if latency >= 1:
                self.latency_tracker.add_sample(latency)
        
        # Log significant movements
        if not twist.is_zero():
            latency_str = f", latency={latency:.0f}ms" if latency >= 1 else ""
            logger.info(f"Twist: linear.y={twist.linear.y:.2f}, angular.z={twist.angular.z:.2f}{latency_str}")
        else:
            logger.info("Twist: STOP command received")
        
        # Publish to ROS2 if configured
        if self._ros2_publisher:
            self._ros2_publisher.publish(twist)
        
        # Call user callback
        if self._user_callback:
            try:
                self._user_callback(twist)
            except Exception as e:
                logger.error(f"User callback error: {e}")
    
    def _handle_error(self, error: Exception) -> None:
        """Handle errors from components."""
        logger.error(f"Error: {error}")
    
    def _on_message(self, data: bytes) -> None:
        """Handle incoming message (from WebRTC or WebSocket)."""
        # Determine priority
        try:
            twist = TwistMessage.decode(data)
            priority = MessagePriority.HIGH if twist.is_zero() else MessagePriority.NORMAL
        except Exception:
            priority = MessagePriority.NORMAL
        
        self._processor.enqueue(data, priority)
    
    def _on_webrtc_state_change(self, state: ConnectionState) -> None:
        """Handle WebRTC connection state changes."""
        logger.info(f"WebRTC state: {state.value}")
        
        if state == ConnectionState.CONNECTED:
            logger.info("✓ WebRTC connected and ready")
        elif state == ConnectionState.FAILED:
            logger.warning("WebRTC connection failed")
    
    def _on_ws_state_change(self, state: WSConnectionState) -> None:
        """Handle WebSocket connection state changes."""
        logger.info(f"WebSocket state: {state.value}")
        
        if state == WSConnectionState.CONNECTED:
            logger.info("✓ WebSocket connected and ready")
        elif state == WSConnectionState.FAILED:
            logger.warning("WebSocket connection failed")
        elif state == WSConnectionState.RECONNECTING:
            logger.info("WebSocket reconnecting...")
    
    async def start(self) -> bool:
        """Start the client."""
        if self._running:
            return True
        
        logger.info("Starting Twist client...")
        self._start_time = time.time()
        
        # Initialize ROS2 if configured
        if self._ros2_publisher:
            if not self._ros2_publisher.initialize():
                logger.warning("ROS2 initialization failed, continuing without ROS2")
                self._ros2_publisher = None
        
        # Start threaded processor
        self._processor.start()
        
        # Connect
        logger.info(f"Connecting to {self.relay_url}...")
        
        if self._websocket:
            success = await self._websocket.connect()
        else:
            success = await self._webrtc.connect()
        
        if success:
            self._running = True
            logger.info("✓ Client started successfully")
            return True
        else:
            logger.error("✗ Failed to connect to relay")
            self._processor.stop()
            return False
    
    async def stop(self) -> None:
        """Stop the client."""
        if not self._running:
            return
        
        logger.info("Stopping Twist client...")
        self._running = False
        
        # Stop processor
        self._processor.stop()
        
        # Close connection
        if self._websocket:
            await self._websocket.close()
        if self._webrtc:
            await self._webrtc.close()
        
        # Shutdown ROS2
        if self._ros2_publisher:
            self._ros2_publisher.shutdown()
        
        # Print final stats
        self._print_stats()
        logger.info("Client stopped")
    
    async def send_twist(self, twist: TwistMessage) -> bool:
        """Send a Twist message to the relay."""
        if not self._running:
            return False
        
        data = twist.encode()
        if self._websocket:
            return await self._websocket.send_binary(data)
        else:
            return await self._webrtc.send(data)
    
    async def run_forever(self) -> None:
        """Run until interrupted."""
        try:
            while self._running:
                await asyncio.sleep(5.0)
                self._print_stats()
        except asyncio.CancelledError:
            pass
    
    def _print_stats(self) -> None:
        """Print current statistics."""
        stats = self._processor.stats
        uptime = time.time() - self._start_time if self._start_time else 0
        
        logger.info(
            f"Stats: msgs={stats.messages_processed}, "
            f"latency={self.latency_tracker}, "
            f"uptime={uptime:.0f}s"
        )
    
    @property
    def is_connected(self) -> bool:
        if self._websocket:
            return self._websocket.is_connected
        return self._webrtc.is_connected if self._webrtc else False


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="WebRTC/WebSocket Twist Message Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # WebRTC mode (default)
    python main.py
    python main.py --relay-url http://192.168.1.100:8080
    
    # WebSocket mode
    python main.py --websocket
    python main.py --websocket --relay-url http://localhost:8080
    
    # With ROS2 topic
    python main.py --topic /cmd_vel
    python main.py --websocket --topic /turtle1/cmd_vel --threads 4
        """
    )
    
    parser.add_argument(
        "--relay-url", "-r",
        default="http://localhost:8080",
        help="Relay server URL (default: http://localhost:8080)"
    )
    
    parser.add_argument(
        "--websocket", "-w",
        action="store_true",
        help="Use WebSocket instead of WebRTC DataChannel"
    )
    
    parser.add_argument(
        "--topic", "-t",
        default=None,
        help="ROS2 topic name to publish Twist messages (e.g., /cmd_vel, /turtle1/cmd_vel)"
    )
    parser.add_argument(
        "--threads", "-n",
        type=int,
        default=2,
        help="Number of processing threads (default: 2)"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    
    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Banner
    mode = "WebSocket" if args.websocket else "WebRTC"
    print(f"""
╔═══════════════════════════════════════════════════════════╗
║     Twist Client ({mode} mode)                          ║
║     ROS2 Robot Teleoperation                              ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    print(f"Configuration:")
    print(f"  Mode:       {mode}")
    print(f"  Relay URL:  {args.relay_url}")
    print(f"  ROS2 Topic: {args.topic or 'disabled'}")
    print(f"  Threads:    {args.threads}")
    print()
    
    if args.topic and not ROS2_AVAILABLE:
        print("WARNING: --topic specified but rclpy not available!")
        print("Install ROS2 and source setup.bash to enable publishing.")
        print()
    
    # Create client
    client = TwistClient(
        relay_url=args.relay_url,
        thread_count=args.threads,
        topic_name=args.topic,
        use_websocket=args.websocket
    )
    
    # Setup shutdown handlers
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()
    
    def signal_handler():
        logger.info("\nShutdown signal received...")
        shutdown_event.set()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        success = await client.start()
        
        if not success:
            logger.error("Failed to start client")
            return 1
        
        print(f"\nClient running ({mode} mode). Press Ctrl+C to stop.\n")
        
        if args.topic:
            print(f"Publishing Twist messages to: {args.topic}")
        else:
            print("Receiving Twist messages (no ROS2 publishing)")
        
        print("\nWaiting for commands from web client...\n")
        
        # Run until shutdown
        await shutdown_event.wait()
        
    finally:
        await client.stop()
    
    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(0)