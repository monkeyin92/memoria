package mediaedge

import (
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"testing"
	"time"
)

func testDeviceKey(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return publicKey, privateKey
}

func signDeviceToken(t *testing.T, privateKey ed25519.PrivateKey, claims DeviceMediaClaims, algorithm string) string {
	t.Helper()
	return signDeviceTokenPayload(t, privateKey, claims, algorithm)
}

func signDeviceTokenPayload(t *testing.T, privateKey ed25519.PrivateKey, payload any, algorithm string) string {
	t.Helper()
	header := map[string]any{"alg": algorithm, "typ": "JWT", "kid": "test-key"}
	headerJSON, err := json.Marshal(header)
	if err != nil {
		t.Fatal(err)
	}
	payloadJSON, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	encode := func(data []byte) string {
		return base64.RawURLEncoding.EncodeToString(data)
	}
	signed := encode(headerJSON) + "." + encode(payloadJSON)
	var signature []byte
	if algorithm == "EdDSA" {
		signature = ed25519.Sign(privateKey, []byte(signed))
	} else {
		t.Fatalf("unsupported test algorithm %s", algorithm)
	}
	return signed + "." + encode(signature)
}

func hs256DeviceToken(t *testing.T, secret []byte, claims DeviceMediaClaims) string {
	t.Helper()
	headerJSON, _ := json.Marshal(map[string]any{"alg": "HS256", "typ": "JWT"})
	payloadJSON, _ := json.Marshal(claims)
	encode := func(data []byte) string {
		return base64.RawURLEncoding.EncodeToString(data)
	}
	signed := encode(headerJSON) + "." + encode(payloadJSON)
	mac := hmacSHA256(secret, []byte(signed))
	return signed + "." + encode(mac)
}

func hmacSHA256(secret, data []byte) []byte {
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write(data)
	return mac.Sum(nil)
}

func validDeviceClaims() DeviceMediaClaims {
	now := time.Now()
	return DeviceMediaClaims{
		Type: DeviceTokenType, SessionID: "session_1", Subject: "account_1",
		DeviceID: "dev_1", ClientID: "client_1", BindingID: "binding_1",
		BindingVersion: 3, ClientType: "device", StreamEpoch: 18,
		SubjectID: "subject_1", RuntimeProfileVersion: 27,
		DeviceSettings: DeviceSettingsClaim{
			SettingsVersion: 4, VolumeLimit: 72, ScreenBrightness: 80,
			LearningMode: "off", AudioMode: DeviceAudioModeHalfDuplexSafe,
			WakeMode: "button_or_keyword", AllowedBargeIn: []string{"button"},
		},
		JTI:    "ticket_1",
		Issuer: "memoria-control-api", Audience: "memoria-media-edge",
		Expiry:    now.Add(5 * time.Minute).Unix(),
		NotBefore: now.Add(-time.Minute).Unix(),
		IssuedAt:  now.Unix(),
	}
}

func newDeviceVerifier(publicKey ed25519.PublicKey) DeviceJWTVerifier {
	return DeviceJWTVerifier{
		PublicKeys: map[string]ed25519.PublicKey{"test-key": publicKey},
		Issuer:     "memoria-control-api",
		Audience:   "memoria-media-edge",
		MaxTTL:     5 * time.Minute,
		ClockSkew:  5 * time.Second,
	}
}

func TestDeviceJWTVerifyAcceptsStrictEdDSA(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	token := signDeviceToken(t, privateKey, validDeviceClaims(), "EdDSA")
	claims, err := newDeviceVerifier(publicKey).Verify(token, "client_1")
	if err != nil {
		t.Fatal(err)
	}
	if claims.SessionID != "session_1" || claims.DeviceID != "dev_1" ||
		claims.BindingID != "binding_1" || claims.BindingVersion != 3 ||
		claims.StreamEpoch != 18 || claims.JTI != "ticket_1" {
		t.Fatalf("claims were not preserved: %+v", claims)
	}
}

func TestDeviceJWTVerifyRejectsOldGatewayTicketType(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	claims := validDeviceClaims()
	claims.Type = "memoria_device_gateway"
	token := signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("legacy gateway ticket was accepted")
	}
}

func TestDeviceJWTVerifyRejectsNonDeviceClientType(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	claims := validDeviceClaims()
	claims.ClientType = "h5"
	token := signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("non-device client type was accepted")
	}
}

