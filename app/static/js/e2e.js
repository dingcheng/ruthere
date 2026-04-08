/**
 * RUThere E2E Encryption Module
 * 
 * Uses the Web Crypto API (native browser, zero dependencies):
 * - PBKDF2 (600,000 iterations, SHA-256) for passphrase → key derivation
 * - AES-256-GCM for authenticated encryption
 * 
 * The passphrase and plaintext NEVER leave the browser.
 * Only ciphertext, salt, nonce, and tag are sent to the server.
 */

const E2E = {
    PBKDF2_ITERATIONS: 600000,
    SALT_BYTES: 16,
    NONCE_BYTES: 12,

    /**
     * Convert ArrayBuffer to base64 string.
     */
    bufToBase64(buf) {
        return btoa(String.fromCharCode(...new Uint8Array(buf)));
    },

    /**
     * Convert base64 string to ArrayBuffer.
     */
    base64ToBuf(b64) {
        const bin = atob(b64);
        const buf = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
        return buf.buffer;
    },

    /**
     * Derive an AES-256-GCM key from a passphrase and salt using PBKDF2.
     */
    async deriveKey(passphrase, salt) {
        const enc = new TextEncoder();
        const keyMaterial = await crypto.subtle.importKey(
            "raw",
            enc.encode(passphrase),
            "PBKDF2",
            false,
            ["deriveKey"]
        );
        return crypto.subtle.deriveKey(
            {
                name: "PBKDF2",
                salt: salt,
                iterations: E2E.PBKDF2_ITERATIONS,
                hash: "SHA-256",
            },
            keyMaterial,
            { name: "AES-GCM", length: 256 },
            false,
            ["encrypt", "decrypt"]
        );
    },

    /**
     * Encrypt plaintext with a passphrase.
     * 
     * Returns: { encrypted_content, encryption_nonce, encryption_tag, encryption_salt }
     * All values are base64-encoded strings ready for the API.
     */
    async encrypt(plaintext, passphrase) {
        const enc = new TextEncoder();
        const salt = crypto.getRandomValues(new Uint8Array(E2E.SALT_BYTES));
        const nonce = crypto.getRandomValues(new Uint8Array(E2E.NONCE_BYTES));
        const key = await E2E.deriveKey(passphrase, salt);

        const ciphertextWithTag = await crypto.subtle.encrypt(
            { name: "AES-GCM", iv: nonce },
            key,
            enc.encode(plaintext)
        );

        // AES-GCM output = ciphertext + 16-byte tag appended
        const ctBytes = new Uint8Array(ciphertextWithTag);
        const ciphertext = ctBytes.slice(0, ctBytes.length - 16);
        const tag = ctBytes.slice(ctBytes.length - 16);

        return {
            encrypted_content: E2E.bufToBase64(ciphertext),
            encryption_nonce: E2E.bufToBase64(nonce),
            encryption_tag: E2E.bufToBase64(tag),
            encryption_salt: E2E.bufToBase64(salt),
        };
    },

    /**
     * Decrypt ciphertext with a passphrase.
     * 
     * Parameters are base64-encoded strings (as stored by the server).
     * Returns the plaintext string.
     * Throws on wrong passphrase or tampered data.
     */
    async decrypt(encrypted_content, encryption_nonce, encryption_tag, encryption_salt, passphrase) {
        const ciphertext = new Uint8Array(E2E.base64ToBuf(encrypted_content));
        const nonce = new Uint8Array(E2E.base64ToBuf(encryption_nonce));
        const tag = new Uint8Array(E2E.base64ToBuf(encryption_tag));
        const salt = new Uint8Array(E2E.base64ToBuf(encryption_salt));

        const key = await E2E.deriveKey(passphrase, salt);

        // Reconstruct ciphertext + tag (AES-GCM expects them concatenated)
        const combined = new Uint8Array(ciphertext.length + tag.length);
        combined.set(ciphertext);
        combined.set(tag, ciphertext.length);

        const plainBuf = await crypto.subtle.decrypt(
            { name: "AES-GCM", iv: nonce },
            key,
            combined
        );

        return new TextDecoder().decode(plainBuf);
    },
};
