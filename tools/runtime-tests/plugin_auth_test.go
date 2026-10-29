//go:build linux && native_backend && plugin_auth_test

package drm

import (
	"context"
	"testing"
	"time"
)

func TestPluginNativeChallengeRoundTrip(t *testing.T) {
	var auth *AuthCoordinator
	auth = NewAuthCoordinator(func(snapshot DRMSnapshot) {
		if snapshot.Challenge == nil || snapshot.Challenge.Type != ChallengeTwoFactor {
			t.Error("missing two-factor challenge")
		}
		if err := auth.SubmitReply("246810"); err != nil {
			t.Error(err)
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	reply, states := exerciseNativeAuth(ctx, auth, t.TempDir(), 16)
	if reply != "246810" {
		t.Fatalf("callback reply = %q", reply)
	}
	for _, state := range states {
		if state == "RUNNING" {
			t.Fatal("scaffolding announced a fictitious successful login")
		}
	}
	if err := auth.SubmitReply("246810"); err == nil {
		t.Fatal("accepted unsolicited challenge reply")
	}
}

func TestPluginNativeChallengeCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	auth := NewAuthCoordinator(func(DRMSnapshot) { cancel() })
	reply, _ := exerciseNativeAuth(ctx, auth, t.TempDir(), 16)
	if reply != "" {
		t.Fatal("cancelled challenge returned credentials")
	}
	if err := auth.SubmitReply("246810"); err == nil {
		t.Fatal("cancelled challenge still pending")
	}
}

func TestPluginNativeChallengeRejectsInvalidReply(t *testing.T) {
	for _, code := range []string{"abcdef", "12345", "1234567"} {
		var auth *AuthCoordinator
		auth = NewAuthCoordinator(func(DRMSnapshot) { _ = auth.SubmitReply(code) })
		reply, _ := exerciseNativeAuth(context.Background(), auth, t.TempDir(), 16)
		if reply != "" {
			t.Fatal("invalid code reached native library")
		}
	}
}
