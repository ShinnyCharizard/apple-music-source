package server

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/silentone12725/musickit-sdk-linux/sdk/diskcache"
)

type heldAudioResponse struct {
	*httptest.ResponseRecorder
	entered chan struct{}
	release chan struct{}
	once    sync.Once
}

func (w *heldAudioResponse) Write(p []byte) (int, error) {
	w.once.Do(func() { close(w.entered) })
	<-w.release
	return w.ResponseRecorder.Write(p)
}

func TestPluginNextAudioPreparesWhileCurrentResponseIsSlow(t *testing.T) {
	cache, _ := diskcache.New(t.TempDir())
	first := &heldAudioResponse{ResponseRecorder: httptest.NewRecorder(), entered: make(chan struct{}), release: make(chan struct{})}
	done := make(chan struct{})
	produce := func(_ context.Context, dst io.Writer) error { _, err := dst.Write([]byte("audio fixture")); return err }
	go func() {
		defer close(done)
		serveCompleteAudio(first, httptest.NewRequest("GET", "/audio", nil), cache, "current", "alac", produce)
	}()
	defer func() { close(first.release); <-done }()
	select {
	case <-first.entered:
	case <-time.After(2 * time.Second):
		t.Fatal("current response did not start")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	second := httptest.NewRecorder()
	serveCompleteAudio(second, httptest.NewRequest("GET", "/audio", nil).WithContext(ctx), cache, "next", "alac", produce)
	if second.Code != 200 || second.Body.String() != "audio fixture" {
		t.Fatalf("next track waited for current delivery: status=%d", second.Code)
	}
}

func TestPluginCompleteAudioRangesHaveRealLength(t *testing.T) {
	cache, err := diskcache.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	data := bytes.Repeat([]byte("audio fixture"), 1000)
	calls := 0
	produce := func(_ context.Context, dst io.Writer) error { calls++; _, err := dst.Write(data); return err }
	for _, method := range []string{"GET", "HEAD", "GET"} {
		req := httptest.NewRequest(method, "/audio", nil)
		req.Header.Set("Range", "bytes=257-8191")
		response := httptest.NewRecorder()
		serveCompleteAudio(response, req, cache, "fixture", "alac", produce)
		if response.Code != 206 || response.Header().Get("Content-Range") != "bytes 257-8191/13000" || response.Header().Get("Content-Length") != "7935" {
			t.Fatalf("incorrect file range: code=%d headers=%v", response.Code, response.Header())
		}
		if method == "GET" && !bytes.Equal(response.Body.Bytes(), data[257:8192]) {
			t.Fatal("audio bytes changed")
		}
		if method == "HEAD" && response.Body.Len() != 0 {
			t.Fatal("HEAD returned a body")
		}
	}
	if calls != 1 {
		t.Fatalf("buffered %d times", calls)
	}
}

func TestPluginCompleteAudioDiscardsIncompleteTrack(t *testing.T) {
	cache, _ := diskcache.New(t.TempDir())
	response := httptest.NewRecorder()
	serveCompleteAudio(response, httptest.NewRequest("GET", "/audio", nil), cache, "fixture", "alac",
		func(_ context.Context, dst io.Writer) error {
			dst.Write([]byte("partial audio"))
			return errors.New("upstream failed")
		})
	if response.Code != 502 {
		t.Fatalf("status=%d", response.Code)
	}
	if _, ok := cache.Path("fixture", "alac"); ok {
		t.Fatal("committed a truncated track")
	}
}

func TestPluginCompleteAudioBoundsTemporaryWrites(t *testing.T) {
	var data bytes.Buffer
	writer := &boundedAudioWriter{&data, 3}
	if _, err := writer.Write([]byte("four")); err == nil || data.Len() != 0 {
		t.Fatal("exceeded temporary buffer bound")
	}
}
