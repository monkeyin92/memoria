package mediaedge

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"testing"
	"time"
)

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
