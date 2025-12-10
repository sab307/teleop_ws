// Package main provides WebSocket handlers for signaling and data transfer.
//
// WebSocket Endpoints:
//   - /ws/signaling - WebRTC signaling with ping/pong keepalive
//   - /ws/data      - Direct data transfer (alternative to DataChannel)
//
// Ping/Pong Mechanism:
//   - Server sends ping every 30 seconds
//   - Client must respond with pong within 10 seconds
//   - Connection closed if pong not received
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/gorilla/websocket"
)

// WebSocket configuration
const (
	// Ping interval - how often to send pings
	pingInterval = 30 * time.Second

	// Pong timeout - how long to wait for pong response
	pongTimeout = 10 * time.Second

	// Write timeout for WebSocket writes
	writeTimeout = 10 * time.Second

	// Max message size (1MB)
	maxMessageSize = 1024 * 1024
)

// WebSocket upgrader with permissive CORS for development
var upgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	CheckOrigin: func(r *http.Request) bool {
		return true // Allow all origins for development
	},
}

// SignalingMessage represents a WebSocket signaling message
type SignalingMessage struct {
	Type      string          `json:"type"`                // "offer", "answer", "ice", "ping", "pong", "error"
	PeerID    string          `json:"peer_id,omitempty"`   // Assigned peer ID
	PeerType  string          `json:"peer_type,omitempty"` // "web" or "python"
	SDP       string          `json:"sdp,omitempty"`       // SDP offer/answer
	Candidate json.RawMessage `json:"candidate,omitempty"` // ICE candidate
	Error     string          `json:"error,omitempty"`     // Error message
	Timestamp int64           `json:"timestamp,omitempty"` // For latency measurement
}

// DataMessage represents a WebSocket data message
type DataMessage struct {
	Type      string `json:"type"`                // "twist", "status", "ping", "pong"
	PeerID    string `json:"peer_id,omitempty"`   // Source peer ID
	PeerType  string `json:"peer_type,omitempty"` // "web" or "python"
	Data      []byte `json:"data,omitempty"`      // Binary data (base64 encoded in JSON)
	Timestamp int64  `json:"timestamp,omitempty"` // Message timestamp
}

// WSClient represents a connected WebSocket client
type WSClient struct {
	ID       string
	PeerType string
	Conn     *websocket.Conn
	Send     chan []byte
	manager  *WSManager
	mu       sync.Mutex
}

// WSManager manages WebSocket connections
type WSManager struct {
	// Signaling clients
	signalingClients map[string]*WSClient
	signalingMu      sync.RWMutex

	// Data clients
	dataClients map[string]*WSClient
	dataMu      sync.RWMutex

	// Message router for data forwarding
	router *MessageRouter
	
	// Peer manager for cross-protocol bridging (WebSocket <-> WebRTC)
	peerManager *PeerManager
}

// NewWSManager creates a new WebSocket manager
func NewWSManager(router *MessageRouter, peerManager *PeerManager) *WSManager {
	return &WSManager{
		signalingClients: make(map[string]*WSClient),
		dataClients:      make(map[string]*WSClient),
		router:           router,
		peerManager:      peerManager,
	}
}

// HandleSignalingWS handles WebSocket connections for signaling
func (m *WSManager) HandleSignalingWS(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("[WS-Signaling] Upgrade error: %v", err)
		return
	}

	// Get peer type from query parameter
	peerType := r.URL.Query().Get("type")
	if peerType == "" {
		peerType = "web"
	}

	clientID := uuid.New().String()[:8]

	client := &WSClient{
		ID:       clientID,
		PeerType: peerType,
		Conn:     conn,
		Send:     make(chan []byte, 256),
		manager:  m,
	}

	m.signalingMu.Lock()
	m.signalingClients[clientID] = client
	m.signalingMu.Unlock()

	log.Printf("[WS-Signaling] Client connected: %s (type: %s)", clientID, peerType)

	// Send welcome message with peer ID
	welcome := SignalingMessage{
		Type:      "welcome",
		PeerID:    clientID,
		PeerType:  peerType,
		Timestamp: time.Now().UnixMilli(),
	}
	welcomeBytes, _ := json.Marshal(welcome)
	client.Send <- welcomeBytes

	// Start read/write pumps
	go client.writePumpSignaling()
	go client.readPumpSignaling()
}

