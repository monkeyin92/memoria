package mediaedge

import (
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// JWTVerifier validates short-lived media tokens. EdDSA public keys are the
// production path; HS256 remains only as an explicit development fallback so
// existing local fixtures do not silently change semantics.
type JWTVerifier struct {
	Secret     []byte
	PublicKey  ed25519.PublicKey
	PublicKeys map[string]ed25519.PublicKey
	KeyID      string
	Issuer     string
	Audience   string
	MaxTTL     time.Duration
	ClockSkew  time.Duration
	Now        func() time.Time
}

// ParseEd25519PublicKeys accepts a small JWKS document containing Ed25519
// signing keys. The returned map is keyed by kid and supports overlap during
// rotation without giving the media edge a private key.
func ParseEd25519PublicKeys(material []byte) (map[string]ed25519.PublicKey, error) {
	trimmed := strings.TrimSpace(string(material))
	if trimmed == "" {
		return nil, fmt.Errorf("media public key material is empty")
	}
	if !strings.HasPrefix(trimmed, "{") {
		return nil, fmt.Errorf("media public key must be a JWKS document")
	}
	var document struct {
		Keys []struct {
			Kty string `json:"kty"`
			Crv string `json:"crv"`
			Kid string `json:"kid"`
			Alg string `json:"alg"`
			Use string `json:"use"`
			X   string `json:"x"`
		} `json:"keys"`
	}
	if err := json.Unmarshal([]byte(trimmed), &document); err != nil {
		return nil, fmt.Errorf("invalid media JWKS: %w", err)
	}
	keys := make(map[string]ed25519.PublicKey, len(document.Keys))
	for _, key := range document.Keys {
		if key.Kty != "OKP" || key.Crv != "Ed25519" || key.Kid == "" ||
			(key.Alg != "" && key.Alg != "EdDSA") ||
			(key.Use != "" && key.Use != "sig") {
			continue
		}
		decoded, err := base64.RawURLEncoding.DecodeString(key.X)
		if err != nil || len(decoded) != ed25519.PublicKeySize {
			continue
		}
		keys[key.Kid] = ed25519.PublicKey(decoded)
	}
	if len(keys) == 0 {
		return nil, fmt.Errorf("media JWKS has no usable Ed25519 signing key")
	}
	return keys, nil
}

// MediaTokenIdentity is the account/device/client binding carried by a
// short-lived media token. The edge compares these values with the session
// request rather than trusting a client-selected body identity.
type MediaTokenIdentity struct {
	SessionID   string
	AccountID   string
	DeviceID    string
	ClientType  string
	StreamEpoch uint64
}

func (v JWTVerifier) Verify(token, sessionID string, expectedEpoch ...uint64) error {
	identity := MediaTokenIdentity{SessionID: sessionID}
	if len(expectedEpoch) > 0 {
		identity.StreamEpoch = expectedEpoch[0]
	}
	return v.VerifyIdentity(token, identity)
}

// ParseIdentity authenticates a bearer token and returns the identity needed
// to create a WHIP session. Claims are decoded once to discover the expected
// identity, then VerifyIdentity validates the signature, issuer, audience,
// expiry and every discovered field before any value is returned.
func (v JWTVerifier) ParseIdentity(token string) (MediaTokenIdentity, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return MediaTokenIdentity{}, fmt.Errorf("invalid media token")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return MediaTokenIdentity{}, fmt.Errorf("invalid media token claims")
	}
	var claims struct {
		SessionID   string `json:"session_id"`
		Subject     string `json:"sub"`
		DeviceID    string `json:"device_id"`
		ClientType  string `json:"client_type"`
		StreamEpoch uint64 `json:"stream_epoch"`
	}
	if err := json.Unmarshal(payload, &claims); err != nil {
		return MediaTokenIdentity{}, fmt.Errorf("invalid media token claims")
	}
	identity := MediaTokenIdentity{
		SessionID: claims.SessionID, AccountID: claims.Subject, DeviceID: claims.DeviceID,
		ClientType: claims.ClientType, StreamEpoch: claims.StreamEpoch,
	}
	if err := v.VerifyIdentity(token, identity); err != nil {
		return MediaTokenIdentity{}, err
	}
	if err := OpenSessionRequest(identity).Validate(); err != nil {
		return MediaTokenIdentity{}, fmt.Errorf("invalid media token identity: %w", err)
	}
	return identity, nil
}

