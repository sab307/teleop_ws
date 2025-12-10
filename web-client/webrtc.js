/**
 * WebRTC Client Module
 * ====================
 * 
 * Browser-based WebRTC client for connecting to the Go relay server.
 * 
 * Features:
 *   - Automatic connection and reconnection
 *   - DataChannel message handling
 *   - Binary message support
 *   - Connection state monitoring
 *   - Event-based callbacks
 * 
 * @module WebRTCClient
 * 
 * @example
 * const client = new WebRTCClient('http://localhost:8080');
 * client.onMessage = (data) => console.log('Received:', data);
 * await client.connect();
 * client.send(twistMessage.encode());
 */

/**
 * Connection states
 * @enum {string}
 */
const ConnectionState = {
    DISCONNECTED: 'disconnected',
    CONNECTING: 'connecting',
    CONNECTED: 'connected',
    FAILED: 'failed',
    CLOSED: 'closed'
};

/**
 * WebRTC client for DataChannel communication
 */
class WebRTCClient {
    /**
     * Create a WebRTC client
     * @param {string} relayUrl - URL of the Go relay server
     * @param {Object} [options] - Configuration options
     * @param {string[]} [options.stunServers] - STUN server URLs
     * @param {string} [options.dataChannelLabel] - DataChannel label
     * @param {number} [options.reconnectAttempts] - Number of reconnection attempts
     * @param {number} [options.reconnectDelay] - Delay between reconnection attempts (ms)
     */
    constructor(relayUrl = 'http://localhost:8080', options = {}) {
        this.relayUrl = relayUrl;
        this.options = {
            stunServers: options.stunServers || [
                'stun:stun.l.google.com:19302',
                'stun:stun1.l.google.com:19302',
                'stun:stun2.l.google.com:19302',
                'stun:stun.services.mozilla.com'
            ],
            dataChannelLabel: options.dataChannelLabel || 'twist',
            reconnectAttempts: options.reconnectAttempts || 3,
            reconnectDelay: options.reconnectDelay || 2000,
            ...options
        };

        // Connection state
        this._state = ConnectionState.DISCONNECTED;
        this._peerId = null;

        // WebRTC components
        this._pc = null;
        this._dc = null;

        // Callbacks
        this.onMessage = null;
        this.onStateChange = null;
        this.onError = null;
        this.onOpen = null;
        this.onClose = null;

        // Statistics
        this._stats = {
            messagesSent: 0,
            messagesReceived: 0,
            bytesReceived: 0,
            bytesSent: 0,
            connectTime: null
        };

        console.log(`[WebRTC] Client created for ${relayUrl}`);
    }

    /**
     * Get current connection state
     * @returns {string}
     */
    get state() {
        return this._state;
    }

    /**
     * Get assigned peer ID
     * @returns {string|null}
     */
    get peerId() {
        return this._peerId;
    }

    /**
     * Check if connected and ready to send
     * @returns {boolean}
     */
    get isConnected() {
        return this._state === ConnectionState.CONNECTED &&
               this._dc !== null &&
               this._dc.readyState === 'open';
    }

    /**
     * Get connection statistics
     * @returns {Object}
     */
    get stats() {
        return { ...this._stats };
    }

    /**
     * Set connection state and fire callback
     * @private
     */
    _setState(state) {
        if (this._state !== state) {
            const oldState = this._state;
            this._state = state;
            console.log(`[WebRTC] State: ${oldState} -> ${state}`);
            
            if (this.onStateChange) {
                this.onStateChange(state, oldState);
            }
        }
    }

