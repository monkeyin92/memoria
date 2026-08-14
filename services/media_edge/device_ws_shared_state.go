package mediaedge

// Redis-backed hardware-device security state. Ticket replay fences and
// connection leases must be shared before more than one direct Device WSS
// instance is allowed: process-local maps make a one-time ticket reusable on
// another host and cannot retire a superseded socket owned by another host.

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	defaultDeviceStatePrefix        = "memoria:device-media:v2"
	defaultDeviceStateTimeout       = 500 * time.Millisecond
	defaultDeviceLeaseTTL           = 30 * time.Second
	defaultDeviceLeaseCheckInterval = 5 * time.Second
)

var redisOwnerIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)

var ErrDeviceSharedStateUnavailable = errors.New("device shared state unavailable")

var redisLeaseInstallScript = redis.NewScript(`
local current = redis.call('GET', KEYS[1])
if current then
  local decoded = cjson.decode(current)
  if tostring(decoded.stream_epoch_sort) >= ARGV[1] then
    return {0, current}
  end
end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
if current then
  local decoded = cjson.decode(current)
  if decoded.owner_id and decoded.owner_id ~= ARGV[4] then
    redis.call('PUBLISH', ARGV[5] .. decoded.owner_id, current)
  end
end
return {1, current or ''}
`)

var redisLeaseRefreshScript = redis.NewScript(`
local current = redis.call('GET', KEYS[1])
if not current or current ~= ARGV[1] then
  return 0
end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
`)

var redisLeaseReleaseScript = redis.NewScript(`
local current = redis.call('GET', KEYS[1])
if not current or current ~= ARGV[1] then
  return 0
end
redis.call('DEL', KEYS[1])
return 1
`)

type RedisDeviceStateConfig struct {
	URL                string
	TLSConfig          *tls.Config
	KeyPrefix          string
	OwnerID            string
	CommandTimeout     time.Duration
	LeaseTTL           time.Duration
	LeaseCheckInterval time.Duration
}

// RedisDeviceState owns the Redis client and the two stores installed on one
// DeviceWSServer. Redis errors fail closed; availability loss can reject or
// close a session, but can never silently fall back to process-local state.
type RedisDeviceState struct {
	client  *redis.Client
	tickets *redisTicketState
	leases  *redisLeaseState
}

type redisTicketState struct {
	client  *redis.Client
	prefix  string
	timeout time.Duration
	replays atomic.Uint64
}

type redisLeaseState struct {
	client        *redis.Client
	prefix        string
	ownerID       string
	timeout       time.Duration
	ttl           time.Duration
	checkInterval time.Duration
	channelPrefix string

	activeMu sync.Mutex
	active   map[uint64]DeviceLease

	handlerMu sync.RWMutex
	handler   func(DeviceLease)

	pubsub   *redis.PubSub
	cancel   context.CancelFunc
	shutdown atomic.Bool
}

func NewRedisDeviceState(config RedisDeviceStateConfig) (*RedisDeviceState, error) {
	if config.URL == "" {
		return nil, fmt.Errorf("device state Redis URL is required")
	}
	if !redisOwnerIDPattern.MatchString(config.OwnerID) {
		return nil, fmt.Errorf("device state owner id is invalid")
	}
	prefix := config.KeyPrefix
	if prefix == "" {
		prefix = defaultDeviceStatePrefix
	}
	if len(prefix) > 128 || !redisOwnerIDPattern.MatchString(prefix) {
		return nil, fmt.Errorf("device state Redis key prefix is invalid")
	}
	timeout := config.CommandTimeout
	if timeout <= 0 {
		timeout = defaultDeviceStateTimeout
	}
	ttl := config.LeaseTTL
	if ttl <= 0 {
		ttl = defaultDeviceLeaseTTL
	}
	checkInterval := config.LeaseCheckInterval
	if checkInterval <= 0 {
		checkInterval = defaultDeviceLeaseCheckInterval
	}
	if ttl < 3*checkInterval {
		return nil, fmt.Errorf("device lease TTL must be at least three check intervals")
	}

	var options *redis.Options
	var err error
	if strings.HasPrefix(config.URL, "unix://") {
		socketPath := strings.TrimPrefix(config.URL, "unix://")
		if socketPath == "" {
			return nil, fmt.Errorf("parse device state Redis URL: Unix socket path is required")
		}
		options = &redis.Options{Network: "unix", Addr: socketPath}
	} else {
		options, err = redis.ParseURL(config.URL)
		if err != nil {
			return nil, fmt.Errorf("parse device state Redis URL: %w", err)
		}
	}
	if config.TLSConfig != nil {
		if options.TLSConfig == nil {
			return nil, fmt.Errorf("device state Redis TLS configuration requires rediss://")
		}
		options.TLSConfig = config.TLSConfig.Clone()
		if options.TLSConfig.MinVersion < tls.VersionTLS12 {
			options.TLSConfig.MinVersion = tls.VersionTLS12
		}
	}
	// Security-state operations are already bounded by a request context. Do
	// not let client retries extend that fence or turn a Redis outage into a
	// multi-second device handshake stall.
	options.MaxRetries = -1
	options.DialTimeout = timeout
	options.ReadTimeout = timeout
	options.WriteTimeout = timeout
	client := redis.NewClient(options)
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	err = client.Ping(ctx).Err()
	cancel()
	if err != nil {
		_ = client.Close()
		return nil, fmt.Errorf("device state Redis unavailable: %w", err)
	}

	leaseState := &redisLeaseState{
		client: client, prefix: prefix, ownerID: config.OwnerID,
		timeout: timeout, ttl: ttl, checkInterval: checkInterval,
		channelPrefix: prefix + ":lease:supersede:",
		active:        make(map[uint64]DeviceLease),
	}
	if err := leaseState.startSubscriber(); err != nil {
		_ = client.Close()
		return nil, err
	}
	return &RedisDeviceState{
		client: client,
		tickets: &redisTicketState{
			client: client, prefix: prefix, timeout: timeout,
		},
		leases: leaseState,
	}, nil
}

