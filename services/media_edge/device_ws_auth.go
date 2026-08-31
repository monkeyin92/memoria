package mediaedge

// Hardware device WSS authentication. Production accepts only a strict
// Media Edge token: typ=memoria_device_media with the full session/sub/
// device/client/binding/stream-epoch binding and a single-use jti. The old
// memoria-device-media-gateway and miniprogram gateway tickets have a
// different typ and are rejected even when they verify. EdDSA/JWKS is the
// production path; HS256 is an explicit development-only fallback.

import (
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"sync"
	"time"
)

const (
	DeviceTokenType            = "memoria_device_media"
	DeviceTokenClientType      = "device"
	DeviceTokenMaxTTL          = 5 * time.Minute
	DeviceTokenMaxJTI          = 128
	DeviceTicketReplayCapacity = 100_000
)

// DeviceMediaClaims is the strict claim set minted by the Control API for
// exactly one hardware session.
type DeviceMediaClaims struct {
	Type                  string              `json:"typ"`
	SessionID             string              `json:"session_id"`
	Subject               string              `json:"sub"`
	DeviceID              string              `json:"device_id"`
	ClientID              string              `json:"client_id"`
	BindingID             string              `json:"binding_id"`
	BindingVersion        uint64              `json:"binding_version"`
	SubjectID             string              `json:"subject_id"`
	ClientType            string              `json:"client_type"`
	StreamEpoch           uint64              `json:"stream_epoch"`
	RuntimeProfileVersion uint64              `json:"runtime_profile_version"`
	DeviceSettings        DeviceSettingsClaim `json:"device_settings"`
	JTI                   string              `json:"jti"`
	Issuer                string              `json:"iss"`
	Audience              string              `json:"aud"`
	Expiry                int64               `json:"exp"`
	NotBefore             int64               `json:"nbf"`
	IssuedAt              int64               `json:"iat"`
}

func (c DeviceMediaClaims) validate(issuer, audience string) error {
	if c.Type != DeviceTokenType {
		return fmt.Errorf("media token type is not a device media token")
	}
	if c.ClientType != DeviceTokenClientType {
		return fmt.Errorf("media token client_type must be device")
	}
	for field, value := range map[string]string{
		"session_id": c.SessionID, "sub": c.Subject, "device_id": c.DeviceID,
		"client_id": c.ClientID, "binding_id": c.BindingID,
	} {
		if err := validateDeviceIdentifier(value, field); err != nil {
			return fmt.Errorf("media token %w", err)
		}
	}
	// subject_id is the runtime profile subject and may be empty for an
	// unknown_safe session. An empty value is an explicit fence value, not a
	// skip signal: it is carried into identity comparisons and must equal the
	// runtime subject echoed by the Voice Core. A non-empty subject still has
	// to be a well-formed identifier.
	if c.SubjectID != "" {
		if err := validateDeviceIdentifier(c.SubjectID, "subject_id"); err != nil {
			return fmt.Errorf("media token %w", err)
		}
	}
	if c.JTI == "" || len(c.JTI) > DeviceTokenMaxJTI {
		return fmt.Errorf("media token jti is required")
	}
	if c.BindingVersion == 0 {
		return fmt.Errorf("media token binding_version must be positive")
	}
	if c.StreamEpoch == 0 || c.StreamEpoch > math.MaxUint32 {
		return fmt.Errorf("media token stream_epoch must be a positive uint32")
	}
	if c.RuntimeProfileVersion == 0 || c.RuntimeProfileVersion > math.MaxUint32 {
		return fmt.Errorf("media token runtime_profile_version must be a positive uint32")
	}
	if err := c.DeviceSettings.validate(); err != nil {
		return fmt.Errorf("media token device_settings: %w", err)
	}
	if issuer != "" && c.Issuer != issuer {
		return fmt.Errorf("media token issuer mismatch")
	}
	if audience != "" && c.Audience != audience {
		return fmt.Errorf("media token audience mismatch")
	}
	return nil
}