// HandleDataWS handles WebSocket connections for data transfer
func (m *WSManager) HandleDataWS(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("[WS-Data] Upgrade error: %v", err)
		return
	}

	// Get peer type from query parameter
	peerType := r.URL.Query().Get("type")
	if peerType == "" {
		peerType = "web"
	}

	clientID := uuid.New().String()[:8]

	client := &WSClient{
		ID:       clientID,
		PeerType: peerType,
		Conn:     conn,
		Send:     make(chan []byte, 256),
		manager:  m,
	}

	m.dataMu.Lock()
	m.dataClients[clientID] = client
	m.dataMu.Unlock()

	log.Printf("[WS-Data] Client connected: %s (type: %s)", clientID, peerType)

	// Send welcome message
	welcome := DataMessage{
		Type:      "welcome",
		PeerID:    clientID,
		PeerType:  peerType,
		Timestamp: time.Now().UnixMilli(),
	}
	welcomeBytes, _ := json.Marshal(welcome)
	client.Send <- welcomeBytes

	// Start read/write pumps
	go client.writePumpData()
	go client.readPumpData()
}

// readPumpSignaling reads messages from the WebSocket (signaling)
func (c *WSClient) readPumpSignaling() {
	defer func() {
		c.manager.removeSignalingClient(c.ID)
		c.Conn.Close()
	}()

	c.Conn.SetReadLimit(maxMessageSize)
	c.Conn.SetReadDeadline(time.Now().Add(pongTimeout + pingInterval))
	c.Conn.SetPongHandler(func(string) error {
		c.Conn.SetReadDeadline(time.Now().Add(pongTimeout + pingInterval))
		log.Printf("[WS-Signaling] Pong received from %s", c.ID)
		return nil
	})

	for {
		_, message, err := c.Conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseAbnormalClosure) {
				log.Printf("[WS-Signaling] Read error from %s: %v", c.ID, err)
			}
			break
		}

		// Parse signaling message
		var msg SignalingMessage
		if err := json.Unmarshal(message, &msg); err != nil {
			log.Printf("[WS-Signaling] Parse error: %v", err)
			continue
		}

		c.handleSignalingMessage(&msg)
	}
}

// writePumpSignaling writes messages to the WebSocket (signaling)
func (c *WSClient) writePumpSignaling() {
	ticker := time.NewTicker(pingInterval)
	defer func() {
		ticker.Stop()
		c.Conn.Close()
	}()

	for {
		select {
		case message, ok := <-c.Send:
			c.Conn.SetWriteDeadline(time.Now().Add(writeTimeout))
			if !ok {
				c.Conn.WriteMessage(websocket.CloseMessage, []byte{})
				return
			}

			if err := c.Conn.WriteMessage(websocket.TextMessage, message); err != nil {
				log.Printf("[WS-Signaling] Write error to %s: %v", c.ID, err)
				return
			}

		case <-ticker.C:
			// Send ping
			c.Conn.SetWriteDeadline(time.Now().Add(writeTimeout))
			if err := c.Conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				log.Printf("[WS-Signaling] Ping error to %s: %v", c.ID, err)
				return
			}
			log.Printf("[WS-Signaling] Ping sent to %s", c.ID)
		}
	}
}