func (s *RedisDeviceState) Install(server *DeviceWSServer) {
	server.Tickets.shared = s.tickets
	server.Leases.shared = s.leases
	server.SharedState = s
	server.Leases.SetSupersedeHandler(func(lease DeviceLease) {
		connection := server.connection(lease.ConnID)
		if connection == nil || connection.deviceID != lease.DeviceID ||
			connection.sessionID != lease.SessionID || uint64(connection.epoch) != lease.StreamEpoch {
			return
		}
		go connection.supersede()
	})
}

func (s *RedisDeviceState) Ready() bool {
	ctx, cancel := context.WithTimeout(context.Background(), s.tickets.timeout)
	defer cancel()
	return s.client.Ping(ctx).Err() == nil
}

func (s *RedisDeviceState) Close() error {
	s.leases.close()
	return s.client.Close()
}

func (s *redisTicketState) consume(jti string, expiry int64) error {
	ttl := time.Unix(expiry, 0).Sub(time.Now())
	if ttl <= 0 {
		return fmt.Errorf("device media ticket expired")
	}
	ctx, cancel := context.WithTimeout(context.Background(), s.timeout)
	defer cancel()
	accepted, err := s.client.SetNX(ctx, s.ticketKey(jti), "1", ttl).Result()
	if err != nil {
		return fmt.Errorf("%w: ticket replay store: %v", ErrDeviceSharedStateUnavailable, err)
	}
	if !accepted {
		s.replays.Add(1)
		return fmt.Errorf("device media ticket already used")
	}
	return nil
}

func (s *redisTicketState) replayCount() uint64 {
	return s.replays.Load()
}

func (s *redisTicketState) ticketKey(jti string) string {
	digest := sha256.Sum256([]byte(jti))
	return s.prefix + ":ticket:" + hex.EncodeToString(digest[:])
}

func (s *redisLeaseState) leaseKey(deviceID string) string {
	digest := sha256.Sum256([]byte(deviceID))
	return s.prefix + ":lease:" + hex.EncodeToString(digest[:])
}

