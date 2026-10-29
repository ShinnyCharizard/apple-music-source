package server

import (
 "io"
 "net/http"
 "net/http/httptest"
 "strings"
 "testing"
)

func TestPluginPersonalBoundsAndDestination(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 calls := 0
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  calls++
  if r.URL.Host != "amp-api.music.apple.com" || r.URL.Path != "/v1/me/recommendations" || r.Header.Get("Music-User-Token") != "user-fixture" || r.Header.Get("Authorization") != "Bearer fixture" {
   t.Fatal("discovery destination, filters or authorization changed")
  }
  return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"results":{}}`))}, nil
 })}
 server := &APIServer{cachedToken: "fixture", mkMusicToken: "user-fixture"}
 w := httptest.NewRecorder()
 server.handlePluginPersonal(w, httptest.NewRequest("GET", "/catalog/personal?sf=mx&section=recommendations&id=none&offset=0", nil))
 if w.Code != 200 || calls != 1 { t.Fatal("charts request failed") }
 for _, query := range []string{"sf=../&section=recommendations&id=none&offset=0", "sf=mx&section=unknown&id=none&offset=0", "sf=mx&section=playlist&id=../secret&offset=0", "sf=mx&section=recommendations&id=none&offset=10001"} {
  w = httptest.NewRecorder()
  server.handlePluginPersonal(w, httptest.NewRequest("GET", "/catalog/personal?"+query, nil))
  if w.Code != 400 || calls != 1 { t.Fatal("invalid discovery reached upstream") }
 }
}

func TestPluginPersonalDoesNotFollowRedirect(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 calls := 0
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  calls++
  return &http.Response{StatusCode: 302, Header: http.Header{"Location": {"https://example.invalid"}}, Body: io.NopCloser(strings.NewReader(""))}, nil
 })}
 w := httptest.NewRecorder()
 (&APIServer{cachedToken: "fixture", mkMusicToken: "user-fixture"}).handlePluginPersonal(w, httptest.NewRequest("GET", "/catalog/personal?sf=mx&section=recommendations&id=none&offset=0", nil))
 if w.Code != 502 || calls != 1 { t.Fatal("redirect was followed or accepted") }
}

func TestPluginPersonalPlaylistHasNoCollectionPagination(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  if r.URL.Path != "/v1/catalog/mx/playlists/pl.pm-fixture" || r.URL.Query().Has("limit") || r.URL.Query().Has("offset") {
   t.Fatal("a single playlist must not receive collection pagination")
  }
  return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"data":[]}`))}, nil
 })}
 w := httptest.NewRecorder()
 (&APIServer{cachedToken: "fixture", mkMusicToken: "user-fixture"}).handlePluginPersonal(w, httptest.NewRequest("GET", "/catalog/personal?sf=mx&section=playlist&id=pl.pm-fixture&offset=0", nil))
 if w.Code != 200 { t.Fatal("playlist failed") }
}