// DeviceSettingsClaim is the server-authoritative settings snapshot signed
// into the short-lived hardware ticket. The device never accepts these facts
// from device.hello or another client-controlled frame.
type DeviceSettingsClaim struct {
	SettingsVersion  uint64   `json:"settings_version"`
	VolumeLimit      uint64   `json:"volume_limit"`
	ScreenBrightness uint64   `json:"screen_brightness"`
	NightMode        bool     `json:"night_mode"`
	DoNotDisturb     bool     `json:"do_not_disturb"`
	LearningMode     string   `json:"learning_mode"`
	AudioMode        string   `json:"audio_mode"`
	WakeMode         string   `json:"wake_mode"`
	WakeWordID       string   `json:"wake_word_id,omitempty"`
	WakeWordPinyin   string   `json:"wake_word_pinyin,omitempty"`
	WakeWordDisplay  string   `json:"wake_word_display,omitempty"`
	AllowedBargeIn   []string `json:"allowed_barge_in"`
}

func (s DeviceSettingsClaim) validate() error {
	if s.SettingsVersion > math.MaxUint32 {
		return fmt.Errorf("settings_version must be a uint32")
	}
	if s.VolumeLimit > 100 || s.ScreenBrightness > 100 {
		return fmt.Errorf("volume_limit and screen_brightness must be between 0 and 100")
	}
	switch s.LearningMode {
	case "off", "tutor_english", "tutor_homework":
	default:
		return fmt.Errorf("learning_mode is invalid")
	}
	switch s.AudioMode {
	case DeviceAudioModeHalfDuplexSafe, DeviceAudioModeInterruptAssist, DeviceAudioModeFullDuplex:
	default:
		return fmt.Errorf("audio_mode is invalid")
	}
	switch s.WakeMode {
	case "button", "keyword", "button_or_keyword":
	default:
		return fmt.Errorf("wake_mode is invalid")
	}
	if len(s.AllowedBargeIn) == 0 || len(s.AllowedBargeIn) > 4 {
		return fmt.Errorf("allowed_barge_in must contain 1-4 entries")
	}
	seen := make(map[string]struct{}, len(s.AllowedBargeIn))
	for _, kind := range s.AllowedBargeIn {
		switch kind {
		case "none", "button", "keyword", "voice":
		default:
			return fmt.Errorf("allowed_barge_in is invalid")
		}
		if _, ok := seen[kind]; ok {
			return fmt.Errorf("allowed_barge_in must be unique")
		}
		seen[kind] = struct{}{}
	}
	if _, none := seen["none"]; none && len(seen) != 1 {
		return fmt.Errorf("allowed_barge_in none must be the only value")
	}
	return nil
}

// DeviceJWTVerifier validates the signature, time bounds and every binding
// claim before the connection is upgraded.
type DeviceJWTVerifier struct {
	Secret            []byte
	PublicKeys        map[string]ed25519.PublicKey
	Issuer            string
	Audience          string
	MaxTTL            time.Duration
	ClockSkew         time.Duration
	AllowHS256DevOnly bool
	Now               func() time.Time
}

func (v DeviceJWTVerifier) Configured() bool {
	if len(v.PublicKeys) > 0 {
		return true
	}
	return len(v.Secret) >= 32 && v.AllowHS256DevOnly
}