// VerifyIdentity validates the token and, when supplied, binds all account,
// device, client-type and stream-epoch claims to the current media identity.
func (v JWTVerifier) VerifyIdentity(token string, expected MediaTokenIdentity) error {
	if token == "" || (len(v.PublicKey) != ed25519.PublicKeySize &&
		len(v.PublicKeys) == 0 && len(v.Secret) < 32) {
		return fmt.Errorf("media token verifier is not configured")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return fmt.Errorf("invalid media token")
	}
	decode := func(value string, target any) error {
		data, err := base64.RawURLEncoding.DecodeString(value)
		if err != nil {
			return err
		}
		return json.Unmarshal(data, target)
	}
	var header struct {
		Alg string `json:"alg"`
		Typ string `json:"typ"`
		Kid string `json:"kid"`
	}
	if err := decode(parts[0], &header); err != nil || header.Typ != "JWT" {
		return fmt.Errorf("unsupported media token header")
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return fmt.Errorf("invalid media token signature")
	}
	signed := []byte(parts[0] + "." + parts[1])
	switch header.Alg {
	case "EdDSA":
		key := v.PublicKey
		if len(v.PublicKeys) > 0 {
			if header.Kid == "" {
				return fmt.Errorf("media token key id is missing")
			}
			key = v.PublicKeys[header.Kid]
		} else if v.KeyID != "" && header.Kid != v.KeyID {
			return fmt.Errorf("media token key id mismatch")
		}
		if len(key) != ed25519.PublicKeySize || !ed25519.Verify(key, signed, signature) {
			return fmt.Errorf("invalid media token signature")
		}
	case "HS256":
		if len(v.PublicKey) > 0 || len(v.PublicKeys) > 0 || len(v.Secret) < 32 {
			return fmt.Errorf("legacy media token algorithm is not allowed")
		}
		mac := hmac.New(sha256.New, v.Secret)
		_, _ = mac.Write(signed)
		if !hmac.Equal(signature, mac.Sum(nil)) {
			return fmt.Errorf("invalid media token signature")
		}
	default:
		return fmt.Errorf("unsupported media token algorithm")
	}
	var claims struct {
		Issuer      string `json:"iss"`
		Audience    string `json:"aud"`
		SessionID   string `json:"session_id"`
		Subject     string `json:"sub"`
		DeviceID    string `json:"device_id"`
		ClientType  string `json:"client_type"`
		StreamEpoch uint64 `json:"stream_epoch"`
		Expiry      int64  `json:"exp"`
		NotBefore   int64  `json:"nbf"`
		IssuedAt    int64  `json:"iat"`
	}
	if err := decode(parts[1], &claims); err != nil {
		return fmt.Errorf("invalid media token claims")
	}
	if (v.Issuer != "" && claims.Issuer != v.Issuer) ||
		(v.Audience != "" && claims.Audience != v.Audience) {
		return fmt.Errorf("media token issuer or audience mismatch")
	}
	if expected.SessionID == "" || claims.SessionID != expected.SessionID || claims.Subject == "" {
		return fmt.Errorf("media token session mismatch")
	}
	if expected.AccountID != "" && claims.Subject != expected.AccountID {
		return fmt.Errorf("media token account mismatch")
	}
	if expected.DeviceID != "" && claims.DeviceID != expected.DeviceID {
		return fmt.Errorf("media token device mismatch")
	}
	if expected.ClientType != "" && claims.ClientType != expected.ClientType {
		return fmt.Errorf("media token client type mismatch")
	}
	if expected.StreamEpoch != 0 && claims.StreamEpoch != expected.StreamEpoch {
		return fmt.Errorf("media token stream epoch mismatch")
	}
	now := time.Now()
	if v.Now != nil {
		now = v.Now()
	}
	skew := v.ClockSkew
	if skew < 0 {
		skew = 0
	}
	if claims.Expiry == 0 || time.Unix(claims.Expiry, 0).Add(skew).Before(now) {
		return fmt.Errorf("media token expired")
	}
	if claims.NotBefore != 0 && time.Unix(claims.NotBefore, 0).After(now.Add(skew)) {
		return fmt.Errorf("media token is not active")
	}
	if claims.IssuedAt != 0 && time.Unix(claims.IssuedAt, 0).After(now.Add(skew)) {
		return fmt.Errorf("media token issued in the future")
	}
	if v.MaxTTL > 0 && claims.IssuedAt != 0 &&
		time.Unix(claims.Expiry, 0).Sub(time.Unix(claims.IssuedAt, 0)) > v.MaxTTL {
		return fmt.Errorf("media token ttl exceeds limit")
	}
	return nil
}