    /**
     * Connect to the relay server
     * @returns {Promise<boolean>} True if connected successfully
     */
    async connect() {
        if (this._state === ConnectionState.CONNECTED) {
            console.log('[WebRTC] Already connected');
            return true;
        }

        this._setState(ConnectionState.CONNECTING);

        try {
            // Check WebRTC support
            if (typeof RTCPeerConnection === 'undefined') {
                throw new Error('WebRTC is not supported in this browser');
            }

            // Test server connectivity first
            try {
                console.log(`[WebRTC] Checking server at ${this.relayUrl}...`);
                const healthCheck = await fetch(`${this.relayUrl}/health`, { 
                    method: 'GET',
                    mode: 'cors'
                });
                if (!healthCheck.ok) {
                    throw new Error(`Server returned ${healthCheck.status}`);
                }
                console.log('[WebRTC] Server health check passed');
            } catch (e) {
                throw new Error(`Cannot reach server at ${this.relayUrl}: ${e.message}`);
            }

            // Create RTCPeerConnection
            const config = {
                iceServers: this.options.stunServers.map(url => ({ urls: url }))
            };
            console.log('[WebRTC] Creating peer connection...');
            this._pc = new RTCPeerConnection(config);
            this._setupPeerConnection();

            // Create DataChannel
            this._dc = this._pc.createDataChannel(this.options.dataChannelLabel, {
                ordered: true
            });
            this._setupDataChannel();

            // Create offer
            console.log('[WebRTC] Creating offer...');
            const offer = await this._pc.createOffer();
            await this._pc.setLocalDescription(offer);

            // Wait for ICE gathering
            console.log('[WebRTC] Gathering ICE candidates...');
            await this._waitForIceGathering();

            // Send offer to signaling server
            console.log('[WebRTC] Sending offer to signaling server...');
            const answer = await this._sendOffer();
            
            if (!answer) {
                throw new Error('Failed to get answer from server');
            }

            // Set remote description
            console.log('[WebRTC] Setting remote description...');
            await this._pc.setRemoteDescription(answer);

            // Wait for DataChannel to open
            console.log('[WebRTC] Waiting for DataChannel to open...');
            await this._waitForDataChannel();

            return true;

        } catch (error) {
            console.error('[WebRTC] Connection failed:', error);
            this._setState(ConnectionState.FAILED);
            
            if (this.onError) {
                this.onError(error);
            }
            
            return false;
        }
    }

    /**
     * Setup peer connection event handlers
     * @private
     */
    _setupPeerConnection() {
        this._pc.onconnectionstatechange = () => {
            const state = this._pc.connectionState;
            console.log(`[WebRTC] Connection state: ${state}`);

            switch (state) {
                case 'failed':
                    this._setState(ConnectionState.FAILED);
                    break;
                case 'closed':
                    this._setState(ConnectionState.CLOSED);
                    break;
            }
        };

        this._pc.oniceconnectionstatechange = () => {
            console.log(`[WebRTC] ICE state: ${this._pc.iceConnectionState}`);
        };

        this._pc.ondatachannel = (event) => {
            console.log(`[WebRTC] DataChannel received: ${event.channel.label}`);
            this._dc = event.channel;
            this._setupDataChannel();
        };
    }

    /**
     * Setup data channel event handlers
     * @private
     */
    _setupDataChannel() {
        this._dc.binaryType = 'arraybuffer';

        this._dc.onopen = () => {
            console.log(`[WebRTC] DataChannel '${this._dc.label}' opened`);
            this._stats.connectTime = Date.now();
            this._setState(ConnectionState.CONNECTED);
            
            if (this.onOpen) {
                this.onOpen();
            }
        };

        this._dc.onclose = () => {
            console.log(`[WebRTC] DataChannel '${this._dc.label}' closed`);
            
            if (this._state === ConnectionState.CONNECTED) {
                this._setState(ConnectionState.DISCONNECTED);
            }
            
            if (this.onClose) {
                this.onClose();
            }
        };

        this._dc.onerror = (event) => {
            console.error('[WebRTC] DataChannel error:', event);
            
            if (this.onError) {
                this.onError(new Error('DataChannel error'));
            }
        };

        this._dc.onmessage = (event) => {
            this._stats.messagesReceived++;
            
            let data = event.data;
            if (data instanceof ArrayBuffer) {
                this._stats.bytesReceived += data.byteLength;
            } else if (typeof data === 'string') {
                this._stats.bytesReceived += data.length;
                // Convert to ArrayBuffer for consistency
                const encoder = new TextEncoder();
                data = encoder.encode(data).buffer;
            }

            if (this.onMessage) {
                this.onMessage(data);
            }
        };
    }

