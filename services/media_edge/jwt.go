package mediaedge

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// JWTVerifier is a minimal HS256 verifier for the reference edge.  Production
// deployments may replace it with a JWKS verifier, while preserving the same
// audience/session claim checks.
type JWTVerifier struct {
	Secret   []byte
	Issuer   string
	Audience string
	Now      func() time.Time
}

// MediaTokenIdentity is the account/device/client binding carried by a
// short-lived media token.  The edge compares these values with the session
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

// VerifyIdentity validates the token and, when supplied, binds all account,
// device, client-type and stream-epoch claims to the current media identity.
func (v JWTVerifier) VerifyIdentity(token string, expected MediaTokenIdentity) error {
	if len(v.Secret) < 32 || token == "" {
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
	}
	if err := decode(parts[0], &header); err != nil || header.Alg != "HS256" || header.Typ != "JWT" {
		return fmt.Errorf("unsupported media token header")
	}
	mac := hmac.New(sha256.New, v.Secret)
	_, _ = mac.Write([]byte(parts[0] + "." + parts[1]))
	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil || !hmac.Equal(signature, mac.Sum(nil)) {
		return fmt.Errorf("invalid media token signature")
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
	}
	if err := decode(parts[1], &claims); err != nil {
		return fmt.Errorf("invalid media token claims")
	}
	if v.Issuer != "" && claims.Issuer != v.Issuer || v.Audience != "" && claims.Audience != v.Audience {
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
	if claims.Expiry <= now.Unix() {
		return fmt.Errorf("media token expired")
	}
	return nil
}