func TestDeviceJWTVerifyRejectsExpiredAndFutureTokens(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	now := time.Now()
	cases := []struct {
		name   string
		mutate func(*DeviceMediaClaims)
	}{
		{"expired", func(claims *DeviceMediaClaims) { claims.Expiry = now.Add(-time.Minute).Unix() }},
		{"nbf future", func(claims *DeviceMediaClaims) { claims.NotBefore = now.Add(time.Minute).Unix() }},
		{"iat future", func(claims *DeviceMediaClaims) { claims.IssuedAt = now.Add(time.Minute).Unix() }},
		{"ttl too long", func(claims *DeviceMediaClaims) { claims.IssuedAt = now.Add(-10 * time.Minute).Unix() }},
		{"missing exp", func(claims *DeviceMediaClaims) { claims.Expiry = 0 }},
		{"exp before iat", func(claims *DeviceMediaClaims) { claims.Expiry = claims.IssuedAt }},
		{"nbf at exp", func(claims *DeviceMediaClaims) { claims.NotBefore = claims.Expiry }},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			claims := validDeviceClaims()
			testCase.mutate(&claims)
			token := signDeviceToken(t, privateKey, claims, "EdDSA")
			if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
				t.Fatal("invalid time bounds were accepted")
			}
		})
	}
}

func TestDeviceJWTVerifyRejectsClientMismatch(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	token := signDeviceToken(t, privateKey, validDeviceClaims(), "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_other"); err == nil {
		t.Fatal("client mismatch was accepted")
	}
	if _, err := newDeviceVerifier(publicKey).Verify(token, ""); err == nil {
		t.Fatal("missing client header was accepted")
	}
}

func TestDeviceJWTVerifyRejectsIssuerAudienceMismatch(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	claims := validDeviceClaims()
	claims.Issuer = "someone-else"
	token := signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("issuer mismatch was accepted")
	}
	claims = validDeviceClaims()
	claims.Audience = "memoria-miniprogram-media-gateway"
	token = signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("audience mismatch was accepted")
	}
}

func TestDeviceJWTVerifyRejectsMissingBinding(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	claims := validDeviceClaims()
	claims.BindingID = ""
	token := signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("missing binding_id was accepted")
	}
	claims = validDeviceClaims()
	claims.BindingVersion = 0
	token = signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("zero binding_version was accepted")
	}
	claims = validDeviceClaims()
	claims.StreamEpoch = 0
	token = signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("zero stream_epoch was accepted")
	}
	claims = validDeviceClaims()
	claims.StreamEpoch = uint64(math.MaxUint32) + 1
	token = signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("stream_epoch outside the device uint32 wire domain was accepted")
	}
	claims = validDeviceClaims()
	claims.RuntimeProfileVersion = 0
	token = signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("zero runtime_profile_version was accepted")
	}
	claims = validDeviceClaims()
	claims.RuntimeProfileVersion = uint64(math.MaxUint32) + 1
	token = signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("runtime_profile_version outside the device uint32 domain was accepted")
	}
	claims = validDeviceClaims()
	claims.DeviceSettings.SettingsVersion = uint64(math.MaxUint32) + 1
	token = signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("settings_version outside the device uint32 domain was accepted")
	}
}

func TestDeviceJWTVerifyAllowsEmptyRuntimeSubject(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	claims := validDeviceClaims()
	claims.SubjectID = ""
	token := signDeviceToken(t, privateKey, claims, "EdDSA")
	verified, err := newDeviceVerifier(publicKey).Verify(token, "client_1")
	if err != nil {
		t.Fatalf("unknown_safe token with empty subject_id was rejected: %v", err)
	}
	if verified.SubjectID != "" || verified.BindingID != "binding_1" ||
		verified.BindingVersion != 3 || verified.RuntimeProfileVersion != 27 {
		t.Fatalf("empty subject token lost the runtime profile authority fence: %+v", verified)
	}
}

func TestDeviceJWTVerifyRequiresExplicitRuntimeSubjectClaim(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	payloadJSON, err := json.Marshal(validDeviceClaims())
	if err != nil {
		t.Fatal(err)
	}
	var payload map[string]any
	if err := json.Unmarshal(payloadJSON, &payload); err != nil {
		t.Fatal(err)
	}
	delete(payload, "subject_id")
	token := signDeviceTokenPayload(t, privateKey, payload, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("token without an explicit subject_id claim was accepted")
	}
	payload["subject_id"] = nil
	token = signDeviceTokenPayload(t, privateKey, payload, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("token with a null subject_id claim was accepted")
	}
}

func TestDeviceJWTVerifyRejectsMalformedRuntimeSubject(t *testing.T) {
	publicKey, privateKey := testDeviceKey(t)
	claims := validDeviceClaims()
	claims.SubjectID = "subject with spaces"
	token := signDeviceToken(t, privateKey, claims, "EdDSA")
	if _, err := newDeviceVerifier(publicKey).Verify(token, "client_1"); err == nil {
		t.Fatal("malformed subject_id was accepted")
	}
}

