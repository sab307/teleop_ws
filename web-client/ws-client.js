/**
 * WebSocket Client Module
 * =======================
 * 
 * WebSocket client for data transfer with ping/pong keepalive.
 * Alternative to WebRTC DataChannel for simpler deployments.
 * 
 * Features:
 *   - Binary message support for Twist data
 *   - Automatic ping/pong keepalive
 *   - Reconnection support
 *   - Connection state tracking
 * 
 * @module WebSocketClient
 */

/**
 * WebSocket connection states
 */
const WSState = {
    DISCONNECTED: 'disconnected',
    CONNECTING: 'connecting',
    CONNECTED: 'connected',
    RECONNECTING: 'reconnecting',
    FAILED: 'failed'
};

/**
 * WebSocket client for Twist data transfer
 */
class WSDataClient {
    /**
     * Create WebSocket data client
     * @param {string} baseUrl - Server base URL (http://localhost:8080)
     * @param {Object} options - Configuration options
     * @param {string} [options.peerType='web'] - Client type
     * @param {number} [options.pingInterval=25000] - Ping interval (ms)
     * @param {number} [options.reconnectDelay=2000] - Reconnection delay (ms)
     * @param {number} [options.maxReconnectAttempts=5] - Max reconnection attempts
     */
    constructor(baseUrl, options = {}) {
        this.peerType = options.peerType || 'web';
        this.pingInterval = options.pingInterval || 25000;
        this.reconnectDelay = options.reconnectDelay || 2000;
        this.maxReconnectAttempts = options.maxReconnectAttempts || 5;
        
        // Convert HTTP URL to WebSocket URL
        this.url = baseUrl
            .replace('http://', 'ws://')
            .replace('https://', 'wss://')
            .replace(/\/$/, '') + '/ws/data?type=' + this.peerType;
        
        // Connection state
        this._state = WSState.DISCONNECTED;
        this._ws = null;
        this._peerId = null;
        this._reconnectCount = 0;
        
        // Ping/pong
        this._pingTimer = null;
        this._lastPongTime = null;
        
        // Callbacks
        this.onMessage = null;      // (data: ArrayBuffer) => void
        this.onStateChange = null;  // (state: string) => void
        this.onError = null;        // (error: Error) => void
        this.onOpen = null;         // () => void
        this.onClose = null;        // () => void
        
        // Statistics
        this.stats = {
            messagesSent: 0,
            messagesReceived: 0,
            bytesSent: 0,
            bytesReceived: 0,
            reconnectCount: 0
        };
        
        console.log('[WSClient] Initialized:', this.url);
    }

    /**
     * Get current connection state
     */
    get state() {
        return this._state;
    }

    /**
     * Check if connected
     */
    get isConnected() {
        return this._state === WSState.CONNECTED && 
               this._ws && 
               this._ws.readyState === WebSocket.OPEN;
    }

    /**
     * Get assigned peer ID
     */
    get peerId() {
        return this._peerId;
    }

    /**
     * Update connection state
     * @private
     */
    _setState(state) {
        if (this._state !== state) {
            this._state = state;
            console.log('[WSClient] State:', state);
            if (this.onStateChange) {
                this.onStateChange(state);
            }
        }
    }

    /**
     * Connect to the WebSocket server
     * @returns {Promise<boolean>} Success status
     */
    async connect() {
        if (this._state === WSState.CONNECTED) {
            return true;
        }
        
        this._setState(WSState.CONNECTING);
        
        return new Promise((resolve) => {
            try {
                this._ws = new WebSocket(this.url);
                this._ws.binaryType = 'arraybuffer';
                
                this._ws.onopen = () => {
                    console.log('[WSClient] Connection opened');
                    // Wait for welcome message before considering connected
                };
                
                this._ws.onmessage = (event) => {
                    this._handleMessage(event.data);
                    
                    // Check if this is the welcome message
                    if (this._state === WSState.CONNECTING && this._peerId) {
                        this._setState(WSState.CONNECTED);
                        this._reconnectCount = 0;
                        this._startPing();
                        
                        if (this.onOpen) {
                            this.onOpen();
                        }
                        
                        resolve(true);
                    }
                };
                
                this._ws.onerror = (event) => {
                    console.error('[WSClient] Error:', event);
                    if (this.onError) {
                        this.onError(new Error('WebSocket error'));
                    }
                };
                
                this._ws.onclose = (event) => {
                    console.log('[WSClient] Connection closed:', event.code, event.reason);
                    this._stopPing();
                    
                    if (this._state === WSState.CONNECTING) {
                        this._setState(WSState.FAILED);
                        resolve(false);
                    } else if (this._state === WSState.CONNECTED) {
                        this._handleDisconnect();
                    }
                    
                    if (this.onClose) {
                        this.onClose();
                    }
                };
                
                // Timeout for connection
                setTimeout(() => {
                    if (this._state === WSState.CONNECTING) {
                        console.error('[WSClient] Connection timeout');
                        this._ws.close();
                        this._setState(WSState.FAILED);
                        resolve(false);
                    }
                }, 10000);
                
            } catch (error) {
                console.error('[WSClient] Connection error:', error);
                this._setState(WSState.FAILED);
                resolve(false);
            }
        });
    }

