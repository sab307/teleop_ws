/**
 * Twist Protocol Module
 * =====================
 * 
 * Binary encoding/decoding for ROS2 geometry_msgs/Twist messages.
 * 
 * Binary Format (56 bytes, little-endian IEEE 754 double-precision):
 *   Offset  Size    Type      Field
 *   ─────────────────────────────────
 *   0       8       float64   linear.x
 *   8       8       float64   linear.y
 *   16      8       float64   linear.z
 *   24      8       float64   angular.x
 *   32      8       float64   angular.y
 *   40      8       float64   angular.z
 *   48      8       uint64    timestamp (ms since epoch)
 *   ─────────────────────────────────
 *   Total: 56 bytes
 * 
 * @module TwistProtocol
 */

// Message size constants
const TWIST_SIZE = 56;          // New size with timestamp
const TWIST_SIZE_LEGACY = 48;   // Legacy size without timestamp

/**
 * TwistMessage class representing ROS2 geometry_msgs/Twist
 */
class TwistMessage {
    /**
     * Create a TwistMessage
     * @param {Object} options - Velocity components
     * @param {number} [options.linearX=0] - Linear X velocity (m/s)
     * @param {number} [options.linearY=0] - Linear Y velocity (m/s)
     * @param {number} [options.linearZ=0] - Linear Z velocity (m/s)
     * @param {number} [options.angularX=0] - Angular X velocity (rad/s)
     * @param {number} [options.angularY=0] - Angular Y velocity (rad/s)
     * @param {number} [options.angularZ=0] - Angular Z velocity (rad/s)
     * @param {number} [options.timestamp=0] - Timestamp in ms (0 = auto-set on encode)
     */
    constructor({
        linearX = 0,
        linearY = 0,
        linearZ = 0,
        angularX = 0,
        angularY = 0,
        angularZ = 0,
        timestamp = 0
    } = {}) {
        this.linear = {
            x: linearX,
            y: linearY,
            z: linearZ
        };
        this.angular = {
            x: angularX,
            y: angularY,
            z: angularZ
        };
        this.timestamp = timestamp;
    }

    /**
     * Encode the Twist message to binary format
     * @param {boolean} [includeTimestamp=true] - Whether to include timestamp
     * @returns {ArrayBuffer} 56-byte binary representation
     */
    encode(includeTimestamp = true) {
        const size = includeTimestamp ? TWIST_SIZE : TWIST_SIZE_LEGACY;
        const buffer = new ArrayBuffer(size);
        const view = new DataView(buffer);
        
        // Little-endian encoding
        view.setFloat64(0, this.linear.x, true);
        view.setFloat64(8, this.linear.y, true);
        view.setFloat64(16, this.linear.z, true);
        view.setFloat64(24, this.angular.x, true);
        view.setFloat64(32, this.angular.y, true);
        view.setFloat64(40, this.angular.z, true);
        
        if (includeTimestamp) {
            // Set timestamp - if 0, use current time
            const ts = this.timestamp || Date.now();
            // Write as two 32-bit integers (JavaScript doesn't have native 64-bit int)
            view.setUint32(48, ts & 0xFFFFFFFF, true);        // Low 32 bits
            view.setUint32(52, Math.floor(ts / 0x100000000), true);  // High 32 bits
        }
        
        return buffer;
    }

    /**
     * Decode binary data into a TwistMessage
     * @param {ArrayBuffer} buffer - 48 or 56-byte binary data
     * @returns {TwistMessage} Decoded message
     * @throws {Error} If buffer size is invalid
     */
    static decode(buffer) {
        if (buffer.byteLength !== TWIST_SIZE && buffer.byteLength !== TWIST_SIZE_LEGACY) {
            throw new Error(
                `Invalid twist message size: expected ${TWIST_SIZE} or ${TWIST_SIZE_LEGACY} bytes, ` +
                `got ${buffer.byteLength} bytes`
            );
        }
        
        const view = new DataView(buffer);
        
        let timestamp = 0;
        if (buffer.byteLength >= TWIST_SIZE) {
            // Read timestamp as two 32-bit integers
            const low = view.getUint32(48, true);
            const high = view.getUint32(52, true);
            timestamp = low + (high * 0x100000000);
        }
        
        return new TwistMessage({
            linearX: view.getFloat64(0, true),
            linearY: view.getFloat64(8, true),
            linearZ: view.getFloat64(16, true),
            angularX: view.getFloat64(24, true),
            angularY: view.getFloat64(32, true),
            angularZ: view.getFloat64(40, true),
            timestamp: timestamp
        });
    }

