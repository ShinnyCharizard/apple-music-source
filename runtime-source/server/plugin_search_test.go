package server

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type pluginSearchTransport func(*http.Request) (*http.Response, error)

func (f pluginSearchTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestPluginSearchEncodesQueryAndKeepsAuthorizationOnCatalog(t *testing.T) {
	previous := artworkClient
	defer func() { artworkClient = previous }()
	calls := 0
	artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.URL.Host != "amp-api.music.apple.com" || r.URL.Path != "/v1/catalog/mx/search" || r.URL.Query().Get("term") != "song & other" || r.Header.Get("Authorization") != "Bearer fixture" {
			t.Fatal("catalog request did not preserve its fixed destination, query, and session")
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"results":{"songs":{"data":[]}}}`))}, nil
	})}
	server := &APIServer{cachedToken: "fixture"}
	w := httptest.NewRecorder()
	server.handlePluginSearch(w, httptest.NewRequest("GET", "/catalog/search?sf=mx&term=song+%26+other&limit=25", nil))
	if calls != 1 || w.Code != 200 || !strings.Contains(w.Body.String(), `"results"`) {
		t.Fatal("search response lost")
	}
	for _, path := range []string{"?sf=../&term=song&limit=25", "?sf=mx&term=&limit=25", "?sf=mx&term=song&limit=1000"} {
		w = httptest.NewRecorder()
		server.handlePluginSearch(w, httptest.NewRequest("GET", "/catalog/search"+path, nil))
		if w.Code != 400 || calls != 1 {
			t.Fatal("invalid search reached upstream")
		}
	}
}
