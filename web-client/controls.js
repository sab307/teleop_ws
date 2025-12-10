/**
 * Robot Controls Module
 * =====================
 * 
 * Keyboard and button-based robot teleoperation.
 * 
 * Behavior:
 *   - Commands are sent continuously while keys/buttons are held
 *   - When released, a STOP command (zero velocity) is sent automatically
 *   - Multiple keys can be pressed simultaneously
 *   - Visual feedback shows active controls
 * 
 * Key Mappings:
 *   Arrow Up / W    → Forward  (linear.y = +speed)
 *   Arrow Down / S  → Backward (linear.y = -speed)
 *   Arrow Left / A  → Left     (angular.z = +rate)
 *   Arrow Right / D → Right    (angular.z = -rate)
 *   Space           → Emergency Stop
 * 
 * @module RobotControls
 */

/**
 * Control command types
 */
const ControlCommand = {
    FORWARD: 'forward',
    BACKWARD: 'backward',
    LEFT: 'left',
    RIGHT: 'right',
    STOP: 'stop'
};

/**
 * Key mappings for keyboard control
 */
const KEY_MAPPINGS = {
    // Arrow keys
    'ArrowUp': ControlCommand.FORWARD,
    'ArrowDown': ControlCommand.BACKWARD,
    'ArrowLeft': ControlCommand.LEFT,
    'ArrowRight': ControlCommand.RIGHT,
    // WASD keys (by code)
    'KeyW': ControlCommand.FORWARD,
    'KeyS': ControlCommand.BACKWARD,
    'KeyA': ControlCommand.LEFT,
    'KeyD': ControlCommand.RIGHT,
};

/**
 * Robot controls handler with continuous key press support
 */
class RobotControls {
    /**
     * Create robot controls
     * @param {Object} options - Configuration options
     * @param {number} [options.linearSpeed=1.0] - Linear velocity (m/s)
     * @param {number} [options.angularSpeed=1.0] - Angular velocity (rad/s)
     * @param {number} [options.sendRate=20] - Command send rate (Hz)
     * @param {Function} [options.onCommand] - Callback for velocity commands
     */
    constructor(options = {}) {
        this.linearSpeed = options.linearSpeed || 1.0;
        this.angularSpeed = options.angularSpeed || 1.0;
        this.sendRate = options.sendRate || 20;
        this.onCommand = options.onCommand || null;

        // Currently active commands (keys being held down)
        this._activeCommands = new Set();
        
        // Continuous send timer
        this._sendInterval = null;
        this._sendPeriod = 1000 / this.sendRate;
        
        // Last sent velocity (to avoid sending duplicates)
        this._lastVelocity = { linearY: 0, angularZ: 0 };

        // Bound handlers for cleanup
        this._boundKeyDown = this._handleKeyDown.bind(this);
        this._boundKeyUp = this._handleKeyUp.bind(this);
        this._boundBlur = this._handleBlur.bind(this);

        // Button elements
        this._buttons = {};
        
        // Key display elements (for visual feedback)
        this._keyDisplays = {};

        console.log('[Controls] Initialized:', {
            linearSpeed: this.linearSpeed,
            angularSpeed: this.angularSpeed,
            sendRate: this.sendRate
        });
    }

    /**
     * Initialize controls and bind to DOM elements
     * @param {Object} buttonIds - Button element IDs
     */
    init(buttonIds) {
        // Get button elements
        this._buttons = {
            forward: document.getElementById(buttonIds.forward),
            backward: document.getElementById(buttonIds.backward),
            left: document.getElementById(buttonIds.left),
            right: document.getElementById(buttonIds.right)
        };

        // Bind button events (mouse and touch)
        this._bindButtonEvents(this._buttons.forward, ControlCommand.FORWARD);
        this._bindButtonEvents(this._buttons.backward, ControlCommand.BACKWARD);
        this._bindButtonEvents(this._buttons.left, ControlCommand.LEFT);
        this._bindButtonEvents(this._buttons.right, ControlCommand.RIGHT);

        // Bind keyboard events to document
        document.addEventListener('keydown', this._boundKeyDown);
        document.addEventListener('keyup', this._boundKeyUp);
        
        // Handle window blur (stop all when window loses focus)
        window.addEventListener('blur', this._boundBlur);

        console.log('[Controls] Event listeners bound');
    }

    /**
     * Set key display elements for visual feedback
     * @param {Object} displayIds - Key display element IDs
     */
    setKeyDisplays(displayIds) {
        this._keyDisplays = {
            forward: document.getElementById(displayIds.forward),
            backward: document.getElementById(displayIds.backward),
            left: document.getElementById(displayIds.left),
            right: document.getElementById(displayIds.right)
        };
    }

    /**
     * Bind mouse and touch events to a button
     * @private
     */
    _bindButtonEvents(button, command) {
        if (!button) return;

        // Mouse events
        button.addEventListener('mousedown', (e) => {
            e.preventDefault();
            this._activateCommand(command);
        });

        button.addEventListener('mouseup', () => {
            this._deactivateCommand(command);
        });

        button.addEventListener('mouseleave', () => {
            this._deactivateCommand(command);
        });

        // Touch events
        button.addEventListener('touchstart', (e) => {
            e.preventDefault();
            this._activateCommand(command);
        });

        button.addEventListener('touchend', (e) => {
            e.preventDefault();
            this._deactivateCommand(command);
        });

        button.addEventListener('touchcancel', () => {
            this._deactivateCommand(command);
        });
    }

