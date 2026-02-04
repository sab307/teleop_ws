"""
WebRTC Client Module
====================

Async WebRTC client for connecting to the Go relay server.

Features:
    - Automatic connection and reconnection
    - DataChannel message handling
    - Binary Twist message support
    - Connection state monitoring
    - Graceful shutdown

Architecture:
    ┌─────────────────┐     HTTP/Signaling    ┌─────────────────┐
    │  Python Client  │◄────────────────────►│  Go Relay       │
    │                 │                       │  Server         │
    │  ┌───────────┐  │     WebRTC/DTLS      │                 │
    │  │DataChannel│◄─┼──────────────────────┼─►               │
    │  └───────────┘  │                       │                 │
    └─────────────────┘                       └─────────────────┘

Example Usage:
    >>> async def main():
    ...     client = WebRTCClient("http://localhost:8080")
    ...     client.on_message = lambda data: print(f"Received: {len(data)} bytes")
    ...     await client.connect()
    ...     await client.send(twist.encode())
    ...     await client.close()
"""

import asyncio
import logging
import json
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass
from enum import Enum

import aiohttp
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCDataChannel,
    RTCConfiguration,
    RTCIceServer
)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    """WebRTC connection states."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass
class ClientConfig:
    """Configuration for WebRTC client.
    
    Attributes:
        relay_url: URL of the Go relay server
        stun_servers: List of STUN server URLs
        datachannel_label: Label for the data channel
        peer_type: Client type identifier ("python")
        connect_timeout: Connection timeout in seconds
        reconnect_attempts: Number of reconnection attempts
    """
    relay_url: str = "http://localhost:8080"
    stun_servers: list[str] = None
    datachannel_label: str = "twist"
    peer_type: str = "python"
    connect_timeout: float = 30.0
    reconnect_attempts: int = 3
    
    def __post_init__(self):
        if self.stun_servers is None:
            self.stun_servers = ["stun:stun.l.google.com:19302"]


class WebRTCClient:
    """Async WebRTC client for DataChannel communication.
    
    Connects to the Go relay server via WebRTC and establishes
    a DataChannel for binary Twist message transmission.
    
    Args:
        relay_url: URL of the Go relay server
        config: Optional ClientConfig for advanced settings
    
    Callbacks:
        on_message: Called with raw bytes when a message is received
        on_state_change: Called when connection state changes
        on_error: Called when an error occurs
    
    Example:
        >>> client = WebRTCClient("http://localhost:8080")
        >>> 
        >>> @client.on_message
        >>> def handle_message(data: bytes):
        ...     twist = TwistMessage.decode(data)
        ...     print(f"Received: {twist}")
        >>> 
        >>> await client.connect()
    """
    
    def __init__(
        self,
        relay_url: str = "http://localhost:8080",
        config: Optional[ClientConfig] = None
    ):
        self.config = config or ClientConfig(relay_url=relay_url)
        
        # Connection state
        self._state = ConnectionState.DISCONNECTED
        self._peer_id: Optional[str] = None
        
        # WebRTC components
        self._pc: Optional[RTCPeerConnection] = None
        self._dc: Optional[RTCDataChannel] = None
        
        # Callbacks
        self.on_message: Optional[Callable[[bytes], None]] = None
        self.on_state_change: Optional[Callable[[ConnectionState], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None
        
        # HTTP session for signaling
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Synchronization
        self._connected_event = asyncio.Event()
        self._closed = False
        
        logger.info(f"WebRTCClient created for {self.config.relay_url}")
    
    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        return self._state
    
    @property
    def peer_id(self) -> Optional[str]:
        """Get assigned peer ID from server."""
        return self._peer_id
    
    @property
    def is_connected(self) -> bool:
        """Check if DataChannel is open and ready."""
        return (
            self._state == ConnectionState.CONNECTED and
            self._dc is not None and
            self._dc.readyState == "open"
        )
    
    def _set_state(self, state: ConnectionState) -> None:
        """Update connection state and notify callback."""
        if self._state != state:
            old_state = self._state
            self._state = state
            logger.info(f"State changed: {old_state.value} -> {state.value}")
            
            if self.on_state_change:
                self.on_state_change(state)
            
            if state == ConnectionState.CONNECTED:
                self._connected_event.set()
            else:
                self._connected_event.clear()
    
    async def connect(self) -> bool:
        """Establish WebRTC connection to the relay server.
        
        Returns:
            True if connection succeeded, False otherwise
            
        Raises:
            ConnectionError: If connection fails after all retry attempts
        """
        if self._closed:
            raise RuntimeError("Client has been closed")
        
        self._set_state(ConnectionState.CONNECTING)
        
        # Create HTTP session
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        # Configure ICE servers
        ice_servers = [
            RTCIceServer(urls=url) for url in self.config.stun_servers
        ]
        rtc_config = RTCConfiguration(iceServers=ice_servers)
        
        # Create peer connection
        self._pc = RTCPeerConnection(configuration=rtc_config)
        self._setup_peer_connection()
        
        # Create data channel
        self._dc = self._pc.createDataChannel(
            self.config.datachannel_label,
            ordered=True
        )
        self._setup_data_channel()
        
        try:
            # Create and send offer
            offer = await self._pc.createOffer()
            await self._pc.setLocalDescription(offer)
            
            # Wait for ICE gathering to complete
            await self._wait_for_ice_gathering()
            
            # Send offer to signaling server
            answer = await self._send_offer()
            
            if answer is None:
                raise ConnectionError("Failed to get answer from server")
            
            # Set remote description
            await self._pc.setRemoteDescription(answer)
            
            # Wait for connection
            try:
                await asyncio.wait_for(
                    self._connected_event.wait(),
                    timeout=self.config.connect_timeout
                )
                return True
            except asyncio.TimeoutError:
                raise ConnectionError("Connection timeout")
                
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._set_state(ConnectionState.FAILED)
            if self.on_error:
                self.on_error(e)
            return False
    
    def _setup_peer_connection(self) -> None:
        """Configure peer connection event handlers."""
        
        @self._pc.on("connectionstatechange")
        async def on_connection_state_change():
            state = self._pc.connectionState
            logger.debug(f"Connection state: {state}")
            
            if state == "connected":
                pass  # DataChannel open will set connected
            elif state == "failed":
                self._set_state(ConnectionState.FAILED)
            elif state == "closed":
                self._set_state(ConnectionState.CLOSED)
        
        @self._pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            logger.debug(f"ICE state: {self._pc.iceConnectionState}")
        
        @self._pc.on("datachannel")
        def on_datachannel(channel: RTCDataChannel):
            logger.info(f"DataChannel received: {channel.label}")
            self._dc = channel
            self._setup_data_channel()
    
    def _setup_data_channel(self) -> None:
        """Configure data channel event handlers."""
        
        @self._dc.on("open")
        def on_open():
            logger.info(f"DataChannel '{self._dc.label}' opened")
            self._set_state(ConnectionState.CONNECTED)
        
        @self._dc.on("close")
        def on_close():
            logger.info(f"DataChannel '{self._dc.label}' closed")
            if self._state == ConnectionState.CONNECTED:
                self._set_state(ConnectionState.DISCONNECTED)
        
        @self._dc.on("error")
        def on_error(error):
            logger.error(f"DataChannel error: {error}")
            if self.on_error:
                self.on_error(Exception(str(error)))
        
        @self._dc.on("message")
        def on_message(message):
            # Handle both string and bytes messages
            if isinstance(message, str):
                data = message.encode()
            else:
                data = message
            
            logger.debug(f"Received message: {len(data)} bytes")
            
            if self.on_message:
                self.on_message(data)
    
    async def _wait_for_ice_gathering(self) -> None:
        """Wait for ICE gathering to complete."""
        if self._pc.iceGatheringState == "complete":
            return
        
        gathering_complete = asyncio.Event()
        
        @self._pc.on("icegatheringstatechange")
        def check_state():
            if self._pc.iceGatheringState == "complete":
                gathering_complete.set()
        
        # Check immediately in case already complete
        check_state()
        
        await asyncio.wait_for(gathering_complete.wait(), timeout=10.0)
    
    async def _send_offer(self) -> Optional[RTCSessionDescription]:
        """Send SDP offer to signaling server and get answer.
        
        Returns:
            RTCSessionDescription answer or None on failure
        """
        url = f"{self.config.relay_url}/offer"
        
        payload = {
            "sdp": self._pc.localDescription.sdp,
            "type": "offer",
            "peerType": self.config.peer_type
        }
        
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Signaling error: {resp.status} - {error_text}")
                    return None
                
                data = await resp.json()
                
                self._peer_id = data.get("peerID")
                logger.info(f"Assigned peer ID: {self._peer_id}")
                
                return RTCSessionDescription(
                    sdp=data["sdp"],
                    type=data["type"]
                )
                
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error: {e}")
            return None
    
    async def send(self, data: bytes) -> bool:
        """Send binary data through the DataChannel.
        
        Args:
            data: Binary data to send
            
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.is_connected:
            logger.warning("Cannot send: not connected")
            return False
        
        try:
            self._dc.send(data)
            logger.debug(f"Sent {len(data)} bytes")
            return True
        except Exception as e:
            logger.error(f"Send error: {e}")
            if self.on_error:
                self.on_error(e)
            return False
    
    async def send_text(self, text: str) -> bool:
        """Send text data through the DataChannel.
        
        Args:
            text: Text string to send
            
        Returns:
            True if sent successfully
        """
        return await self.send(text.encode())
    
    async def wait_connected(self, timeout: float = 30.0) -> bool:
        """Wait for the connection to be established.
        
        Args:
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if connected within timeout
        """
        try:
            await asyncio.wait_for(
                self._connected_event.wait(),
                timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            return False
    
    async def close(self) -> None:
        """Close the WebRTC connection and cleanup resources."""
        if self._closed:
            return
        
        self._closed = True
        logger.info("Closing WebRTC client...")
        
        # Close data channel
        if self._dc:
            self._dc.close()
        
        # Close peer connection
        if self._pc:
            await self._pc.close()
        
        # Close HTTP session
        if self._session:
            await self._session.close()
        
        self._set_state(ConnectionState.CLOSED)
        logger.info("WebRTC client closed")
    
    async def __aenter__(self) -> 'WebRTCClient':
        """Async context manager entry."""
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()


async def _test_client():
    """Self-test function."""
    print("WebRTC Client Self-Test")
    print("=" * 50)
    print("Note: Requires Go relay server running on localhost:8080")
    print()
    
    client = WebRTCClient("http://localhost:8080")
    
    messages_received = []
    
    def on_message(data: bytes):
        messages_received.append(data)
        print(f"  Received: {len(data)} bytes")
    
    def on_state_change(state: ConnectionState):
        print(f"  State: {state.value}")
    
    client.on_message = on_message
    client.on_state_change = on_state_change
    
    try:
        print("Connecting...")
        success = await client.connect()
        
        if success:
            print(f"✓ Connected! Peer ID: {client.peer_id}")
            
            # Send a test message
            from twist_protocol import TwistMessage
            twist = TwistMessage(linear_y=1.0, angular_z=0.5)
            await client.send(twist.encode())
            print(f"  Sent: {twist}")
            
            # Wait a bit for any responses
            await asyncio.sleep(2.0)
            
        else:
            print("Connection failed")
            
    except Exception as e:
        print(f"Error: {e}")
        
    finally:
        await client.close()
        print("Test complete")


if __name__ == "__main__":
    asyncio.run(_test_client())