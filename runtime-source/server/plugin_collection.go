package server

import (
 "io"
 "net/http"
 "net/url"
 "regexp"
 "strings"
 "encoding/json"
 "strconv"
)

var pluginLibraryID = regexp.MustCompile(`^[a-z]\.[A-Za-z0-9_-]{1,100}$`)

// I bind account mutations to one validated resource and a fixed upstream host.
func (s *APIServer) handlePluginCollection(w http.ResponseWriter, r *http.Request) {
 q := r.URL.Query()
 kind, id, sf, collection := q.Get("kind"), q.Get("id"), q.Get("sf"), q.Get("collection")
 validKind := kind == "songs" || kind == "albums" || kind == "artists" || kind == "playlists"
 validID := pluginSongID.MatchString(id)
 if kind == "playlists" { validID = pluginPlaylistID.MatchString(id) && id != "none" }
 if !validKind || !validID || !pluginStorefront.MatchString(sf) || (collection != "favorites" && collection != "library") || (r.Method != "GET" && r.Method != "POST" && r.Method != "DELETE") {
  http.Error(w, "invalid collection operation", 400); return
 }
 token, user := s.token(), s.musicUserToken()
 if token == "" || user == "" { http.Error(w, "sign in required", 401); return }
 client := *artworkClient
 client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
 send := func(method, path string, query url.Values) (*http.Response, error) {
  req, err := http.NewRequestWithContext(r.Context(), method, "https://amp-api.music.apple.com"+path+"?"+query.Encode(), nil)
  if err != nil { return nil, err }
  req.Header.Set("Authorization", "Bearer "+token)
  req.Header.Set("Music-User-Token", user)
  req.Header.Set("Origin", "https://music.apple.com")
  return client.Do(req)
 }
 forwardError := func(response *http.Response) {
  w.Header().Set("Retry-After", response.Header.Get("Retry-After"))
  status := response.StatusCode
  w.Header().Set("X-Plugin-Upstream-Status", strconv.Itoa(status))
  if status != 401 && status != 403 && status != 429 && status != 404 { status = 502 }
  http.Error(w, "collection operation failed", status)
 }
 path := "/v1/catalog/"+sf+"/"+kind+"/"+id
 if strings.HasPrefix(id, "p.") { path = "/v1/me/library/playlists/"+id }
 query := url.Values{"extend": {"inFavorites,personalRating"}, "relate": {"library"}}
 if kind == "artists" || strings.HasPrefix(id, "p.") { query = url.Values{"extend": {"inFavorites,personalRating"}} }
 if r.Method == "GET" || (collection == "library" && r.Method == "DELETE") {
  response, err := send("GET", path, query)
  if err != nil { http.Error(w, "collection unavailable", 502); return }
  defer response.Body.Close()
  if response.StatusCode != 200 { forwardError(response); return }
  body, err := io.ReadAll(io.LimitReader(response.Body, 1024*1024+1))
  if err != nil || len(body) > 1024*1024 { http.Error(w, "invalid collection response", 502); return }
  if r.Method == "GET" { w.Header().Set("Content-Type", "application/json"); w.Header().Set("Cache-Control", "no-store"); w.Write(body); return }
  var document struct { Data []struct { Relationships struct { Library struct { Data []struct { ID string `json:"id"` } `json:"data"` } `json:"library"` } `json:"relationships"` } `json:"data"` }
  if json.Unmarshal(body, &document) != nil { http.Error(w, "invalid library relationship", 502); return }
  libraryID := ""
  if len(document.Data) == 1 && len(document.Data[0].Relationships.Library.Data) == 1 { libraryID = document.Data[0].Relationships.Library.Data[0].ID }
  if strings.HasPrefix(id, "p.") { libraryID = id }
  if !pluginLibraryID.MatchString(libraryID) { http.Error(w, "resource not in library", 404); return }
  path, query = "/v1/me/library/"+kind+"/"+libraryID, url.Values{}
 } else {
  resourceType := kind
  if strings.HasPrefix(id, "p.") { resourceType = "library-playlists" }
  path, query = "/v1/me/"+collection, url.Values{"ids["+resourceType+"]": {id}}
 }
 response, err := send(r.Method, path, query)
 if err != nil { http.Error(w, "collection unavailable", 502); return }
 defer response.Body.Close()
 if response.StatusCode < 200 || response.StatusCode >= 300 { forwardError(response); return }
 writeJSON(w, 200, map[string]bool{"accepted": true})
}
