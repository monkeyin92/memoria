package main

import "testing"

func TestBuildWebRTCConfigFailsClosedInProduction(t *testing.T) {
	t.Setenv("MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON", "")
	t.Setenv("MEDIA_EDGE_WEBRTC_PUBLIC_IPS", "")
	t.Setenv("MEDIA_EDGE_WEBRTC_UDP_PORT_MIN", "")
	t.Setenv("MEDIA_EDGE_WEBRTC_UDP_PORT_MAX", "")
	if _, err := buildWebRTCConfig(true); err == nil {
		t.Fatal("production WebRTC accepted no reachable ICE configuration")
	}
}

func TestBuildWebRTCConfigAcceptsTURNOrBoundedPublicUDP(t *testing.T) {
	t.Run("turn", func(t *testing.T) {
		t.Setenv("MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON", `[{"urls":["turn:turn.example:3478"],"username":"edge","credential":"secret"}]`)
		if _, err := buildWebRTCConfig(true); err != nil {
			t.Fatal(err)
		}
	})
	t.Run("public UDP", func(t *testing.T) {
		t.Setenv("MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON", "")
		t.Setenv("MEDIA_EDGE_WEBRTC_PUBLIC_IPS", "203.0.113.10")
		t.Setenv("MEDIA_EDGE_WEBRTC_UDP_PORT_MIN", "40000")
		t.Setenv("MEDIA_EDGE_WEBRTC_UDP_PORT_MAX", "40100")
		if _, err := buildWebRTCConfig(true); err != nil {
			t.Fatal(err)
		}
	})
}

func TestRequestedInteractionAuthorityNeverEnablesUnprovenGoAuthority(t *testing.T) {
	t.Setenv("MEDIA_EDGE_INTERACTION_AUTHORITY", "go_shadow")
	shadow, err := requestedInteractionAuthority()
	if err != nil || shadow.String() != "INTERACTION_AUTHORITY_GO_SHADOW" {
		t.Fatalf("go shadow was not selectable: mode=%v err=%v", shadow, err)
	}
	t.Setenv("MEDIA_EDGE_INTERACTION_AUTHORITY", "go_authoritative")
	authoritative, err := requestedInteractionAuthority()
	if err != nil || authoritative.String() != "INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE" {
		t.Fatalf("unproven Go authority did not fall back to Python: mode=%v err=%v", authoritative, err)
	}
}
