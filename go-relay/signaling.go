// Package main provides HTTP signaling endpoints for WebRTC connection establishment.
//
// Signaling Endpoints:
//   - POST /offer     - Submit SDP offer, receive SDP answer
//   - POST /answer    - Submit SDP answer for a peer
//   - POST /ice       - Submit ICE candidate
//   - GET  /status    - Get server status and peer count
//   - GET  /health    - Health check endpoint
package main

import (
	"encoding/json"
	"log"
	"net/http"

	"github.com/pion/webrtc/v3"
)

// SignalingHandler handles WebRTC signaling over HTTP.
type SignalingHandler struct {
	peerManager *PeerManager
}

// NewSignalingHandler creates a new SignalingHandler with the given PeerManager.
func NewSignalingHandler(pm *PeerManager) *SignalingHandler {
	return &SignalingHandler{
		peerManager: pm,
	}
}

// OfferRequest represents an incoming SDP offer from a client.
type OfferRequest struct {
	SDP      string `json:"sdp"`      // SDP offer string
	Type     string `json:"type"`     // Should be "offer"
	PeerType string `json:"peerType"` // "web" or "python"
}

// AnswerResponse is sent back after processing an offer.
type AnswerResponse struct {
	SDP    string `json:"sdp"`    // SDP answer string
	Type   string `json:"type"`   // Should be "answer"
	PeerID string `json:"peerID"` // Assigned peer ID
}

// ICECandidateRequest represents an ICE candidate submission.
type ICECandidateRequest struct {
	PeerID    string `json:"peerID"`    // Target peer ID
	Candidate string `json:"candidate"` // ICE candidate string
	SDPMid    string `json:"sdpMid"`    // SDP media ID
	SDPMLine  uint16 `json:"sdpMLine"`  // SDP media line index
}

// StatusResponse contains server status information.
type StatusResponse struct {
	Status     string `json:"status"`     // Server status
	PeerCount  int    `json:"peerCount"`  // Number of connected peers
	WebPeers   int    `json:"webPeers"`   // Number of web clients
	PyPeers    int    `json:"pyPeers"`    // Number of Python clients
}

// ErrorResponse represents an error response.
type ErrorResponse struct {
	Error   string `json:"error"`   // Error message
	Details string `json:"details"` // Additional details
}

// RegisterRoutes sets up all HTTP routes on the given ServeMux.
func (sh *SignalingHandler) RegisterRoutes(mux *http.ServeMux) {
	// Wrap handlers with CORS middleware
	mux.HandleFunc("/offer", sh.corsMiddleware(sh.handleOffer))
	mux.HandleFunc("/answer", sh.corsMiddleware(sh.handleAnswer))
	mux.HandleFunc("/ice", sh.corsMiddleware(sh.handleICE))
	mux.HandleFunc("/status", sh.corsMiddleware(sh.handleStatus))
	mux.HandleFunc("/health", sh.corsMiddleware(sh.handleHealth))
}

// corsMiddleware adds CORS headers to allow cross-origin requests.
func (sh *SignalingHandler) corsMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Set CORS headers
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")

		// Handle preflight requests
		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusOK)
			return
		}

		next(w, r)
	}
}

