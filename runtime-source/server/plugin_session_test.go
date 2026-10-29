package server

import (
	"context"
	"github.com/silentone12725/musickit-sdk-linux/sdk/drm"
	"testing"
	"time"
)

type pluginAccountBackend struct {
	drm.DRMBackend
	events  chan drm.DRMEvent
	account drm.AccountInfo
}

func (b *pluginAccountBackend) SetAuthSource(drm.AuthSource) {}
func (b *pluginAccountBackend) Running() bool                { return true }
func (b *pluginAccountBackend) Stop() error                  { return nil }
func (b *pluginAccountBackend) Events() <-chan drm.DRMEvent  { return b.events }
func (b *pluginAccountBackend) GetAccount(context.Context) (drm.AccountInfo, error) {
	return b.account, nil
}

func TestPluginPlaybackUsesLiveSessionWithoutTokenFile(t *testing.T) {
	backend := &pluginAccountBackend{events: make(chan drm.DRMEvent, 1), account: drm.AccountInfo{DevToken: "fixture-developer", MusicToken: "fixture-user"}}
	session := drm.NewSessionManager(t.TempDir())
	ready := make(chan struct{}, 1)
	manager := drm.NewDRMManager(backend, session, func(s drm.DRMSnapshot) {
		if s.State.FairPlay == drm.FairPlayReady {
			select {
			case ready <- struct{}{}:
			default:
			}
		}
	}, drm.BackendConfig{}, drm.DefaultRestartPolicy)
	defer manager.Shutdown()
	defer close(backend.events)
	event := drm.DRMEvent{}
	event.Snapshot.State.FairPlay = drm.FairPlayReady
	backend.events <- event
	select {
	case <-ready:
	case <-time.After(time.Second):
		t.Fatal("native session did not become ready")
	}
	server := &APIServer{dm: manager, session: session}
	if session.ReadMusicToken() != "" {
		t.Fatal("fixture unexpectedly has a token file")
	}
	if server.token() != "fixture-developer" || server.mediaUserToken() != "fixture-user" {
		t.Fatal("playback did not receive the active account token pair")
	}
	backend.account.MusicToken = "fixture-rotated"
	if server.mediaUserToken() != "fixture-rotated" {
		t.Fatal("playback reused a stale account token")
	}
}
