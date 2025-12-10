// WebRTC Relay Server for ROS2 Twist Message Transmission
//
// This server acts as a Selective Forwarding Unit (SFU) relay between web clients
// and Python ROS2 clients. It handles WebRTC signaling and forwards binary Twist
// messages through DataChannels or WebSockets.
//
// Architecture:
//   - Web clients connect and send control commands (Twist messages)
//   - Python client connects and receives forwarded Twist messages
//   - Messages are forwarded in binary format for minimal latency
//
// Endpoints:
//   - POST /offer      - WebRTC signaling (SDP offer/answer exchange)
//   - POST /ice        - ICE candidate exchange
//   - GET  /status     - Server status and peer information
//   - GET  /health     - Health check
//   - WS   /ws/signaling - WebSocket for signaling with ping/pong keepalive
//   - WS   /ws/data      - WebSocket for data transfer (alternative to DataChannel)
//
// Usage:
//
//	go run .
//	# Server starts on :8080 by default
//
// Environment Variables:
//   - PORT: HTTP server port (default: 8080)
//   - STUN_SERVER: STUN server URL (default: stun:stun.l.google.com:19302)
package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/pion/webrtc/v3"
)

// Server configuration
type Config struct {
	Port       string   // HTTP server port
	STUNServer string   // STUN server URL
	Origins    []string // Allowed CORS origins
}

// loadConfig loads configuration from environment variables with defaults.
func loadConfig() *Config {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	stunServer := os.Getenv("STUN_SERVER")
	if stunServer == "" {
		stunServer = "stun:stun.l.google.com:19302"
	}

	return &Config{
		Port:       port,
		STUNServer: stunServer,
		Origins:    []string{"*"},
	}
}

// MessageRouter handles routing of Twist messages between peers.
type MessageRouter struct {
	peerManager *PeerManager
	wsManager   *WSManager  // WebSocket manager for cross-protocol routing
	stats       *RouterStats
}

// RouterStats tracks message routing statistics.
type RouterStats struct {
	MessagesReceived  uint64
	MessagesForwarded uint64
	ParseErrors       uint64
}

// NewMessageRouter creates a new message router.
func NewMessageRouter(pm *PeerManager) *MessageRouter {
	return &MessageRouter{
		peerManager: pm,
		stats:       &RouterStats{},
	}
}

// SetWSManager sets the WebSocket manager for cross-protocol routing.
func (mr *MessageRouter) SetWSManager(wsm *WSManager) {
	mr.wsManager = wsm
}

// HandleMessage processes an incoming DataChannel message.
// Routes Twist messages from web clients to Python clients and vice versa.
// Also bridges to WebSocket clients.
func (mr *MessageRouter) HandleMessage(from *Peer, data []byte) {
	mr.stats.MessagesReceived++

	// Attempt to decode as Twist message
	twist, err := DecodeTwist(data)
	if err != nil {
		// Not a valid Twist message - could be a control message
		log.Printf("[Router] Non-Twist message from %s (%d bytes)", from.ID, len(data))
		mr.stats.ParseErrors++
		return
	}

	if !twist.IsZero() {
		log.Printf("[Router] Twist from %s: %s", from.ID, twist.String())
	}

	// Route based on source peer type
	switch from.Type {
	case PeerTypeWeb:
		// Forward to all Python clients (WebRTC)
		sent := mr.peerManager.BroadcastToType(PeerTypePython, data)
		mr.stats.MessagesForwarded += uint64(sent)
		
		// Also forward to Python WebSocket clients
		if mr.wsManager != nil {
			wsSent := mr.wsManager.BroadcastToType("python", data)
			mr.stats.MessagesForwarded += uint64(wsSent)
			sent += wsSent
		}
		
		if sent > 0 {
			log.Printf("[Router] Forwarded to %d Python client(s)", sent)
		}

	case PeerTypePython:
		// Forward to all web clients (WebRTC)
		sent := mr.peerManager.BroadcastToType(PeerTypeWeb, data)
		mr.stats.MessagesForwarded += uint64(sent)
		
		// Also forward to web WebSocket clients
		if mr.wsManager != nil {
			wsSent := mr.wsManager.BroadcastToType("web", data)
			mr.stats.MessagesForwarded += uint64(wsSent)
			sent += wsSent
		}
		
		if sent > 0 {
			log.Printf("[Router] Forwarded to %d web client(s)", sent)
		}
	}
}