// handleOffer processes an SDP offer and returns an SDP answer.
// Creates a new peer connection and sets up the data channel.
//
// POST /offer
// Request:  { "sdp": "...", "type": "offer", "peerType": "web" }
// Response: { "sdp": "...", "type": "answer", "peerID": "abc123" }
func (sh *SignalingHandler) handleOffer(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sh.sendError(w, http.StatusMethodNotAllowed, "Method not allowed", "Use POST")
		return
	}

	var req OfferRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		sh.sendError(w, http.StatusBadRequest, "Invalid JSON", err.Error())
		return
	}

	// Validate peer type
	peerType := PeerType(req.PeerType)
	if peerType != PeerTypeWeb && peerType != PeerTypePython {
		peerType = PeerTypeWeb // Default to web
	}

	// Create new peer
	peer, err := sh.peerManager.CreatePeer(peerType)
	if err != nil {
		sh.sendError(w, http.StatusInternalServerError, "Failed to create peer", err.Error())
		return
	}

	// Set remote description (the offer)
	offer := webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer,
		SDP:  req.SDP,
	}
	if err := peer.Connection.SetRemoteDescription(offer); err != nil {
		sh.peerManager.RemovePeer(peer.ID)
		sh.sendError(w, http.StatusBadRequest, "Invalid SDP offer", err.Error())
		return
	}

	// Create answer
	answer, err := peer.Connection.CreateAnswer(nil)
	if err != nil {
		sh.peerManager.RemovePeer(peer.ID)
		sh.sendError(w, http.StatusInternalServerError, "Failed to create answer", err.Error())
		return
	}

	// Set local description
	if err := peer.Connection.SetLocalDescription(answer); err != nil {
		sh.peerManager.RemovePeer(peer.ID)
		sh.sendError(w, http.StatusInternalServerError, "Failed to set local description", err.Error())
		return
	}

	// Wait for ICE gathering to complete (simplified - in production use trickle ICE)
	<-webrtc.GatheringCompletePromise(peer.Connection)

	// Send response with complete SDP
	resp := AnswerResponse{
		SDP:    peer.Connection.LocalDescription().SDP,
		Type:   "answer",
		PeerID: peer.ID,
	}

	log.Printf("[Signaling] Offer processed for peer %s (type: %s)", peer.ID, peerType)
	sh.sendJSON(w, http.StatusOK, resp)
}

// handleAnswer processes an SDP answer (for cases where server creates offer).
// Currently not used in the main flow but available for future use.
//
// POST /answer
func (sh *SignalingHandler) handleAnswer(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sh.sendError(w, http.StatusMethodNotAllowed, "Method not allowed", "Use POST")
		return
	}

	// This endpoint is available for advanced scenarios where
	// the server needs to initiate offers
	sh.sendJSON(w, http.StatusOK, map[string]string{
		"message": "Answer endpoint ready",
	})
}

// handleICE processes an ICE candidate from a client.
// Adds the candidate to the specified peer connection.
//
// POST /ice
// Request: { "peerID": "...", "candidate": "...", "sdpMid": "...", "sdpMLine": 0 }
func (sh *SignalingHandler) handleICE(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sh.sendError(w, http.StatusMethodNotAllowed, "Method not allowed", "Use POST")
		return
	}

	var req ICECandidateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		sh.sendError(w, http.StatusBadRequest, "Invalid JSON", err.Error())
		return
	}

	peer := sh.peerManager.GetPeer(req.PeerID)
	if peer == nil {
		sh.sendError(w, http.StatusNotFound, "Peer not found", req.PeerID)
		return
	}

	// Add ICE candidate
	candidate := webrtc.ICECandidateInit{
		Candidate:     req.Candidate,
		SDPMid:        &req.SDPMid,
		SDPMLineIndex: &req.SDPMLine,
	}

	if err := peer.Connection.AddICECandidate(candidate); err != nil {
		sh.sendError(w, http.StatusBadRequest, "Failed to add ICE candidate", err.Error())
		return
	}

	log.Printf("[Signaling] ICE candidate added for peer %s", req.PeerID)
	sh.sendJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// handleStatus returns the current server status.
//
// GET /status
func (sh *SignalingHandler) handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" {
		sh.sendError(w, http.StatusMethodNotAllowed, "Method not allowed", "Use GET")
		return
	}

	webPeers := len(sh.peerManager.GetPeersByType(PeerTypeWeb))
	pyPeers := len(sh.peerManager.GetPeersByType(PeerTypePython))

	resp := StatusResponse{
		Status:    "running",
		PeerCount: sh.peerManager.PeerCount(),
		WebPeers:  webPeers,
		PyPeers:   pyPeers,
	}

	sh.sendJSON(w, http.StatusOK, resp)
}

// handleHealth is a simple health check endpoint.
//
// GET /health
func (sh *SignalingHandler) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(`{"status":"healthy"}`))
}

// sendJSON sends a JSON response with the given status code.
func (sh *SignalingHandler) sendJSON(w http.ResponseWriter, status int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(data)
}

// sendError sends an error response in JSON format.
func (sh *SignalingHandler) sendError(w http.ResponseWriter, status int, message, details string) {
	sh.sendJSON(w, status, ErrorResponse{
		Error:   message,
		Details: details,
	})
}