    /**
     * Wait for ICE gathering to complete
     * @private
     * @returns {Promise<void>}
     */
    _waitForIceGathering() {
        return new Promise((resolve, reject) => {
            if (this._pc.iceGatheringState === 'complete') {
                resolve();
                return;
            }

            const timeout = setTimeout(() => {
                reject(new Error('ICE gathering timeout'));
            }, 10000);

            this._pc.onicegatheringstatechange = () => {
                if (this._pc.iceGatheringState === 'complete') {
                    clearTimeout(timeout);
                    resolve();
                }
            };
        });
    }

    /**
     * Wait for DataChannel to open
     * @private
     * @returns {Promise<void>}
     */
    _waitForDataChannel() {
        return new Promise((resolve, reject) => {
            if (this._dc && this._dc.readyState === 'open') {
                resolve();
                return;
            }

            const timeout = setTimeout(() => {
                reject(new Error('DataChannel open timeout'));
            }, 30000);

            const originalOnOpen = this._dc.onopen;
            this._dc.onopen = (event) => {
                clearTimeout(timeout);
                if (originalOnOpen) {
                    originalOnOpen(event);
                }
                resolve();
            };
        });
    }

    /**
     * Send SDP offer to signaling server
     * @private
     * @returns {Promise<RTCSessionDescription|null>}
     */
    async _sendOffer() {
        const url = `${this.relayUrl}/offer`;
        
        const payload = {
            sdp: this._pc.localDescription.sdp,
            type: 'offer',
            peerType: 'web'
        };

        try {
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.error(`[WebRTC] Signaling error: ${response.status} - ${errorText}`);
                return null;
            }

            const data = await response.json();
            
            this._peerId = data.peerID;
            console.log(`[WebRTC] Assigned peer ID: ${this._peerId}`);

            return new RTCSessionDescription({
                type: data.type,
                sdp: data.sdp
            });

        } catch (error) {
            console.error('[WebRTC] Signaling request failed:', error);
            return null;
        }
    }

    /**
     * Send binary data through the DataChannel
     * @param {ArrayBuffer} data - Binary data to send
     * @returns {boolean} True if sent successfully
     */
    send(data) {
        if (!this.isConnected) {
            console.warn('[WebRTC] Cannot send: not connected');
            return false;
        }

        try {
            this._dc.send(data);
            this._stats.messagesSent++;
            this._stats.bytesSent += data.byteLength;
            return true;
        } catch (error) {
            console.error('[WebRTC] Send error:', error);
            
            if (this.onError) {
                this.onError(error);
            }
            
            return false;
        }
    }

    /**
     * Send text data through the DataChannel
     * @param {string} text - Text to send
     * @returns {boolean} True if sent successfully
     */
    sendText(text) {
        const encoder = new TextEncoder();
        return this.send(encoder.encode(text).buffer);
    }

    /**
     * Close the WebRTC connection
     */
    close() {
        console.log('[WebRTC] Closing connection...');

        if (this._dc) {
            this._dc.close();
        }

        if (this._pc) {
            this._pc.close();
        }

        this._setState(ConnectionState.CLOSED);
        console.log('[WebRTC] Connection closed');
    }

    /**
     * Reconnect to the server
     * @returns {Promise<boolean>}
     */
    async reconnect() {
        this.close();
        await this._delay(this.options.reconnectDelay);
        return this.connect();
    }

    /**
     * Helper delay function
     * @private
     */
    _delay(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
}

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { WebRTCClient, ConnectionState };
}
