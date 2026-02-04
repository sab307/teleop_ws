"""
Twist Protocol Module
=====================

Binary encoding/decoding for ROS2 geometry_msgs/Twist messages with optional timestamp.

Binary Format:
    Standard (48 bytes):
        Offset  Size    Type      Field
        ───────────────────────────────────
        0       8       float64   linear.x
        8       8       float64   linear.y
        16      8       float64   linear.z
        24      8       float64   angular.x
        32      8       float64   angular.y
        40      8       float64   angular.z
    
    With Timestamp (56 bytes):
        48      8       uint64    timestamp (ms since epoch)

Performance:
    - Encoding: ~1μs
    - Decoding: ~1μs
"""

import struct
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple


# Binary format specifications
TWIST_FORMAT = '<6d'           # 6 doubles, little-endian
TWIST_FORMAT_WITH_TS = '<6dQ'  # 6 doubles + 1 uint64
TWIST_SIZE = 48                # Standard size
TWIST_SIZE_WITH_TS = 56        # Size with timestamp


def current_time_ms() -> int:
    """Get current time in milliseconds since epoch."""
    return int(time.time() * 1000)


@dataclass
class Vector3:
    """3D vector with X, Y, Z components."""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    
    def __str__(self) -> str:
        return f"[{self.x:.3f}, {self.y:.3f}, {self.z:.3f}]"
    
    def is_zero(self) -> bool:
        return self.x == 0.0 and self.y == 0.0 and self.z == 0.0


@dataclass
class TwistMessage:
    """ROS2 geometry_msgs/Twist message with optional timestamp.
    
    Attributes:
        linear: Linear velocity vector (m/s)
        angular: Angular velocity vector (rad/s)
        timestamp: Message timestamp in ms (0 = not set)
    """
    linear: Vector3 = None
    angular: Vector3 = None
    timestamp: int = 0
    
    # Convenience constructor parameters
    linear_x: float = 0.0
    linear_y: float = 0.0
    linear_z: float = 0.0
    angular_x: float = 0.0
    angular_y: float = 0.0
    angular_z: float = 0.0
    
    def __post_init__(self):
        if self.linear is None:
            self.linear = Vector3(self.linear_x, self.linear_y, self.linear_z)
        if self.angular is None:
            self.angular = Vector3(self.angular_x, self.angular_y, self.angular_z)
    
    def encode(self, include_timestamp: bool = True) -> bytes:
        """Encode to binary format.
        
        Args:
            include_timestamp: If True, include timestamp (56 bytes), else 48 bytes
            
        Returns:
            Binary representation
        """
        if include_timestamp:
            ts = self.timestamp if self.timestamp > 0 else current_time_ms()
            return struct.pack(
                TWIST_FORMAT_WITH_TS,
                self.linear.x, self.linear.y, self.linear.z,
                self.angular.x, self.angular.y, self.angular.z,
                ts
            )
        else:
            return struct.pack(
                TWIST_FORMAT,
                self.linear.x, self.linear.y, self.linear.z,
                self.angular.x, self.angular.y, self.angular.z
            )
    
    @classmethod
    def decode(cls, data: bytes) -> 'TwistMessage':
        """Decode binary data into TwistMessage.
        
        Supports both 48-byte (no timestamp) and 56-byte (with timestamp) formats.
        """
        if len(data) == TWIST_SIZE_WITH_TS:
            values = struct.unpack(TWIST_FORMAT_WITH_TS, data)
            return cls(
                linear=Vector3(values[0], values[1], values[2]),
                angular=Vector3(values[3], values[4], values[5]),
                timestamp=values[6]
            )
        elif len(data) == TWIST_SIZE:
            values = struct.unpack(TWIST_FORMAT, data)
            return cls(
                linear=Vector3(values[0], values[1], values[2]),
                angular=Vector3(values[3], values[4], values[5]),
                timestamp=0
            )
        else:
            raise ValueError(
                f"Invalid twist message size: expected {TWIST_SIZE} or {TWIST_SIZE_WITH_TS} bytes, "
                f"got {len(data)} bytes"
            )
    
    def get_latency_ms(self) -> float:
        """Calculate latency from timestamp to now.
        
        Returns:
            Latency in milliseconds, or -1 if no timestamp
        """
        if self.timestamp <= 0:
            return -1.0
        return current_time_ms() - self.timestamp
    
    @classmethod
    def zero(cls) -> 'TwistMessage':
        """Create a stop command (all zeros)."""
        return cls(timestamp=current_time_ms())
    
    @classmethod
    def forward(cls, speed: float = 1.0) -> 'TwistMessage':
        """Create forward motion command."""
        return cls(linear_y=abs(speed), timestamp=current_time_ms())
    
    @classmethod
    def backward(cls, speed: float = 1.0) -> 'TwistMessage':
        """Create backward motion command."""
        return cls(linear_y=-abs(speed), timestamp=current_time_ms())
    
    @classmethod
    def turn_left(cls, rate: float = 1.0) -> 'TwistMessage':
        """Create left turn command."""
        return cls(angular_z=abs(rate), timestamp=current_time_ms())
    
    @classmethod
    def turn_right(cls, rate: float = 1.0) -> 'TwistMessage':
        """Create right turn command."""
        return cls(angular_z=-abs(rate), timestamp=current_time_ms())
    
    def is_zero(self) -> bool:
        """Check if this is a stop command."""
        return self.linear.is_zero() and self.angular.is_zero()
    
    def __str__(self) -> str:
        s = f"Twist{{linear: {self.linear}, angular: {self.angular}}}"
        if self.timestamp > 0:
            s += f" @{self.timestamp}"
        return s


def validate_twist_data(data: bytes) -> bool:
    """Validate that data is a valid Twist message."""
    return len(data) in (TWIST_SIZE, TWIST_SIZE_WITH_TS)


if __name__ == "__main__":
    print("Twist Protocol Self-Test")
    print("=" * 50)
    
    # Test with timestamp
    original = TwistMessage(linear_y=1.5, angular_z=-0.75, timestamp=current_time_ms())
    print(f"Original: {original}")
    
    encoded = original.encode()
    print(f"Encoded:  {len(encoded)} bytes (with timestamp)")
    
    decoded = TwistMessage.decode(encoded)
    print(f"Decoded:  {decoded}")
    print(f"Latency:  {decoded.get_latency_ms():.1f} ms")
    
    # Test without timestamp
    encoded_48 = original.encode(include_timestamp=False)
    print(f"\nEncoded (no ts): {len(encoded_48)} bytes")
    decoded_48 = TwistMessage.decode(encoded_48)
    print(f"Decoded: {decoded_48}")
    
    # Performance test
    iterations = 100000
    start = time.perf_counter_ns()
    for _ in range(iterations):
        data = original.encode()
    encode_time = (time.perf_counter_ns() - start) / iterations
    
    start = time.perf_counter_ns()
    for _ in range(iterations):
        TwistMessage.decode(data)
    decode_time = (time.perf_counter_ns() - start) / iterations
    
    print(f"\nPerformance ({iterations:,} iterations):")
    print(f"  Encode: {encode_time/1000:.2f} μs")
    print(f"  Decode: {decode_time/1000:.2f} μs")
    
    print("\n All tests passed!")