// Package main provides WebRTC peer connection management.
//
// PeerManager handles creation, tracking, and cleanup of WebRTC peer connections.
// It maintains a thread-safe registry of all connected peers and their data channels.
package main

import (
	"fmt"
	"log"
	"sync"

	"github.com/google/uuid"
	"github.com/pion/webrtc/v3"
)

// PeerType identifies the role of a connected peer.
type PeerType string

const (
	// PeerTypeWeb represents a browser-based web client.
	PeerTypeWeb PeerType = "web"
	// PeerTypePython represents the Python ROS2 bridge client.
	PeerTypePython PeerType = "python"
)

// Peer represents a connected WebRTC peer with its associated resources.
type Peer struct {
	ID             string                    // Unique identifier
	Type           PeerType                  // web or python
	Connection     *webrtc.PeerConnection    // WebRTC peer connection
	DataChannel    *webrtc.DataChannel       // Primary data channel for Twist messages
	mu             sync.RWMutex              // Protects concurrent access
	OnTwistMessage func(twist *TwistMessage) // Callback for received Twist messages
}

// PeerManager manages all connected WebRTC peers.
// Thread-safe for concurrent access from multiple goroutines.
type PeerManager struct {
	peers      map[string]*Peer // Active peers indexed by ID
	mu         sync.RWMutex     // Protects peers map
	webrtcAPI  *webrtc.API      // WebRTC API instance
	config     webrtc.Configuration
	onMessage  func(from *Peer, data []byte) // Global message handler
}

// NewPeerManager creates a new PeerManager with the given WebRTC configuration.
//
// Example:
//
//	config := webrtc.Configuration{
//	    ICEServers: []webrtc.ICEServer{{URLs: []string{"stun:stun.l.google.com:19302"}}},
//	}
//	pm := NewPeerManager(config)
func NewPeerManager(config webrtc.Configuration) *PeerManager {
	return &PeerManager{
		peers:     make(map[string]*Peer),
		webrtcAPI: webrtc.NewAPI(),
		config:    config,
	}
}

// SetMessageHandler sets the global callback for all incoming DataChannel messages.
// The callback receives the source peer and raw message data.
func (pm *PeerManager) SetMessageHandler(handler func(from *Peer, data []byte)) {
	pm.mu.Lock()
	defer pm.mu.Unlock()
	pm.onMessage = handler
}

// CreatePeer creates a new WebRTC peer connection and registers it.
// Returns the peer ID and any error encountered.
//
// Parameters:
//   - peerType: The type of peer (web or python)
//
// Returns:
//   - *Peer: The created peer instance
//   - error: Any error during creation
func (pm *PeerManager) CreatePeer(peerType PeerType) (*Peer, error) {
	// Create new peer connection
	pc, err := pm.webrtcAPI.NewPeerConnection(pm.config)
	if err != nil {
		return nil, fmt.Errorf("failed to create peer connection: %w", err)
	}

	// Generate unique peer ID
	peerID := uuid.New().String()[:8]

	peer := &Peer{
		ID:         peerID,
		Type:       peerType,
		Connection: pc,
	}

	// Set up connection state change handler
	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		log.Printf("[Peer %s] Connection state: %s", peerID, state.String())

		switch state {
		case webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed:
			pm.RemovePeer(peerID)
		}
	})

	// Set up ICE connection state handler
	pc.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		log.Printf("[Peer %s] ICE state: %s", peerID, state.String())
	})

	// Set up data channel handler for incoming channels
	pc.OnDataChannel(func(dc *webrtc.DataChannel) {
		log.Printf("[Peer %s] DataChannel received: %s", peerID, dc.Label())
		pm.setupDataChannel(peer, dc)
	})

	// Register peer
	pm.mu.Lock()
	pm.peers[peerID] = peer
	pm.mu.Unlock()

	log.Printf("[PeerManager] Created peer %s (type: %s)", peerID, peerType)
	return peer, nil
}

