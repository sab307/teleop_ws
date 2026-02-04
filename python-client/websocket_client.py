"""
WebSocket Client Module
=======================

WebSocket client for data transfer with ping/pong keepalive mechanism.

Features:
    - Separate WebSocket for data transfer (alternative to WebRTC DataChannel)
    - Automatic ping/pong keepalive
    - Reconnection support
    - Binary message support for Twist data

Usage:
    >>> client = WSDataClient("ws://localhost:8080/ws/data")
    >>> await client.connect()
    >>> await client.send_binary(twist_data)
    >>> await client.close()
"""

import asyncio
import json
import logging
import time
from typing import Optional, Callable
from enum import Enum

import aiohttp

logger = logging.getLogger(__name__)


class WSConnectionState(Enum):
    """WebSocket connection states."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


class WSDataClient:
    """WebSocket client for data transfer with ping/pong keepalive."""
    
    def __init__(
        self,
        url: str,
        peer_type: str = "python",
        ping_interval: float = 25.0,  # Send ping every 25 seconds
        reconnect_delay: float = 2.0,
        max_reconnect_attempts: int = 5
    ):
        """
        Initialize WebSocket data client.
        
        Args:
            url: WebSocket server URL (e.g., ws://localhost:8080/ws/data)
            peer_type: Client type ("web" or "python")
            ping_interval: Interval between ping messages (seconds)
            reconnect_delay: Delay before reconnection attempts
            max_reconnect_attempts: Maximum reconnection attempts
        """
        # Add peer type query parameter
        self.url = f"{url}?type={peer_type}" if "?" not in url else f"{url}&type={peer_type}"
        self.peer_type = peer_type
        self.ping_interval = ping_interval
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        
        # Connection state
        self._state = WSConnectionState.DISCONNECTED
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._peer_id: Optional[str] = None
        
        # Tasks
        self._receive_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        
        # Callbacks
        self.on_message: Optional[Callable[[bytes], None]] = None
        self.on_state_change: Optional[Callable[[WSConnectionState], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None
        
        # Statistics
        self._messages_sent = 0
        self._messages_received = 0
        self._bytes_sent = 0
        self._bytes_received = 0
        self._last_pong_time: Optional[float] = None
        self._reconnect_count = 0
        
        # Clock synchronization
        self._clock_offset: float = 0.0      # Estimated clock offset (ms)
        self._clock_offsets: list = []       # History for averaging
        self._clock_sync_in_progress: bool = False
        self._pending_sync_t1: int = 0
        
        logger.info(f"WSDataClient initialized: {self.url}")
    
    @property
    def state(self) -> WSConnectionState:
        return self._state
    
    @property
    def is_connected(self) -> bool:
        return self._state == WSConnectionState.CONNECTED and self._ws is not None
    
    @property
    def peer_id(self) -> Optional[str]:
        return self._peer_id
    
    def _set_state(self, state: WSConnectionState) -> None:
        """Update connection state and notify callback."""
        if self._state != state:
            self._state = state
            logger.info(f"WebSocket state: {state.value}")
            if self.on_state_change:
                try:
                    self.on_state_change(state)
                except Exception as e:
                    logger.error(f"State change callback error: {e}")
    
    async def connect(self) -> bool:
        """
        Connect to the WebSocket server.
        
        Returns:
            True if connection successful
        """
        if self._state == WSConnectionState.CONNECTED:
            return True
        
        self._set_state(WSConnectionState.CONNECTING)
        
        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                self.url,
                heartbeat=self.ping_interval,
                receive_timeout=self.ping_interval * 2
            )
            
            # Wait for welcome message
            msg = await asyncio.wait_for(self._ws.receive(), timeout=5.0)
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "welcome":
                    self._peer_id = data.get("peer_id")
                    logger.info(f"Connected with peer ID: {self._peer_id}")
            
            self._set_state(WSConnectionState.CONNECTED)
            self._reconnect_count = 0
            
            # Start receive and ping tasks
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._ping_task = asyncio.create_task(self._ping_loop())
            
            # Perform initial clock synchronization
            asyncio.create_task(self._perform_initial_clock_sync())
            
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._set_state(WSConnectionState.FAILED)
            if self.on_error:
                self.on_error(e)
            await self._cleanup()
            return False
    
    async def _receive_loop(self) -> None:
        """Receive messages from WebSocket."""
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    # Binary Twist data
                    self._messages_received += 1
                    self._bytes_received += len(msg.data)
                    
                    if self.on_message:
                        try:
                            self.on_message(msg.data)
                        except Exception as e:
                            logger.error(f"Message callback error: {e}")
                
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    # JSON message (ping/pong, status, clock_sync)
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type")
                        
                        if msg_type == "pong":
                            self._last_pong_time = time.time()
                            latency = data.get("timestamp", 0)
                            if latency:
                                latency = time.time() * 1000 - latency
                                logger.debug(f"Pong received, latency: {latency:.0f}ms")
                        
                        elif msg_type == "clock_sync_response":
                            self._handle_clock_sync_response(data)
                        
                        elif msg_type == "twist":
                            # Twist data in JSON format (with base64 data)
                            if "data" in data and self.on_message:
                                import base64
                                binary_data = base64.b64decode(data["data"])
                                self.on_message(binary_data)
                        
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON received")
                
                elif msg.type == aiohttp.WSMsgType.PING:
                    # Respond to ping with pong
                    await self._ws.pong(msg.data)
                
                elif msg.type == aiohttp.WSMsgType.PONG:
                    self._last_pong_time = time.time()
                
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    logger.info("WebSocket closed by server")
                    break
                
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {self._ws.exception()}")
                    break
        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Receive loop error: {e}")
            if self.on_error:
                self.on_error(e)
        
        # Connection lost - attempt reconnect
        if self._state == WSConnectionState.CONNECTED:
            await self._handle_disconnect()
    
    async def _ping_loop(self) -> None:
        """Send periodic ping messages for keepalive."""
        try:
            while self._state == WSConnectionState.CONNECTED:
                await asyncio.sleep(self.ping_interval)
                
                if self._ws and not self._ws.closed:
                    # Send application-level ping
                    ping_msg = json.dumps({
                        "type": "ping",
                        "timestamp": int(time.time() * 1000)
                    })
                    await self._ws.send_str(ping_msg)
                    logger.debug("Ping sent")
        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Ping loop error: {e}")
    
    async def _handle_disconnect(self) -> None:
        """Handle disconnection and attempt reconnect."""
        self._set_state(WSConnectionState.RECONNECTING)
        
        await self._cleanup()
        
        # Attempt reconnection
        for attempt in range(self.max_reconnect_attempts):
            self._reconnect_count += 1
            logger.info(f"Reconnection attempt {attempt + 1}/{self.max_reconnect_attempts}")
            
            await asyncio.sleep(self.reconnect_delay * (attempt + 1))
            
            if await self.connect():
                logger.info("Reconnection successful")
                return
        
        logger.error("Max reconnection attempts reached")
        self._set_state(WSConnectionState.FAILED)
    
    async def send_binary(self, data: bytes) -> bool:
        """
        Send binary data (Twist message).
        
        Args:
            data: Binary data to send
            
        Returns:
            True if sent successfully
        """
        if not self.is_connected or not self._ws:
            return False
        
        try:
            await self._ws.send_bytes(data)
            self._messages_sent += 1
            self._bytes_sent += len(data)
            return True
        except Exception as e:
            logger.error(f"Send error: {e}")
            return False
    
    async def send_json(self, data: dict) -> bool:
        """
        Send JSON message.
        
        Args:
            data: Dictionary to send as JSON
            
        Returns:
            True if sent successfully
        """
        if not self.is_connected or not self._ws:
            return False
        
        try:
            await self._ws.send_str(json.dumps(data))
            return True
        except Exception as e:
            logger.error(f"Send JSON error: {e}")
            return False
    
    async def sync_clock(self) -> bool:
        """
        Initiate clock synchronization with server.
        
        Returns:
            True if sync request sent successfully
        """
        if not self.is_connected or self._clock_sync_in_progress:
            return False
        
        self._clock_sync_in_progress = True
        self._pending_sync_t1 = int(time.time() * 1000)
        
        return await self.send_json({
            'type': 'clock_sync_request',
            't1': self._pending_sync_t1
        })
    
    async def _perform_initial_clock_sync(self) -> None:
        """Perform initial clock synchronization (3 rapid syncs)."""
        logger.info("Starting initial clock synchronization...")
        
        for i in range(3):
            await asyncio.sleep(0.1)
            await self.sync_clock()
            await asyncio.sleep(0.2)
        
        logger.info(f"Initial clock sync complete, offset: {self._clock_offset:.1f}ms")
    
    def _handle_clock_sync_response(self, msg: dict) -> None:
        """
        Handle clock sync response from server.
        
        Args:
            msg: Clock sync response with t1, t2, t3
        """
        t4 = int(time.time() * 1000)
        t1 = msg.get('t1', 0)
        t2 = msg.get('t2', 0)
        t3 = msg.get('t3', 0)
        
        if not all([t1, t2, t3]):
            logger.warning("Invalid clock sync response")
            self._clock_sync_in_progress = False
            return
        
        # Calculate Round Trip Time and Clock Offset
        # RTT = (t4 - t1) - (t3 - t2)  [time spent in transit]
        # Offset = ((t2 - t1) + (t3 - t4)) / 2  [clock difference]
        rtt = (t4 - t1) - (t3 - t2)
        offset = ((t2 - t1) + (t3 - t4)) / 2
        
        # Store offset and compute moving average (last 5 samples)
        self._clock_offsets.append(offset)
        if len(self._clock_offsets) > 5:
            self._clock_offsets.pop(0)
        self._clock_offset = sum(self._clock_offsets) / len(self._clock_offsets)
        
        self._clock_sync_in_progress = False
        
        logger.info(f"ClockSync: RTT={rtt}ms, Offset={self._clock_offset:.1f}ms")
    
    def adjust_timestamp(self, remote_timestamp: int) -> int:
        """
        Adjust a remote timestamp to local time.
        
        Args:
            remote_timestamp: Timestamp from remote peer (ms)
            
        Returns:
            Adjusted timestamp in local time
        """
        return int(remote_timestamp - self._clock_offset)
    
    @property
    def clock_offset(self) -> float:
        """Get current clock offset in milliseconds."""
        return self._clock_offset
    
    async def _cleanup(self) -> None:
        """Clean up connection resources."""
        # Cancel tasks
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None
        
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        
        # Close WebSocket
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        
        # Close session
        if self._session:
            await self._session.close()
        self._session = None
    
    async def close(self) -> None:
        """Close the WebSocket connection."""
        logger.info("Closing WebSocket connection")
        self._set_state(WSConnectionState.DISCONNECTED)
        await self._cleanup()
    
    @property
    def stats(self) -> dict:
        """Get connection statistics."""
        return {
            "messages_sent": self._messages_sent,
            "messages_received": self._messages_received,
            "bytes_sent": self._bytes_sent,
            "bytes_received": self._bytes_received,
            "reconnect_count": self._reconnect_count,
            "last_pong": self._last_pong_time
        }


class WSSignalingClient:
    """WebSocket client for WebRTC signaling with ping/pong keepalive."""
    
    def __init__(
        self,
        url: str,
        peer_type: str = "python",
        ping_interval: float = 25.0
    ):
        """
        Initialize WebSocket signaling client.
        
        Args:
            url: WebSocket server URL (e.g., ws://localhost:8080/ws/signaling)
            peer_type: Client type ("web" or "python")
            ping_interval: Interval between ping messages
        """
        self.url = f"{url}?type={peer_type}" if "?" not in url else f"{url}&type={peer_type}"
        self.peer_type = peer_type
        self.ping_interval = ping_interval
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._peer_id: Optional[str] = None
        self._connected = False
        
        # Callbacks
        self.on_offer: Optional[Callable[[dict], None]] = None
        self.on_answer: Optional[Callable[[dict], None]] = None
        self.on_ice: Optional[Callable[[dict], None]] = None
        
        logger.info(f"WSSignalingClient initialized: {self.url}")
    
    async def connect(self) -> bool:
        """Connect to signaling server."""
        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                self.url,
                heartbeat=self.ping_interval
            )
            
            # Wait for welcome
            msg = await asyncio.wait_for(self._ws.receive(), timeout=5.0)
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "welcome":
                    self._peer_id = data.get("peer_id")
                    logger.info(f"Signaling connected, peer ID: {self._peer_id}")
            
            self._connected = True
            asyncio.create_task(self._receive_loop())
            return True
            
        except Exception as e:
            logger.error(f"Signaling connection failed: {e}")
            return False
    
    async def _receive_loop(self) -> None:
        """Handle incoming signaling messages."""
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")
                    
                    if msg_type == "offer" and self.on_offer:
                        self.on_offer(data)
                    elif msg_type == "answer" and self.on_answer:
                        self.on_answer(data)
                    elif msg_type == "ice" and self.on_ice:
                        self.on_ice(data)
                    elif msg_type == "pong":
                        logger.debug("Signaling pong received")
                
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    break
        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Signaling receive error: {e}")
        
        self._connected = False
    
    async def send_offer(self, sdp: str) -> bool:
        """Send SDP offer."""
        return await self._send({"type": "offer", "sdp": sdp})
    
    async def send_answer(self, sdp: str) -> bool:
        """Send SDP answer."""
        return await self._send({"type": "answer", "sdp": sdp})
    
    async def send_ice(self, candidate: dict) -> bool:
        """Send ICE candidate."""
        return await self._send({"type": "ice", "candidate": candidate})
    
    async def _send(self, data: dict) -> bool:
        """Send JSON message."""
        if not self._connected or not self._ws:
            return False
        try:
            await self._ws.send_str(json.dumps(data))
            return True
        except Exception as e:
            logger.error(f"Signaling send error: {e}")
            return False
    
    async def close(self) -> None:
        """Close signaling connection."""
        self._connected = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()


if __name__ == "__main__":
    # Self-test
    import sys
    
    logging.basicConfig(level=logging.DEBUG)
    
    async def test():
        url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8080/ws/data"
        
        client = WSDataClient(url, peer_type="python")
        
        def on_message(data):
            print(f"Received: {len(data)} bytes")
        
        def on_state(state):
            print(f"State: {state.value}")
        
        client.on_message = on_message
        client.on_state_change = on_state
        
        if await client.connect():
            print("Connected! Press Ctrl+C to exit")
            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                pass
        
        await client.close()
        print("Done")
    
    asyncio.run(test())