// Verify authenticates the bearer token and checks the X-Client-ID header
// against the claim so a ticket cannot be replayed from another client.
func (v DeviceJWTVerifier) Verify(token, clientID string) (DeviceMediaClaims, error) {
	if token == "" || !v.Configured() {
		return DeviceMediaClaims{}, fmt.Errorf("device media verifier is not configured")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return DeviceMediaClaims{}, fmt.Errorf("invalid device media token")
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
		return DeviceMediaClaims{}, fmt.Errorf("unsupported device media token header")
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return DeviceMediaClaims{}, fmt.Errorf("invalid device media token signature")
	}
	signed := []byte(parts[0] + "." + parts[1])
	switch header.Alg {
	case "EdDSA":
		if len(v.PublicKeys) == 0 || header.Kid == "" {
			return DeviceMediaClaims{}, fmt.Errorf("device media token key id is missing")
		}
		key, ok := v.PublicKeys[header.Kid]
		if !ok || len(key) != ed25519.PublicKeySize || !ed25519.Verify(key, signed, signature) {
			return DeviceMediaClaims{}, fmt.Errorf("invalid device media token signature")
		}
	case "HS256":
		if len(v.PublicKeys) > 0 || !v.AllowHS256DevOnly || len(v.Secret) < 32 {
			return DeviceMediaClaims{}, fmt.Errorf("HS256 device tokens are not allowed outside explicit development")
		}
		mac := hmac.New(sha256.New, v.Secret)
		_, _ = mac.Write(signed)
		if !hmac.Equal(signature, mac.Sum(nil)) {
			return DeviceMediaClaims{}, fmt.Errorf("invalid device media token signature")
		}
	default:
		return DeviceMediaClaims{}, fmt.Errorf("unsupported device media token algorithm")
	}
	var requiredClaims struct {
		SubjectID *string `json:"subject_id"`
	}
	if err := decode(parts[1], &requiredClaims); err != nil {
		return DeviceMediaClaims{}, fmt.Errorf("invalid device media token claims")
	}
	if requiredClaims.SubjectID == nil {
		return DeviceMediaClaims{}, fmt.Errorf("media token subject_id claim is required")
	}
	var claims DeviceMediaClaims
	if err := decode(parts[1], &claims); err != nil {
		return DeviceMediaClaims{}, fmt.Errorf("invalid device media token claims")
	}
	if err := claims.validate(v.Issuer, v.Audience); err != nil {
		return DeviceMediaClaims{}, err
	}
	if clientID == "" || claims.ClientID != clientID {
		return DeviceMediaClaims{}, fmt.Errorf("device media token client mismatch")
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
		return DeviceMediaClaims{}, fmt.Errorf("device media token expired")
	}
	if claims.IssuedAt == 0 || time.Unix(claims.IssuedAt, 0).After(now.Add(skew)) {
		return DeviceMediaClaims{}, fmt.Errorf("device media token issued in the future")
	}
	if claims.NotBefore != 0 && time.Unix(claims.NotBefore, 0).After(now.Add(skew)) {
		return DeviceMediaClaims{}, fmt.Errorf("device media token is not active")
	}
	if claims.Expiry <= claims.IssuedAt ||
		(claims.NotBefore != 0 && claims.NotBefore >= claims.Expiry) {
		return DeviceMediaClaims{}, fmt.Errorf("device media token time bounds are invalid")
	}
	maxTTL := v.MaxTTL
	if maxTTL <= 0 {
		maxTTL = DeviceTokenMaxTTL
	}
	if time.Unix(claims.Expiry, 0).Sub(time.Unix(claims.IssuedAt, 0)) > maxTTL {
		return DeviceMediaClaims{}, fmt.Errorf("device media token ttl exceeds limit")
	}
	return claims, nil
}

// DeviceTicketStore is the single-use jti replay store. A jti is consumed at
// upgrade time and stays consumed until its exp; the same ticket cannot open
// a second connection, and an old gateway ticket is rejected before it ever
// reaches this store because its typ differs.
type DeviceTicketStore struct {
	mu      sync.Mutex
	used    map[string]int64
	order   []string
	now     func() time.Time
	replays uint64
	shared  *redisTicketState
}

func NewDeviceTicketStore() *DeviceTicketStore {
	return &DeviceTicketStore{used: make(map[string]int64)}
}

func (s *DeviceTicketStore) SetClock(now func() time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.now = now
}

func (s *DeviceTicketStore) currentTime() time.Time {
	if s.now != nil {
		return s.now()
	}
	return time.Now()
}