func TestPluginPersonalPlaylistFolderRoutes(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 expected := "/v1/me/library/playlist-folders"
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  if r.URL.Path != expected || r.Header.Get("Music-User-Token") != "user-fixture" { t.Fatal("folder route changed") }
  return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"data":[]}`))}, nil
 })}
 server := &APIServer{cachedToken: "fixture", mkMusicToken: "user-fixture"}
 for _, query := range []string{"sf=mx&section=folder-root&id=none&offset=0", "sf=mx&section=folder-children&id=p.child&offset=0"} {
  if strings.Contains(query, "folder-children") { expected = "/v1/me/library/playlist-folders/p.child/children" }
  w := httptest.NewRecorder()
  server.handlePluginPersonal(w, httptest.NewRequest("GET", "/catalog/personal?"+query, nil))
  if w.Code != 200 { t.Fatal("folder request failed") }
 }
}

func TestPluginCollectionsKeepFavoriteWritesSeparate(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 calls := 0
 method := "POST"
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  calls++
  if r.Method != method || r.URL.Host != "amp-api.music.apple.com" || r.URL.Path != "/v1/me/favorites" || r.URL.Query().Get("ids[songs]") != "123" || r.Header.Get("Music-User-Token") != "user-fixture" { t.Fatal("favorite request changed") }
  return &http.Response{StatusCode: 202, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(""))}, nil
 })}
 server := &APIServer{cachedToken: "fixture", mkMusicToken: "user-fixture"}
 for _, verb := range []string{"POST", "DELETE"} {
  method = verb
  w := httptest.NewRecorder()
  server.handlePluginCollection(w, httptest.NewRequest(verb, "/catalog/collection?sf=mx&kind=songs&id=123&collection=favorites", nil))
  if w.Code != 200 { t.Fatal("favorite mutation failed") }
 }
 if calls != 2 { t.Fatal("unexpected extra library write") }
 for _, query := range []string{"sf=mx&kind=songs&id=../123&collection=favorites", "sf=mx&kind=unknown&id=123&collection=favorites", "sf=mx&kind=songs&id=123&collection=unknown"} {
  w := httptest.NewRecorder()
  server.handlePluginCollection(w, httptest.NewRequest("POST", "/catalog/collection?"+query, nil))
  if w.Code != 400 || calls != 2 { t.Fatal("invalid input reached upstream") }
 }
}

func TestPluginLibraryRemovalUsesLibraryIdentity(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 calls := 0
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  calls++
  body, status := `{"data":[{"relationships":{"library":{"data":[{"id":"l.fixture"}]}}}]}`, 200
  if calls == 1 {
   if r.Method != "GET" || r.URL.Path != "/v1/catalog/mx/albums/123" { t.Fatal("missing catalog lookup") }
  } else {
   if calls != 2 || r.Method != "DELETE" || r.URL.Path != "/v1/me/library/albums/l.fixture" { t.Fatal("wrong library identity or extra write") }
   body, status = "", 204
  }
  return &http.Response{StatusCode: status, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body))}, nil
 })}
 w := httptest.NewRecorder()
 (&APIServer{cachedToken:"fixture",mkMusicToken:"user-fixture"}).handlePluginCollection(w,httptest.NewRequest("DELETE","/catalog/collection?sf=mx&kind=albums&id=123&collection=library",nil))
 if w.Code != 200 || calls != 2 { t.Fatal("library removal failed") }
}

func TestPluginCollectionRedirectDoesNotForwardAccount(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 calls := 0
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  calls++
  return &http.Response{StatusCode:307,Header:http.Header{"Location":{"https://example.invalid"}},Body:io.NopCloser(strings.NewReader(""))},nil
 })}
 w:=httptest.NewRecorder()
 (&APIServer{cachedToken:"fixture",mkMusicToken:"user-fixture"}).handlePluginCollection(w,httptest.NewRequest("POST","/catalog/collection?sf=mx&kind=songs&id=123&collection=favorites",nil))
 if calls != 1 || w.Code != 502 { t.Fatal("mutation redirect followed") }
}

func TestPluginFavoriteStateOnlyRequestsSupportedExtensions(t *testing.T) {
 previous := artworkClient
 defer func() { artworkClient = previous }()
 artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
  if strings.Contains(r.URL.Query().Get("extend"), "inLibrary") { t.Fatal("library membership is a relationship, not an extension") }
  if strings.Contains(r.URL.Path, "/artists/") && r.URL.Query().Get("relate") != "" { t.Fatal("artist library relationship is unsupported") }
  return &http.Response{StatusCode:200,Header:make(http.Header),Body:io.NopCloser(strings.NewReader(`{"data":[]}`))},nil
 })}
 for _, kind := range []string{"songs","albums","artists","playlists"} {
  id := "123"
  if kind == "playlists" { id = "pl.fixture" }
  w:=httptest.NewRecorder()
  (&APIServer{cachedToken:"fixture",mkMusicToken:"user-fixture"}).handlePluginCollection(w,httptest.NewRequest("GET","/catalog/collection?sf=mx&kind="+kind+"&id="+id+"&collection=favorites",nil))
  if w.Code != 200 { t.Fatalf("state request failed: %s %d",kind,w.Code) }
 }
}