// handleSignalingMessage processes incoming signaling messages
func (c *WSClient) handleSignalingMessage(msg *SignalingMessage) {
	switch msg.Type {
	case "offer":
		log.Printf("[WS-Signaling] Received offer from %s", c.ID)
		// Forward to other clients or handle WebRTC offer
		c.manager.broadcastSignaling(c.ID, msg)

	case "answer":
		log.Printf("[WS-Signaling] Received answer from %s", c.ID)
		c.manager.broadcastSignaling(c.ID, msg)

	case "ice":
		log.Printf("[WS-Signaling] Received ICE candidate from %s", c.ID)
		c.manager.broadcastSignaling(c.ID, msg)

	case "ping":
		// Respond with pong
		pong := SignalingMessage{
			Type:      "pong",
			PeerID:    c.ID,
			Timestamp: time.Now().UnixMilli(),
		}
		pongBytes, _ := json.Marshal(pong)
		c.Send <- pongBytes

	default:
		log.Printf("[WS-Signaling] Unknown message type: %s", msg.Type)
	}
}

// readPumpData reads messages from the WebSocket (data)
func (c *WSClient) readPumpData() {
	defer func() {
		c.manager.removeDataClient(c.ID)
		c.Conn.Close()
	}()

	c.Conn.SetReadLimit(maxMessageSize)
	c.Conn.SetReadDeadline(time.Now().Add(pongTimeout + pingInterval))
	c.Conn.SetPongHandler(func(string) error {
		c.Conn.SetReadDeadline(time.Now().Add(pongTimeout + pingInterval))
		return nil
	})

	for {
		messageType, message, err := c.Conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseAbnormalClosure) {
				log.Printf("[WS-Data] Read error from %s: %v", c.ID, err)
			}
			break
		}

		// Handle binary messages (raw Twist data)
		if messageType == websocket.BinaryMessage {
			c.handleBinaryData(message)
			continue
		}

		// Handle text messages (JSON)
		var msg DataMessage
		if err := json.Unmarshal(message, &msg); err != nil {
			log.Printf("[WS-Data] Parse error: %v", err)
			continue
		}

		c.handleDataMessage(&msg)
	}
}