func (s *redisLeaseState) install(lease DeviceLease) (DeviceLease, bool, error) {
	if err := validateDeviceIdentifier(lease.DeviceID, "device_id"); err != nil {
		return DeviceLease{}, false, err
	}
	if err := validateDeviceIdentifier(lease.SessionID, "session_id"); err != nil {
		return DeviceLease{}, false, err
	}
	if lease.StreamEpoch == 0 || lease.ConnID == 0 {
		return DeviceLease{}, false, fmt.Errorf("device lease epoch and connection id must be positive")
	}
	lease.OwnerID = s.ownerID
	payload, err := encodeRedisDeviceLease(lease)
	if err != nil {
		return DeviceLease{}, false, fmt.Errorf("encode device lease: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), s.timeout)
	defer cancel()
	result, err := redisLeaseInstallScript.Run(
		ctx,
		s.client,
		[]string{s.leaseKey(lease.DeviceID)},
		fmt.Sprintf("%020d", lease.StreamEpoch),
		string(payload),
		s.ttl.Milliseconds(),
		s.ownerID,
		s.channelPrefix,
	).Slice()
	if err != nil {
		return DeviceLease{}, false, fmt.Errorf("%w: install lease: %v", ErrDeviceSharedStateUnavailable, err)
	}
	if len(result) != 2 {
		return DeviceLease{}, false, fmt.Errorf("device lease store returned an invalid result")
	}
	accepted, err := redisScriptInt64(result[0])
	if err != nil {
		return DeviceLease{}, false, err
	}
	oldRaw, _ := result[1].(string)
	var old DeviceLease
	replaced := oldRaw != ""
	if replaced {
		if err := json.Unmarshal([]byte(oldRaw), &old); err != nil {
			return DeviceLease{}, false, fmt.Errorf("decode previous device lease: %w", err)
		}
	}
	if accepted != 1 {
		return DeviceLease{}, false, fmt.Errorf(
			"device already has an active connection for stream epoch %d",
			old.StreamEpoch,
		)
	}

	s.activeMu.Lock()
	if replaced && old.OwnerID == s.ownerID {
		delete(s.active, old.ConnID)
	}
	s.active[lease.ConnID] = lease
	s.activeMu.Unlock()
	return old, replaced, nil
}

func (s *redisLeaseState) refresh(lease DeviceLease) (bool, error) {
	lease.OwnerID = s.ownerID
	payload, err := encodeRedisDeviceLease(lease)
	if err != nil {
		return false, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), s.timeout)
	defer cancel()
	result, err := redisLeaseRefreshScript.Run(
		ctx,
		s.client,
		[]string{s.leaseKey(lease.DeviceID)},
		string(payload),
		s.ttl.Milliseconds(),
	).Int64()
	if err != nil {
		return false, fmt.Errorf("%w: refresh lease: %v", ErrDeviceSharedStateUnavailable, err)
	}
	if result != 1 {
		s.removeActive(lease)
		return false, nil
	}
	return true, nil
}

func (s *redisLeaseState) release(deviceID string, connID uint64) {
	s.activeMu.Lock()
	lease, ok := s.active[connID]
	if ok && lease.DeviceID == deviceID {
		delete(s.active, connID)
	} else {
		ok = false
	}
	s.activeMu.Unlock()
	if !ok {
		return
	}
	if s.shutdown.Load() {
		// A draining process cannot safely issue one bounded Redis command per
		// connection in series. The lease TTL remains the crash/shutdown fence;
		// a reconnect with a higher epoch can replace it immediately.
		return
	}
	payload, err := encodeRedisDeviceLease(lease)
	if err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), s.timeout)
	defer cancel()
	_, _ = redisLeaseReleaseScript.Run(
		ctx,
		s.client,
		[]string{s.leaseKey(deviceID)},
		string(payload),
	).Result()
}

func (s *redisLeaseState) activeCount() int {
	s.activeMu.Lock()
	defer s.activeMu.Unlock()
	return len(s.active)
}

func (s *redisLeaseState) removeActive(lease DeviceLease) {
	s.activeMu.Lock()
	defer s.activeMu.Unlock()
	if current, ok := s.active[lease.ConnID]; ok && current.DeviceID == lease.DeviceID &&
		current.StreamEpoch == lease.StreamEpoch {
		delete(s.active, lease.ConnID)
	}
}

func (s *redisLeaseState) setSupersedeHandler(handler func(DeviceLease)) {
	s.handlerMu.Lock()
	defer s.handlerMu.Unlock()
	s.handler = handler
}

func (s *redisLeaseState) startSubscriber() error {
	ctx, cancel := context.WithCancel(context.Background())
	pubsub := s.client.Subscribe(ctx, s.channelPrefix+s.ownerID)
	receiveCtx, receiveCancel := context.WithTimeout(ctx, s.timeout)
	_, err := pubsub.Receive(receiveCtx)
	receiveCancel()
	if err != nil {
		cancel()
		_ = pubsub.Close()
		return fmt.Errorf("subscribe device lease supersession channel: %w", err)
	}
	s.pubsub = pubsub
	s.cancel = cancel
	messages := pubsub.Channel()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case message, ok := <-messages:
				if !ok {
					return
				}
				var lease DeviceLease
				if json.Unmarshal([]byte(message.Payload), &lease) != nil || lease.OwnerID != s.ownerID {
					continue
				}
				s.removeActive(lease)
				s.handlerMu.RLock()
				handler := s.handler
				s.handlerMu.RUnlock()
				if handler != nil {
					handler(lease)
				}
			}
		}
	}()
	return nil
}

func (s *redisLeaseState) close() {
	if s.cancel != nil {
		s.cancel()
	}
	if s.pubsub != nil {
		_ = s.pubsub.Close()
	}
}

func redisScriptInt64(value any) (int64, error) {
	switch typed := value.(type) {
	case int64:
		return typed, nil
	case string:
		parsed, err := strconv.ParseInt(typed, 10, 64)
		if err == nil {
			return parsed, nil
		}
	}
	return 0, fmt.Errorf("device lease store returned an invalid integer")
}

func encodeRedisDeviceLease(lease DeviceLease) ([]byte, error) {
	// Lua 5.1 numbers are IEEE-754 doubles and cannot compare all uint64
	// epochs exactly. A fixed-width decimal sort key preserves the full fence
	// domain while the ordinary numeric field remains convenient for Go.
	payload := struct {
		DeviceLease
		StreamEpochSort string `json:"stream_epoch_sort"`
	}{
		DeviceLease:     lease,
		StreamEpochSort: fmt.Sprintf("%020d", lease.StreamEpoch),
	}
	return json.Marshal(payload)
}
