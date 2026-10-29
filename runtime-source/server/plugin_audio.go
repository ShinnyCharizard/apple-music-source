package server

import (
	"context"
	"errors"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/silentone12725/musickit-sdk-linux/sdk/diskcache"
	"github.com/silentone12725/musickit-sdk-linux/sdk/pipeline"
)

// Native file readers require the exact length before opening a track.
// The streaming endpoint's guessed range length cannot satisfy that contract.
var completeAudioSlot = make(chan struct{}, 1)

const completeAudioLimit int64 = 512 << 20

type boundedAudioWriter struct {
	target io.Writer
	left   int64
}

func (w *boundedAudioWriter) Write(p []byte) (int, error) {
	if int64(len(p)) > w.left {
		return 0, errors.New("temporary audio buffer limit reached")
	}
	n, err := w.target.Write(p)
	w.left -= int64(n)
	return n, err
}

func (s *APIServer) handleCompleteAudio(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	session, ok := s.pm.GetSession(id)
	if !ok || !session.Capabilities.Audio {
		http.Error(w, "audio session unavailable", http.StatusNotFound)
		return
	}
	serveCompleteAudio(w, r, s.diskCache, session.AssetID, session.Codec+"-complete",
		func(ctx context.Context, dst io.Writer) error { return s.pm.Stream(ctx, id, pipeline.KindAudio, dst) })
}

func serveCompleteAudio(w http.ResponseWriter, r *http.Request, cache *diskcache.Cache,
	asset, qualifier string, produce func(context.Context, io.Writer) error) {
	if cache == nil {
		http.Error(w, "temporary buffer unavailable", http.StatusServiceUnavailable)
		return
	}
	serve := func(file *os.File) {
		defer file.Close()
		w.Header().Set("Content-Type", "audio/mp4")
		w.Header().Set("Cache-Control", "no-store")
		http.ServeContent(w, r, "audio.m4a", time.Time{}, file)
	}
	if file, ok := cache.Get(asset, qualifier); ok {
		serve(file)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 35*time.Second)
	defer cancel()
	select {
	case completeAudioSlot <- struct{}{}:
	case <-ctx.Done():
		http.Error(w, "audio buffer timed out", http.StatusGatewayTimeout)
		return
	}
	// Keep only one unfinished full-track buffer at a time.
	held := true
	release := func() {
		if held {
			<-completeAudioSlot
			held = false
		}
	}
	defer release()
	if file, ok := cache.Get(asset, qualifier); ok {
		release()
		serve(file)
		return
	}
	pending, err := cache.BeginPut(asset, qualifier)
	if err != nil || pending == nil {
		http.Error(w, "audio buffer unavailable", http.StatusServiceUnavailable)
		return
	}
	defer pending.Discard()
	if err := produce(ctx, &boundedAudioWriter{pending, completeAudioLimit}); err != nil || ctx.Err() != nil {
		http.Error(w, "audio buffering failed", http.StatusBadGateway)
		return
	}
	// Pin the file before commit's asynchronous eviction, including large tracks.
	file, err := os.Open(pending.Name())
	if err != nil {
		http.Error(w, "audio buffer unavailable", http.StatusServiceUnavailable)
		return
	}
	if err := pending.Commit(); err != nil {
		file.Close()
		http.Error(w, "audio buffer commit failed", http.StatusServiceUnavailable)
		return
	}
	// I serialize preparation only; a slow reader must not block the next track.
	release()
	serve(file)
}