    /**
     * Handle incoming message
     * @private
     */
    _handleMessage(data) {
        // Binary data (Twist message)
        if (data instanceof ArrayBuffer) {
            this.stats.messagesReceived++;
            this.stats.bytesReceived += data.byteLength;
            
            if (this.onMessage) {
                this.onMessage(data);
            }
            return;
        }
        
        // Text message (JSON)
        try {
            const msg = JSON.parse(data);
            
            switch (msg.type) {
                case 'welcome':
                    this._peerId = msg.peer_id;
                    console.log('[WSClient] Welcome, peer ID:', this._peerId);
                    break;
                    
                case 'pong':
                    this._lastPongTime = Date.now();
                    const latency = msg.timestamp ? (Date.now() - msg.timestamp) : 0;
                    console.debug('[WSClient] Pong received, latency:', latency, 'ms');
                    break;
                    
                default:
                    console.log('[WSClient] Message:', msg);
            }
        } catch (e) {
            console.warn('[WSClient] Failed to parse message:', e);
        }
    }

    /**
     * Handle disconnection and attempt reconnect
     * @private
     */
    async _handleDisconnect() {
        this._setState(WSState.RECONNECTING);
        
        for (let attempt = 0; attempt < this.maxReconnectAttempts; attempt++) {
            this._reconnectCount++;
            this.stats.reconnectCount++;
            
            console.log(`[WSClient] Reconnect attempt ${attempt + 1}/${this.maxReconnectAttempts}`);
            
            await new Promise(r => setTimeout(r, this.reconnectDelay * (attempt + 1)));
            
            if (await this.connect()) {
                console.log('[WSClient] Reconnected successfully');
                return;
            }
        }
        
        console.error('[WSClient] Max reconnection attempts reached');
        this._setState(WSState.FAILED);
    }

    /**
     * Start ping/pong keepalive
     * @private
     */
    _startPing() {
        this._stopPing();
        
        this._pingTimer = setInterval(() => {
            if (this.isConnected) {
                const ping = JSON.stringify({
                    type: 'ping',
                    timestamp: Date.now()
                });
                this._ws.send(ping);
                console.debug('[WSClient] Ping sent');
            }
        }, this.pingInterval);
    }

    /**
     * Stop ping/pong keepalive
     * @private
     */
    _stopPing() {
        if (this._pingTimer) {
            clearInterval(this._pingTimer);
            this._pingTimer = null;
        }
    }

    /**
     * Send binary data (Twist message)
     * @param {ArrayBuffer} data - Binary data to send
     * @returns {boolean} Success status
     */
    send(data) {
        if (!this.isConnected) {
            return false;
        }
        
        try {
            this._ws.send(data);
            this.stats.messagesSent++;
            this.stats.bytesSent += data.byteLength;
            return true;
        } catch (error) {
            console.error('[WSClient] Send error:', error);
            return false;
        }
    }

    /**
     * Send JSON message
     * @param {Object} data - Object to send as JSON
     * @returns {boolean} Success status
     */
    sendJson(data) {
        if (!this.isConnected) {
            return false;
        }
        
        try {
            this._ws.send(JSON.stringify(data));
            return true;
        } catch (error) {
            console.error('[WSClient] Send JSON error:', error);
            return false;
        }
    }

    /**
     * Close the connection
     */
    close() {
        console.log('[WSClient] Closing connection');
        this._setState(WSState.DISCONNECTED);
        this._stopPing();
        
        if (this._ws) {
            this._ws.close();
            this._ws = null;
        }
        
        this._peerId = null;
    }
}

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { WSDataClient, WSState };
}