    /**
     * Create a zero velocity message (stop/emergency stop command)
     * @returns {TwistMessage}
     */
    static zero() {
        return new TwistMessage({ timestamp: Date.now() });
    }

    /**
     * Create an emergency stop message (same as zero but explicit)
     * @returns {TwistMessage}
     */
    static emergencyStop() {
        return new TwistMessage({ timestamp: Date.now() });
    }

    /**
     * Create a forward motion command
     * @param {number} [speed=1.0] - Forward speed in m/s
     * @returns {TwistMessage}
     */
    static forward(speed = 1.0) {
        return new TwistMessage({ linearY: Math.abs(speed), timestamp: Date.now() });
    }

    /**
     * Create a backward motion command
     * @param {number} [speed=1.0] - Backward speed in m/s
     * @returns {TwistMessage}
     */
    static backward(speed = 1.0) {
        return new TwistMessage({ linearY: -Math.abs(speed), timestamp: Date.now() });
    }

    /**
     * Create a left turn command
     * @param {number} [rate=1.0] - Turn rate in rad/s
     * @returns {TwistMessage}
     */
    static turnLeft(rate = 1.0) {
        return new TwistMessage({ angularZ: Math.abs(rate), timestamp: Date.now() });
    }

    /**
     * Create a right turn command
     * @param {number} [rate=1.0] - Turn rate in rad/s
     * @returns {TwistMessage}
     */
    static turnRight(rate = 1.0) {
        return new TwistMessage({ angularZ: -Math.abs(rate), timestamp: Date.now() });
    }

    /**
     * Check if this is a stop command (all zeros)
     * @returns {boolean}
     */
    isZero() {
        return this.linear.x === 0 && this.linear.y === 0 && this.linear.z === 0 &&
               this.angular.x === 0 && this.angular.y === 0 && this.angular.z === 0;
    }

    /**
     * Calculate latency from this message's timestamp to now
     * @returns {number} Latency in milliseconds
     */
    getLatency() {
        if (this.timestamp === 0) return 0;
        return Date.now() - this.timestamp;
    }

    /**
     * Get string representation
     * @returns {string}
     */
    toString() {
        const lin = this.linear;
        const ang = this.angular;
        return `Twist{linear: [${lin.x.toFixed(3)}, ${lin.y.toFixed(3)}, ${lin.z.toFixed(3)}], ` +
               `angular: [${ang.x.toFixed(3)}, ${ang.y.toFixed(3)}, ${ang.z.toFixed(3)}], ` +
               `ts: ${this.timestamp}}`;
    }

    /**
     * Clone the message
     * @returns {TwistMessage}
     */
    clone() {
        return new TwistMessage({
            linearX: this.linear.x,
            linearY: this.linear.y,
            linearZ: this.linear.z,
            angularX: this.angular.x,
            angularY: this.angular.y,
            angularZ: this.angular.z,
            timestamp: this.timestamp
        });
    }
}

/**
 * Validate that data is a valid Twist message
 * @param {ArrayBuffer} buffer - Data to validate
 * @returns {boolean}
 */
function validateTwistData(buffer) {
    return buffer && (buffer.byteLength === TWIST_SIZE || buffer.byteLength === TWIST_SIZE_LEGACY);
}

/**
 * Quick encode function for direct values
 * @param {number} linearY - Linear Y velocity
 * @param {number} angularZ - Angular Z velocity
 * @returns {ArrayBuffer}
 */
function encodeTwist(linearY = 0, angularZ = 0) {
    return new TwistMessage({ linearY, angularZ, timestamp: Date.now() }).encode();
}

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        TwistMessage,
        TWIST_SIZE,
        TWIST_SIZE_LEGACY,
        validateTwistData,
        encodeTwist
    };
}
