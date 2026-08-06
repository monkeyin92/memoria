package mediaedge

import (
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"testing"
	"time"
)

func signedEdDSAToken(t *testing.T, key ed25519.PrivateKey, kid string, claims map[string]any) string {
	t.Helper()
	encode := func(value any) string {
		payload, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		return base64.RawURLEncoding.EncodeToString(payload)
	}
	header := encode(map[string]string{"alg": "EdDSA", "typ": "JWT", "kid": kid})
	body := encode(claims)
	signature := ed25519.Sign(key, []byte(header+"."+body))
	return header + "." + body + "." + base64.RawURLEncoding.EncodeToString(signature)
}

func signedMediaToken(t *testing.T, secret []byte, claims map[string]any) string {
	t.Helper()
	encode := func(value any) string {
		payload, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		return base64.RawURLEncoding.EncodeToString(payload)
	}
	header := encode(map[string]string{"alg": "HS256", "typ": "JWT"})
	body := encode(claims)
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write([]byte(header + "." + body))
	return header + "." + body + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func TestJWTVerifierBindsMediaIdentity(t *testing.T) {
	secret := []byte("media-token-secret-that-is-long-enough")
	now := time.Unix(1_800_000_000, 0)
	verifier := JWTVerifier{
		Secret:   secret,
		Issuer:   "voice-agent",
		Audience: "memoria-media",
		Now:      func() time.Time { return now },
	}
	claims := map[string]any{
		"iss":          "voice-agent",
		"aud":          "memoria-media",
		"sub":          "account-1",
		"session_id":   "session-1",
		"device_id":    "device-1",
		"client_type":  "device",
		"stream_epoch": 2,
		"exp":          now.Add(time.Minute).Unix(),
	}
	token := signedMediaToken(t, secret, claims)
	expected := MediaTokenIdentity{
		SessionID:   "session-1",
		AccountID:   "account-1",
		DeviceID:    "device-1",
		ClientType:  "device",
		StreamEpoch: 2,
	}
	if err := verifier.VerifyIdentity(token, expected); err != nil {
		t.Fatalf("valid identity rejected: %v", err)
	}
	for name, mutate := range map[string]func(MediaTokenIdentity) MediaTokenIdentity{
		"account": func(value MediaTokenIdentity) MediaTokenIdentity { value.AccountID = "other"; return value },
		"device":  func(value MediaTokenIdentity) MediaTokenIdentity { value.DeviceID = "other"; return value },
		"client":  func(value MediaTokenIdentity) MediaTokenIdentity { value.ClientType = "h5"; return value },
		"epoch":   func(value MediaTokenIdentity) MediaTokenIdentity { value.StreamEpoch = 3; return value },
	} {
		t.Run(name, func(t *testing.T) {
			if err := verifier.VerifyIdentity(token, mutate(expected)); err == nil {
				t.Fatalf("mismatched %s identity was accepted", name)
			}
		})
	}
}

func TestJWTVerifierAcceptsEdDSAAndJWKSRotationKeys(t *testing.T) {
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_800_000_000, 0)
	claims := map[string]any{
		"iss": "voice-agent", "aud": "memoria-media", "sub": "account-1",
		"session_id": "session-1", "device_id": "device-1", "client_type": "h5",
		"stream_epoch": 2, "iat": now.Unix(), "exp": now.Add(time.Minute).Unix(),
	}
	token := signedEdDSAToken(t, private, "media-2026-08", claims)
	verifier := JWTVerifier{
		PublicKeys: map[string]ed25519.PublicKey{"media-2026-08": public},
		Issuer:     "voice-agent", Audience: "memoria-media", MaxTTL: 2 * time.Minute,
		ClockSkew: time.Second, Now: func() time.Time { return now },
	}
	identity, err := verifier.ParseIdentity(token)
	if err != nil {
		t.Fatalf("valid EdDSA token rejected: %v", err)
	}
	if identity.AccountID != "account-1" || identity.ClientType != "h5" {
		t.Fatalf("unexpected identity: %+v", identity)
	}

	badKid := signedEdDSAToken(t, private, "old-key", claims)
	if err := verifier.VerifyIdentity(badKid, identity); err == nil {
		t.Fatal("token signed with an unknown kid was accepted")
	}
	legacy := signedMediaToken(t, []byte("media-token-secret-that-is-long-enough"), claims)
	if err := verifier.VerifyIdentity(legacy, identity); err == nil {
		t.Fatal("HS256 fallback was accepted when EdDSA keys were configured")
	}

	jwks := `{"keys":[{"kty":"OKP","crv":"Ed25519","kid":"media-2026-08","alg":"EdDSA","use":"sig","x":"` + base64.RawURLEncoding.EncodeToString(public) + `"}]}`
	keys, err := ParseEd25519PublicKeys([]byte(jwks))
	if err != nil || len(keys) != 1 {
		t.Fatalf("JWKS parse failed: keys=%d err=%v", len(keys), err)
	}
}
