# teleop_ws

### WebRTC Relay Server for ROS2 Twist Message Transmission

This server acts as a Selective Forwarding Unit (SFU) relay between web clients and Python ROS2 clients. It handles WebRTC signaling and forwards binary Twist
messages through DataChannels or WebSockets.

## Architecture:
Web clients connect and send control commands (Twist messages)
Python client connects and receives forwarded Twist messages
Messages are forwarded in binary format for minimal latency

## Endpoints:
POST /offer      - WebRTC signaling (SDP offer/answer exchange)
POST /ice        - ICE candidate exchange
GET  /status     - Server status and peer information
GET  /health     - Health check
WS   /ws/signaling - WebSocket for signaling with ping/pong keepalive
WS   /ws/data      - WebSocket for data transfer (alternative to DataChannel)

## Usage:
```
    cd go-relay
    go mod tidy
	go run .
```
Server starts on :8080 by default

## Environment Variables:
PORT: HTTP server port (default: 8080)
STUN_SERVER: STUN server URL (default: stun:stun.l.google.com:19302)