// CreateDataChannel creates a new DataChannel on the peer connection.
// Used when this peer is the offerer.
func (pm *PeerManager) CreateDataChannel(peer *Peer, label string) error {
	// Data channel configuration for reliable, ordered delivery
	ordered := true
	dc, err := peer.Connection.CreateDataChannel(label, &webrtc.DataChannelInit{
		Ordered: &ordered,
	})
	if err != nil {
		return fmt.Errorf("failed to create data channel: %w", err)
	}

	pm.setupDataChannel(peer, dc)
	return nil
}

// setupDataChannel configures event handlers for a DataChannel.
func (pm *PeerManager) setupDataChannel(peer *Peer, dc *webrtc.DataChannel) {
	peer.mu.Lock()
	peer.DataChannel = dc
	peer.mu.Unlock()

	dc.OnOpen(func() {
		log.Printf("[Peer %s] DataChannel '%s' opened", peer.ID, dc.Label())
	})

	dc.OnClose(func() {
		log.Printf("[Peer %s] DataChannel '%s' closed", peer.ID, dc.Label())
	})

	dc.OnError(func(err error) {
		log.Printf("[Peer %s] DataChannel error: %v", peer.ID, err)
	})

	// Handle incoming messages
	dc.OnMessage(func(msg webrtc.DataChannelMessage) {
		pm.mu.RLock()
		handler := pm.onMessage
		pm.mu.RUnlock()

		if handler != nil {
			handler(peer, msg.Data)
		}
	})
}

// GetPeer retrieves a peer by its ID.
// Returns nil if the peer doesn't exist.
func (pm *PeerManager) GetPeer(peerID string) *Peer {
	pm.mu.RLock()
	defer pm.mu.RUnlock()
	return pm.peers[peerID]
}

// GetPeersByType returns all peers of the specified type.
func (pm *PeerManager) GetPeersByType(peerType PeerType) []*Peer {
	pm.mu.RLock()
	defer pm.mu.RUnlock()

	var result []*Peer
	for _, peer := range pm.peers {
		if peer.Type == peerType {
			result = append(result, peer)
		}
	}
	return result
}

// RemovePeer removes and closes a peer connection.
func (pm *PeerManager) RemovePeer(peerID string) {
	pm.mu.Lock()
	peer, exists := pm.peers[peerID]
	if exists {
		delete(pm.peers, peerID)
	}
	pm.mu.Unlock()

	if exists && peer.Connection != nil {
		peer.Connection.Close()
		log.Printf("[PeerManager] Removed peer %s", peerID)
	}
}

// BroadcastToType sends data to all peers of the specified type.
// Returns the number of peers that received the message.
func (pm *PeerManager) BroadcastToType(peerType PeerType, data []byte) int {
	peers := pm.GetPeersByType(peerType)
	sent := 0

	for _, peer := range peers {
		if err := pm.SendToPeer(peer.ID, data); err == nil {
			sent++
		}
	}

	return sent
}

// SendToPeer sends binary data to a specific peer via its DataChannel.
func (pm *PeerManager) SendToPeer(peerID string, data []byte) error {
	peer := pm.GetPeer(peerID)
	if peer == nil {
		return fmt.Errorf("peer %s not found", peerID)
	}

	peer.mu.RLock()
	dc := peer.DataChannel
	peer.mu.RUnlock()

	if dc == nil {
		return fmt.Errorf("peer %s has no data channel", peerID)
	}

	if dc.ReadyState() != webrtc.DataChannelStateOpen {
		return fmt.Errorf("data channel not open (state: %s)", dc.ReadyState().String())
	}

	return dc.Send(data)
}

// PeerCount returns the current number of connected peers.
func (pm *PeerManager) PeerCount() int {
	pm.mu.RLock()
	defer pm.mu.RUnlock()
	return len(pm.peers)
}

// Close shuts down all peer connections and cleans up resources.
func (pm *PeerManager) Close() {
	pm.mu.Lock()
	defer pm.mu.Unlock()

	for id, peer := range pm.peers {
		if peer.Connection != nil {
			peer.Connection.Close()
		}
		delete(pm.peers, id)
	}

	log.Println("[PeerManager] All peers closed")
}