// GetStats returns current routing statistics.
func (mr *MessageRouter) GetStats() RouterStats {
	return *mr.stats
}

func main() {
	// ASCII banner
	banner := `
╔═══════════════════════════════════════════════════════════╗
║     WebRTC + WebSocket Twist Relay Server                 ║
║     ROS2 Robot Teleoperation                              ║
╚═══════════════════════════════════════════════════════════╝`
	fmt.Println(banner)

	// Load configuration
	config := loadConfig()
	log.Printf("Configuration: Port=%s, STUN=%s", config.Port, config.STUNServer)

	// Create WebRTC configuration
	webrtcConfig := webrtc.Configuration{
		ICEServers: []webrtc.ICEServer{
			{URLs: []string{config.STUNServer}},
		},
	}

	// Initialize peer manager
	peerManager := NewPeerManager(webrtcConfig)
	defer peerManager.Close()

	// Initialize message router
	router := NewMessageRouter(peerManager)
	peerManager.SetMessageHandler(router.HandleMessage)

	// Initialize signaling handler
	signaling := NewSignalingHandler(peerManager)

	// Initialize WebSocket manager with router for cross-protocol bridging
	wsManager := NewWSManager(router, peerManager)
	
	// Connect WSManager to router for bidirectional bridging
	router.SetWSManager(wsManager)

	// Set up HTTP server
	mux := http.NewServeMux()
	signaling.RegisterRoutes(mux)

	// Register WebSocket endpoints
	mux.HandleFunc("/ws/signaling", wsManager.HandleSignalingWS)
	mux.HandleFunc("/ws/data", wsManager.HandleDataWS)

	// Add stats endpoint
	mux.HandleFunc("/stats", func(w http.ResponseWriter, r *http.Request) {
		stats := router.GetStats()
		wsWeb, wsPython := wsManager.GetDataClientsByType()
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"received":%d,"forwarded":%d,"errors":%d,"ws_signaling":%d,"ws_data_web":%d,"ws_data_python":%d}`,
			stats.MessagesReceived, stats.MessagesForwarded, stats.ParseErrors,
			wsManager.GetSignalingClientCount(), wsWeb, wsPython)
	})

	// Serve web client files from ../web-client directory
	webClientDir := "../web-client"

	// Check if web-client directory exists
	if _, err := os.Stat(webClientDir); err == nil {
		// Serve index.html at root
		mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/" {
				http.ServeFile(w, r, webClientDir+"/index.html")
				return
			}
			// Serve other static files (js, css, etc.)
			http.StripPrefix("/", http.FileServer(http.Dir(webClientDir))).ServeHTTP(w, r)
		})
		log.Printf("📁 Serving web client from %s", webClientDir)
	} else {
		log.Printf("⚠️  Web client directory not found at %s", webClientDir)
		log.Println("   Open web-client/index.html directly in browser")
	}

	// Create HTTP server with timeouts
	server := &http.Server{
		Addr:         ":" + config.Port,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// Graceful shutdown handling
	done := make(chan bool, 1)
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-quit
		log.Println("\nShutting down server...")

		// Close all peer connections
		peerManager.Close()

		// Shutdown HTTP server
		server.Close()
		done <- true
	}()

	// Start server
	log.Printf("🚀 Server starting on http://localhost:%s", config.Port)
	log.Println("")
	log.Println("Web Interface:")
	log.Printf("  http://localhost:%s/          - Robot Control UI", config.Port)
	log.Println("")
	log.Println("HTTP Endpoints:")
	log.Println("  POST /offer  - WebRTC signaling")
	log.Println("  POST /ice    - ICE candidates")
	log.Println("  GET  /status - Server status")
	log.Println("  GET  /stats  - Message statistics")
	log.Println("  GET  /health - Health check")
	log.Println("")
	log.Println("WebSocket Endpoints:")
	log.Printf("  ws://localhost:%s/ws/signaling - Signaling + ping/pong keepalive", config.Port)
	log.Printf("  ws://localhost:%s/ws/data      - Data transfer (Twist messages)", config.Port)
	log.Println("")
	log.Println("Press Ctrl+C to stop")

	if err := server.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatalf("Server error: %v", err)
	}

	<-done
	log.Println("Server stopped")
}