    /**
     * Handle keyboard key down - activate command
     * @private
     */
    _handleKeyDown(event) {
        // Don't capture if typing in input field
        if (event.target.tagName === 'INPUT' || event.target.tagName === 'TEXTAREA') {
            return;
        }

        // Get command for this key
        const command = KEY_MAPPINGS[event.code] || KEY_MAPPINGS[event.key];
        
        if (command && !event.repeat) {
            event.preventDefault();
            this._activateCommand(command);
            console.log(`[Controls] Key pressed: ${event.code} -> ${command}`);
        }
    }

    /**
     * Handle keyboard key up - deactivate command and send stop if no keys held
     * @private
     */
    _handleKeyUp(event) {
        const command = KEY_MAPPINGS[event.code] || KEY_MAPPINGS[event.key];
        
        if (command) {
            event.preventDefault();
            this._deactivateCommand(command);
            console.log(`[Controls] Key released: ${event.code} -> ${command}`);
        }
    }

    /**
     * Handle window blur - stop all commands
     * @private
     */
    _handleBlur() {
        console.log('[Controls] Window lost focus - stopping all');
        this.stop();
    }

    /**
     * Activate a control command
     * @private
     */
    _activateCommand(command) {
        if (this._activeCommands.has(command)) return;

        this._activeCommands.add(command);
        this._updateVisuals(command, true);
        
        // Start continuous sending if not already running
        this._startContinuousSend();
        
        // Send immediately
        this._sendCurrentVelocity();
    }

    /**
     * Deactivate a control command
     * @private
     */
    _deactivateCommand(command) {
        if (!this._activeCommands.has(command)) return;

        this._activeCommands.delete(command);
        this._updateVisuals(command, false);
        
        // Send updated velocity (or stop if no commands active)
        this._sendCurrentVelocity();
        
        // Stop continuous sending if no commands active
        if (this._activeCommands.size === 0) {
            this._stopContinuousSend();
        }
    }

    /**
     * Update visual state of buttons and key displays
     * @private
     */
    _updateVisuals(command, active) {
        // Update button
        const button = this._buttons[command];
        if (button) {
            if (active) {
                button.classList.add('active');
            } else {
                button.classList.remove('active');
            }
        }

        // Update key display
        const keyDisplay = this._keyDisplays[command];
        if (keyDisplay) {
            if (active) {
                keyDisplay.classList.add('key-active');
            } else {
                keyDisplay.classList.remove('key-active');
            }
        }
    }

    /**
     * Start continuous command sending
     * @private
     */
    _startContinuousSend() {
        if (this._sendInterval) return;
        
        this._sendInterval = setInterval(() => {
            this._sendCurrentVelocity();
        }, this._sendPeriod);
    }

    /**
     * Stop continuous command sending
     * @private
     */
    _stopContinuousSend() {
        if (this._sendInterval) {
            clearInterval(this._sendInterval);
            this._sendInterval = null;
        }
    }

    /**
     * Calculate current velocity based on active commands
     * @returns {Object} Velocity values {linearY, angularZ}
     */
    getCurrentVelocity() {
        let linearY = 0;
        let angularZ = 0;

        for (const command of this._activeCommands) {
            switch (command) {
                case ControlCommand.FORWARD:
                    linearY += this.linearSpeed;
                    break;
                case ControlCommand.BACKWARD:
                    linearY -= this.linearSpeed;
                    break;
                case ControlCommand.LEFT:
                    angularZ += this.angularSpeed;
                    break;
                case ControlCommand.RIGHT:
                    angularZ -= this.angularSpeed;
                    break;
            }
        }

        return { linearY, angularZ };
    }

    /**
     * Send current velocity via callback
     * @private
     */
    _sendCurrentVelocity() {
        const velocity = this.getCurrentVelocity();
        
        // Always send (even if same as last) to maintain control
        this._lastVelocity = { ...velocity };

        if (this.onCommand) {
            this.onCommand(velocity);
        }
    }

    /**
     * Stop all motion - clear all commands and send zero velocity
     */
    stop() {
        // Clear all active commands
        this._activeCommands.clear();
        
        // Stop continuous sending
        this._stopContinuousSend();
        
        // Clear all button states
        for (const command of Object.keys(this._buttons)) {
            this._updateVisuals(command, false);
        }

        // Send stop command (zero velocity)
        this._lastVelocity = { linearY: 0, angularZ: 0 };
        
        if (this.onCommand) {
            this.onCommand({ linearY: 0, angularZ: 0 });
        }

        console.log('[Controls] All commands stopped');
    }

    /**
     * Set velocity speeds
     * @param {number} linear - Linear speed (m/s)
     * @param {number} angular - Angular speed (rad/s)
     */
    setSpeed(linear, angular) {
        this.linearSpeed = linear;
        this.angularSpeed = angular;
        console.log(`[Controls] Speed updated: linear=${linear}, angular=${angular}`);
    }

    /**
     * Update send rate
     * @param {number} rate - Send rate in Hz
     */
    setSendRate(rate) {
        this.sendRate = rate;
        this._sendPeriod = 1000 / rate;
        
        // Restart interval if running
        if (this._sendInterval) {
            this._stopContinuousSend();
            this._startContinuousSend();
        }
    }

    /**
     * Check if any command is active
     * @returns {boolean}
     */
    get isActive() {
        return this._activeCommands.size > 0;
    }

    /**
     * Get list of active commands
     * @returns {string[]}
     */
    get activeCommands() {
        return Array.from(this._activeCommands);
    }

    /**
     * Cleanup event listeners
     */
    destroy() {
        document.removeEventListener('keydown', this._boundKeyDown);
        document.removeEventListener('keyup', this._boundKeyUp);
        window.removeEventListener('blur', this._boundBlur);
        
        this.stop();
        
        console.log('[Controls] Destroyed');
    }
}

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { RobotControls, ControlCommand, KEY_MAPPINGS };
}