func TestDeviceJWTVerifyHS256RequiresExplicitDevelopment(t *testing.T) {
	secret := []byte("0123456789abcdef0123456789abcdef")
	claims := validDeviceClaims()
	token := hs256DeviceToken(t, secret, claims)
	verifier := DeviceJWTVerifier{
		Secret: secret, Issuer: "memoria-control-api", Audience: "memoria-media-edge",
		MaxTTL: 5 * time.Minute, ClockSkew: 5 * time.Second,
	}
	if _, err := verifier.Verify(token, "client_1"); err == nil {
		t.Fatal("HS256 without the development flag was accepted")
	}
	verifier.AllowHS256DevOnly = true
	if _, err := verifier.Verify(token, "client_1"); err != nil {
		t.Fatalf("explicit development HS256 was rejected: %v", err)
	}
	verifier2 := DeviceJWTVerifier{
		Secret: secret, AllowHS256DevOnly: true,
		PublicKeys: map[string]ed25519.PublicKey{"x": make([]byte, 32)},
	}
	if _, err := verifier2.Verify(token, "client_1"); err == nil {
		t.Fatal("HS256 was accepted while EdDSA keys are configured")
	}
}

func TestDeviceTicketStoreSingleUseAndExpiry(t *testing.T) {
	store := NewDeviceTicketStore()
	now := time.Now()
	store.SetClock(func() time.Time { return now })
	expiry := now.Add(time.Minute).Unix()
	if err := store.Consume("ticket_1", expiry); err != nil {
		t.Fatal(err)
	}
	if err := store.Consume("ticket_1", expiry); err == nil {
		t.Fatal("ticket replay was accepted")
	}
	if err := store.Consume("ticket_2", expiry); err != nil {
		t.Fatal(err)
	}
	now = now.Add(2 * time.Minute)
	if err := store.Consume("ticket_1", expiry); err != nil {
		t.Fatalf("expired ticket entry should be reusable: %v", err)
	}
	if store.ReplayCount() != 1 {
		t.Fatalf("replay count = %d, want 1", store.ReplayCount())
	}
}

func TestDeviceTicketStoreFailsClosedWithoutEvictingLiveReplayFences(t *testing.T) {
	store := NewDeviceTicketStore()
	now := time.Now()
	store.SetClock(func() time.Time { return now })
	expiry := now.Add(time.Minute).Unix()
	for index := 0; index < DeviceTicketReplayCapacity; index++ {
		id := fmt.Sprintf("ticket_%d", index)
		store.used[id] = expiry
		store.order = append(store.order, id)
	}
	if err := store.Consume("overflow", expiry); err == nil {
		t.Fatal("replay store accepted a ticket by evicting a live fence")
	}
	if err := store.Consume("ticket_0", expiry); err == nil {
		t.Fatal("oldest live ticket became replayable at capacity")
	}
	now = now.Add(2 * time.Minute)
	if err := store.Consume("after_expiry", now.Add(time.Minute).Unix()); err != nil {
		t.Fatalf("expired replay fences were not pruned: %v", err)
	}
}

func TestDeviceSettingsClaimRejectsMixedNoneBargeIn(t *testing.T) {
	settings := validDeviceClaims().DeviceSettings
	settings.AllowedBargeIn = []string{"none", "button"}
	if err := settings.validate(); err == nil {
		t.Fatal("allowed_barge_in accepted none mixed with an active source")
	}
}

func TestDeviceLeaseRegistrySingleConnectionAndTakeover(t *testing.T) {
	registry := NewDeviceLeaseRegistry()
	lease1 := DeviceLease{DeviceID: "dev_1", SessionID: "s1", StreamEpoch: 1, ConnID: 1}
	old, replaced, err := registry.Install(lease1)
	if err != nil || replaced || old != (DeviceLease{}) {
		t.Fatalf("first lease install failed: replaced=%v old=%+v err=%v", replaced, old, err)
	}
	if _, _, err := registry.Install(DeviceLease{DeviceID: "dev_1", SessionID: "s1", StreamEpoch: 1, ConnID: 2}); err == nil {
		t.Fatal("equal stream epoch second connection was accepted")
	}
	if _, _, err := registry.Install(DeviceLease{DeviceID: "dev_1", SessionID: "s1", StreamEpoch: 0, ConnID: 3}); err == nil {
		t.Fatal("older stream epoch was accepted")
	}
	old, replaced, err = registry.Install(DeviceLease{DeviceID: "dev_1", SessionID: "s1b", StreamEpoch: 2, ConnID: 4})
	if err != nil || !replaced || old.ConnID != 1 {
		t.Fatalf("takeover failed: replaced=%v old=%+v err=%v", replaced, old, err)
	}
	registry.Release("dev_1", 1)
	if registry.ActiveCount() != 1 {
		t.Fatal("stale connection released the newer lease")
	}
	registry.Release("dev_1", 4)
	if registry.ActiveCount() != 0 {
		t.Fatal("lease was not released")
	}
}