// Consume marks a jti used exactly once. Replays and reused ids are rejected.
func (s *DeviceTicketStore) Consume(jti string, expiry int64) error {
	if s.shared != nil {
		return s.shared.consume(jti, expiry)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.currentTime()
	if usedUntil, seen := s.used[jti]; seen {
		if usedUntil >= now.Unix() {
			s.replays++
			return fmt.Errorf("device media ticket already used")
		}
		delete(s.used, jti)
	}
	if len(s.used) >= DeviceTicketReplayCapacity {
		// Never evict an unexpired JTI: doing so would make a valid one-time
		// credential replayable. Prune only expired entries and fail closed
		// when the live set itself reaches capacity.
		kept := s.order[:0]
		keptIDs := make(map[string]struct{}, len(s.used))
		for _, id := range s.order {
			expires, ok := s.used[id]
			if !ok || expires < now.Unix() {
				delete(s.used, id)
				continue
			}
			if _, duplicate := keptIDs[id]; duplicate {
				continue
			}
			keptIDs[id] = struct{}{}
			kept = append(kept, id)
		}
		s.order = kept
		if len(s.used) >= DeviceTicketReplayCapacity {
			return fmt.Errorf("device media replay store capacity exhausted")
		}
	}
	s.used[jti] = expiry
	s.order = append(s.order, jti)
	return nil
}

func (s *DeviceTicketStore) ReplayCount() uint64 {
	if s.shared != nil {
		return s.shared.replayCount()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.replays
}

// DeviceLease is one device's active connection lease.
type DeviceLease struct {
	DeviceID    string `json:"device_id"`
	SessionID   string `json:"session_id"`
	StreamEpoch uint64 `json:"stream_epoch"`
	ConnID      uint64 `json:"conn_id"`
	OwnerID     string `json:"owner_id"`
}

// DeviceLeaseRegistry enforces one connection per device. A reconnect with a
// strictly higher stream_epoch atomically replaces the old lease; equal or
// older epochs are rejected so a stale socket cannot displace a live one.
type DeviceLeaseRegistry struct {
	mu     sync.Mutex
	leases map[string]DeviceLease
	shared *redisLeaseState
}

func NewDeviceLeaseRegistry() *DeviceLeaseRegistry {
	return &DeviceLeaseRegistry{leases: make(map[string]DeviceLease)}
}

// Install atomically claims the device for this connection and returns the
// lease it replaced (if any) so the caller can close the superseded socket.
func (r *DeviceLeaseRegistry) Install(lease DeviceLease) (old DeviceLease, replaced bool, err error) {
	if r.shared != nil {
		return r.shared.install(lease)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if current, ok := r.leases[lease.DeviceID]; ok && current.StreamEpoch >= lease.StreamEpoch {
		return DeviceLease{}, false, fmt.Errorf("device already has an active connection for stream epoch %d", current.StreamEpoch)
	}
	old, replaced = r.leases[lease.DeviceID]
	r.leases[lease.DeviceID] = lease
	return old, replaced, nil
}

func (r *DeviceLeaseRegistry) Release(deviceID string, connID uint64) {
	if r.shared != nil {
		r.shared.release(deviceID, connID)
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if current, ok := r.leases[deviceID]; ok && current.ConnID == connID {
		delete(r.leases, deviceID)
	}
}

func (r *DeviceLeaseRegistry) ActiveCount() int {
	if r.shared != nil {
		return r.shared.activeCount()
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.leases)
}

// OwnerID is empty for the process-local registry and globally unique for a
// Redis-backed registry. It is part of the compare-and-delete lease value, so
// one Edge process can never release another process's newer connection.
func (r *DeviceLeaseRegistry) OwnerID() string {
	if r.shared == nil {
		return ""
	}
	return r.shared.ownerID
}

// Refresh proves that this exact connection still owns the shared device
// lease and extends its TTL. Redis errors are returned to the caller, which
// must fail closed instead of continuing on stale local state.
func (r *DeviceLeaseRegistry) Refresh(lease DeviceLease) (bool, error) {
	if r.shared != nil {
		return r.shared.refresh(lease)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	current, ok := r.leases[lease.DeviceID]
	return ok && current.ConnID == lease.ConnID && current.StreamEpoch == lease.StreamEpoch, nil
}

func (r *DeviceLeaseRegistry) CheckInterval() time.Duration {
	if r.shared == nil {
		return 0
	}
	return r.shared.checkInterval
}

func (r *DeviceLeaseRegistry) SetSupersedeHandler(handler func(DeviceLease)) {
	if r.shared != nil {
		r.shared.setSupersedeHandler(handler)
	}
}

func (r *DeviceLeaseRegistry) BeginShutdown() {
	if r.shared != nil {
		r.shared.shutdown.Store(true)
	}
}
