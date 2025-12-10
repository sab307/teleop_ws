// Package twist provides binary encoding/decoding for ROS2 Twist messages.
//
// Binary Format (56 bytes, little-endian):
//   - Bytes 0-7:   linear.x  (float64)
//   - Bytes 8-15:  linear.y  (float64)
//   - Bytes 16-23: linear.z  (float64)
//   - Bytes 24-31: angular.x (float64)
//   - Bytes 32-39: angular.y (float64)
//   - Bytes 40-47: angular.z (float64)
//   - Bytes 48-55: timestamp (uint64, milliseconds since epoch)
package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"time"
)

// TwistMessageSize is the fixed size of a binary Twist message in bytes.
const TwistMessageSize = 56

// TwistMessageSizeLegacy is the legacy size without timestamp.
const TwistMessageSizeLegacy = 48

// ErrInvalidMessageSize indicates the message size doesn't match expected format.
var ErrInvalidMessageSize = errors.New("invalid twist message size")

// Vector3 represents a 3D vector with X, Y, Z components.
// Used for both linear and angular velocity in Twist messages.
type Vector3 struct {
	X float64 `json:"x"` // X component
	Y float64 `json:"y"` // Y component
	Z float64 `json:"z"` // Z component
}

// TwistMessage represents a ROS2 geometry_msgs/Twist message.
// Contains linear and angular velocity vectors for robot motion control.
type TwistMessage struct {
	Linear    Vector3 `json:"linear"`    // Linear velocity (m/s)
	Angular   Vector3 `json:"angular"`   // Angular velocity (rad/s)
	Timestamp uint64  `json:"timestamp"` // Timestamp in milliseconds since epoch
}

// NewTwistMessage creates a new TwistMessage with zero velocities and current timestamp.
func NewTwistMessage() *TwistMessage {
	return &TwistMessage{
		Linear:    Vector3{X: 0, Y: 0, Z: 0},
		Angular:   Vector3{X: 0, Y: 0, Z: 0},
		Timestamp: uint64(time.Now().UnixMilli()),
	}
}

// EmergencyStop creates a zero-velocity message (emergency stop command).
func EmergencyStop() *TwistMessage {
	return NewTwistMessage()
}

// EncodeTwist converts a TwistMessage to its binary representation.
// Returns a 56-byte slice in little-endian format.
//
// Example:
//
//	twist := &TwistMessage{Linear: Vector3{Y: 1.0}, Angular: Vector3{Z: 0.5}}
//	data := EncodeTwist(twist)
func EncodeTwist(twist *TwistMessage) []byte {
	buf := make([]byte, TwistMessageSize)

	// Encode linear velocity vector (bytes 0-23)
	binary.LittleEndian.PutUint64(buf[0:8], math.Float64bits(twist.Linear.X))
	binary.LittleEndian.PutUint64(buf[8:16], math.Float64bits(twist.Linear.Y))
	binary.LittleEndian.PutUint64(buf[16:24], math.Float64bits(twist.Linear.Z))

	// Encode angular velocity vector (bytes 24-47)
	binary.LittleEndian.PutUint64(buf[24:32], math.Float64bits(twist.Angular.X))
	binary.LittleEndian.PutUint64(buf[32:40], math.Float64bits(twist.Angular.Y))
	binary.LittleEndian.PutUint64(buf[40:48], math.Float64bits(twist.Angular.Z))

	// Encode timestamp (bytes 48-55)
	// If timestamp is 0, use current time
	ts := twist.Timestamp
	if ts == 0 {
		ts = uint64(time.Now().UnixMilli())
	}
	binary.LittleEndian.PutUint64(buf[48:56], ts)

	return buf
}

// DecodeTwist parses a binary Twist message into a TwistMessage struct.
// Supports both 48-byte (legacy) and 56-byte (with timestamp) formats.
// Returns an error if the data size is invalid.
//
// Example:
//
//	twist, err := DecodeTwist(data)
//	if err != nil {
//	    log.Fatal(err)
//	}
//	fmt.Printf("Linear Y: %f\n", twist.Linear.Y)
func DecodeTwist(data []byte) (*TwistMessage, error) {
	if len(data) != TwistMessageSize && len(data) != TwistMessageSizeLegacy {
		return nil, fmt.Errorf("%w: expected %d or %d bytes, got %d bytes",
			ErrInvalidMessageSize, TwistMessageSize, TwistMessageSizeLegacy, len(data))
	}

	twist := &TwistMessage{}

	// Decode linear velocity vector (bytes 0-23)
	twist.Linear.X = math.Float64frombits(binary.LittleEndian.Uint64(data[0:8]))
	twist.Linear.Y = math.Float64frombits(binary.LittleEndian.Uint64(data[8:16]))
	twist.Linear.Z = math.Float64frombits(binary.LittleEndian.Uint64(data[16:24]))

	// Decode angular velocity vector (bytes 24-47)
	twist.Angular.X = math.Float64frombits(binary.LittleEndian.Uint64(data[24:32]))
	twist.Angular.Y = math.Float64frombits(binary.LittleEndian.Uint64(data[32:40]))
	twist.Angular.Z = math.Float64frombits(binary.LittleEndian.Uint64(data[40:48]))

	// Decode timestamp if present (bytes 48-55)
	if len(data) >= TwistMessageSize {
		twist.Timestamp = binary.LittleEndian.Uint64(data[48:56])
	}

	return twist, nil
}

// GetLatencyMs calculates the latency from the message timestamp to now.
// Returns latency in milliseconds.
func (t *TwistMessage) GetLatencyMs() int64 {
	if t.Timestamp == 0 {
		return 0
	}
	return time.Now().UnixMilli() - int64(t.Timestamp)
}

// String returns a human-readable representation of the TwistMessage.
func (t *TwistMessage) String() string {
	return fmt.Sprintf(
		"Twist{linear: [%.3f, %.3f, %.3f], angular: [%.3f, %.3f, %.3f], latency: %dms}",
		t.Linear.X, t.Linear.Y, t.Linear.Z,
		t.Angular.X, t.Angular.Y, t.Angular.Z,
		t.GetLatencyMs(),
	)
}

// IsZero returns true if all velocity components are zero.
func (t *TwistMessage) IsZero() bool {
	return t.Linear.X == 0 && t.Linear.Y == 0 && t.Linear.Z == 0 &&
		t.Angular.X == 0 && t.Angular.Y == 0 && t.Angular.Z == 0
}

// IsEmergencyStop is an alias for IsZero - checks if this is a stop command.
func (t *TwistMessage) IsEmergencyStop() bool {
	return t.IsZero()
}

// Clone creates a deep copy of the TwistMessage.
func (t *TwistMessage) Clone() *TwistMessage {
	return &TwistMessage{
		Linear:    Vector3{X: t.Linear.X, Y: t.Linear.Y, Z: t.Linear.Z},
		Angular:   Vector3{X: t.Angular.X, Y: t.Angular.Y, Z: t.Angular.Z},
		Timestamp: t.Timestamp,
	}
}
