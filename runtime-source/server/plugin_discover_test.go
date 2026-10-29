package server

import (
 "io"
 "net/http"
 "net/http/httptest"
 "strings"
 "testing"
)

func TestPluginDiscoverBoundsAndDestination(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 calls := 0
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  calls++
  if r.URL.Host != "amp-api.music.apple.com" || r.URL.Path != "/v1/catalog/mx/charts" || r.URL.Query().Get("genre") != "20" || r.URL.Query().Get("types") != "songs,albums" || r.Header.Get("Authorization") != "Bearer fixture" {
   t.Fatal("discovery destination, filters or authorization changed")
  }
  return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"results":{}}`))}, nil
 })}
 server := &APIServer{cachedToken: "fixture"}
 w := httptest.NewRecorder()
 server.handlePluginDiscover(w, httptest.NewRequest("GET", "/catalog/discover?sf=mx&section=charts&genre=20&offset=0", nil))
 if w.Code != 200 || calls != 1 { t.Fatal("charts request failed") }
 for _, query := range []string{"sf=../&section=charts&genre=20&offset=0", "sf=mx&section=library&genre=0&offset=0", "sf=mx&section=charts&genre=url&offset=0", "sf=mx&section=charts&genre=0&offset=9999"} {
  w = httptest.NewRecorder()
  server.handlePluginDiscover(w, httptest.NewRequest("GET", "/catalog/discover?"+query, nil))
  if w.Code != 400 || calls != 1 { t.Fatal("invalid discovery reached upstream") }
 }
}

func TestPluginDiscoverDoesNotFollowRedirect(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 calls := 0
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  calls++
  return &http.Response{StatusCode: 302, Header: http.Header{"Location": {"https://example.invalid"}}, Body: io.NopCloser(strings.NewReader(""))}, nil
 })}
 w := httptest.NewRecorder()
 (&APIServer{cachedToken: "fixture"}).handlePluginDiscover(w, httptest.NewRequest("GET", "/catalog/discover?sf=mx&section=charts&genre=0&offset=0", nil))
 if w.Code != 502 || calls != 1 { t.Fatal("redirect was followed or accepted") }
}