// writePumpData writes messages to the WebSocket (data)
func (c *WSClient) writePumpData() {
	ticker := time.NewTicker(pingInterval)
	defer func() {
		ticker.Stop()
		c.Conn.Close()
	}()

	for {
		select {
		case message, ok := <-c.Send:
			c.Conn.SetWriteDeadline(time.Now().Add(writeTimeout))
			if !ok {
				c.Conn.WriteMessage(websocket.CloseMessage, []byte{})
				return
			}

			// Check if it's binary data (starts with non-JSON character)
			if len(message) > 0 && message[0] != '{' {
				if err := c.Conn.WriteMessage(websocket.BinaryMessage, message); err != nil {
					return
				}
			} else {
				if err := c.Conn.WriteMessage(websocket.TextMessage, message); err != nil {
					return
				}
			}

		case <-ticker.C:
			c.Conn.SetWriteDeadline(time.Now().Add(writeTimeout))
			if err := c.Conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}

// handleBinaryData processes binary Twist messages
func (c *WSClient) handleBinaryData(data []byte) {
	// Update router stats
	if c.manager.router != nil {
		c.manager.router.stats.MessagesReceived++
	}

	// Parse twist to check if it's valid
	twist, err := DecodeTwist(data)
	if err != nil {
		log.Printf("[WS-Data] Invalid twist data from %s: %v", c.ID, err)
		return
	}

	latency := twist.GetLatencyMs()
	if !twist.IsZero() {
		log.Printf("[WS-Data] Twist from %s (type: %s): lin.y=%.2f, ang.z=%.2f, latency=%dms",
			c.ID, c.PeerType, twist.Linear.Y, twist.Angular.Z, latency)
	}

	// Forward to other WebSocket clients of opposite type
	c.manager.forwardData(c.ID, c.PeerType, data)
	
	// Also forward to WebRTC peers of opposite type (cross-protocol bridging)
	if c.manager.peerManager != nil {
		var targetType PeerType
		if c.PeerType == "web" {
			targetType = PeerTypePython
		} else {
			targetType = PeerTypeWeb
		}
		sent := c.manager.peerManager.BroadcastToType(targetType, data)
		if sent > 0 {
			log.Printf("[WS-Data] Bridged to %d WebRTC %s client(s)", sent, targetType)
			if c.manager.router != nil {
				c.manager.router.stats.MessagesForwarded += uint64(sent)
			}
		}
	}
}

// handleDataMessage processes JSON data messages
func (c *WSClient) handleDataMessage(msg *DataMessage) {
	switch msg.Type {
	case "twist":
		// Forward binary data
		c.manager.forwardData(c.ID, c.PeerType, msg.Data)

	case "ping":
		pong := DataMessage{
			Type:      "pong",
			PeerID:    c.ID,
			Timestamp: time.Now().UnixMilli(),
		}
		pongBytes, _ := json.Marshal(pong)
		c.Send <- pongBytes

	default:
		log.Printf("[WS-Data] Unknown message type: %s", msg.Type)
	}
}

// forwardData forwards data to WebSocket clients of the opposite peer type
func (m *WSManager) forwardData(senderID, senderType string, data []byte) {
	m.dataMu.RLock()
	defer m.dataMu.RUnlock()

	targetType := "python"
	if senderType == "python" {
		targetType = "web"
	}

	forwarded := 0
	for _, client := range m.dataClients {
		if client.ID != senderID && client.PeerType == targetType {
			select {
			case client.Send <- data:
				forwarded++
			default:
				log.Printf("[WS-Data] Send buffer full for %s", client.ID)
			}
		}
	}

	if m.router != nil && forwarded > 0 {
		m.router.stats.MessagesForwarded += uint64(forwarded)
	}
}

// BroadcastToType sends data to all WebSocket clients of a specific type
// Used for cross-protocol bridging from WebRTC to WebSocket
func (m *WSManager) BroadcastToType(targetType string, data []byte) int {
	m.dataMu.RLock()
	defer m.dataMu.RUnlock()

	sent := 0
	for _, client := range m.dataClients {
		if client.PeerType == targetType {
			select {
			case client.Send <- data:
				sent++
			default:
				log.Printf("[WS-Data] Send buffer full for %s", client.ID)
			}
		}
	}
	return sent
}

// broadcastSignaling broadcasts signaling message to other clients
func (m *WSManager) broadcastSignaling(senderID string, msg *SignalingMessage) {
	m.signalingMu.RLock()
	defer m.signalingMu.RUnlock()

	msg.PeerID = senderID
	msgBytes, _ := json.Marshal(msg)

	for _, client := range m.signalingClients {
		if client.ID != senderID {
			select {
			case client.Send <- msgBytes:
			default:
				log.Printf("[WS-Signaling] Send buffer full for %s", client.ID)
			}
		}
	}
}

// removeSignalingClient removes a client from the signaling pool
func (m *WSManager) removeSignalingClient(id string) {
	m.signalingMu.Lock()
	defer m.signalingMu.Unlock()

	if client, ok := m.signalingClients[id]; ok {
		close(client.Send)
		delete(m.signalingClients, id)
		log.Printf("[WS-Signaling] Client disconnected: %s", id)
	}
}

// removeDataClient removes a client from the data pool
func (m *WSManager) removeDataClient(id string) {
	m.dataMu.Lock()
	defer m.dataMu.Unlock()

	if client, ok := m.dataClients[id]; ok {
		close(client.Send)
		delete(m.dataClients, id)
		log.Printf("[WS-Data] Client disconnected: %s", id)
	}
}

// GetSignalingClientCount returns the number of connected signaling clients
func (m *WSManager) GetSignalingClientCount() int {
	m.signalingMu.RLock()
	defer m.signalingMu.RUnlock()
	return len(m.signalingClients)
}

// GetDataClientCount returns the number of connected data clients
func (m *WSManager) GetDataClientCount() int {
	m.dataMu.RLock()
	defer m.dataMu.RUnlock()
	return len(m.dataClients)
}

// GetDataClientsByType returns count of data clients by type
func (m *WSManager) GetDataClientsByType() (web, python int) {
	m.dataMu.RLock()
	defer m.dataMu.RUnlock()

	for _, client := range m.dataClients {
		if client.PeerType == "web" {
			web++
		} else {
			python++
		}
	}
	return
}
