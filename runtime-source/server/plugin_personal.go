package server

import (
 "io"
 "net/http"
 "net/url"
 "regexp"
 "strconv"
 "strings"
)

var pluginSongID = regexp.MustCompile(`^[0-9]{1,24}$`)
var pluginLyricLanguage = regexp.MustCompile(`^[a-z]{2,3}$`)

func pluginSongIDsValid(ids string) bool {
 parts := strings.Split(ids, ",")
 if len(parts) > 100 { return false }
 for _, id := range parts { if !pluginSongID.MatchString(id) { return false } }
 return true
}

var pluginArtistViews = map[string]bool{
 "top-songs": true, "featured-albums": true, "full-albums": true,
 "featured-playlists": true, "singles": true, "live-albums": true,
 "compilation-albums": true, "appears-on-albums": true, "similar-artists": true,
}

var pluginPlaylistID = regexp.MustCompile(`^(pl\.[A-Za-z0-9._-]{1,100}|p\.[A-Za-z0-9_-]{1,100}|none)$`)

// I keep personal feeds and playlist pagination on fixed authenticated paths.
func (s *APIServer) handlePluginPersonal(w http.ResponseWriter, r *http.Request) {
 q := r.URL.Query()
 sf, section, id := q.Get("sf"), q.Get("section"), q.Get("id")
 artistView := strings.TrimPrefix(section, "artist-")
 isArtistView := strings.HasPrefix(section, "artist-") && pluginArtistViews[artistView]
 offset, err := strconv.Atoi(q.Get("offset"))
 if !pluginStorefront.MatchString(sf) || !((section != "songs" && section != "lyrics" && section != "artist" && section != "artist-albums" && !isArtistView && pluginPlaylistID.MatchString(id)) || ((section == "songs" || section == "lyrics" || section == "artist" || section == "artist-albums" || isArtistView) && pluginSongIDsValid(id))) || err != nil || offset < 0 || offset > 10000 {
  http.Error(w, "invalid personal parameters", 400); return
 }
 path := ""
 switch section {
 case "artist", "artist-albums":
  if !pluginSongID.MatchString(id) { http.Error(w, "invalid artist", 400); return }
  path = "/v1/catalog/"+sf+"/artists/"+id
  if section == "artist-albums" { path += "/albums" }
 case "songs": path = "/v1/catalog/"+sf+"/songs"
 case "lyrics":
  if strings.Contains(id, ",") { http.Error(w, "invalid song", 400); return }
  path = "/v1/catalog/"+sf+"/songs/"+id+"/lyrics"
 case "recommendations": path = "/v1/me/recommendations"
 case "library": path = "/v1/me/library/playlists"
 case "folder-root": path = "/v1/me/library/playlist-folders"
 case "folder-children":
  if !strings.HasPrefix(id, "p.") { http.Error(w, "invalid folder", 400); return }
  path = "/v1/me/library/playlist-folders/"+id+"/children"
 case "library-songs", "library-albums", "library-artists": path = "/v1/me/library/"+strings.TrimPrefix(section, "library-")
 case "playlist", "tracks":
  if id == "none" { http.Error(w, "missing playlist", 400); return }
  path = "/v1/catalog/"+sf+"/playlists/"+id
  if strings.HasPrefix(id, "p.") { path = "/v1/me/library/playlists/"+id }
  if section == "tracks" { path += "/tracks" }
 default:
  if !isArtistView || !pluginSongID.MatchString(id) { http.Error(w, "invalid personal section", 400); return }
  path = "/v1/catalog/"+sf+"/artists/"+id+"/view/"+artistView
 }

 token := s.token()
 if token == "" { http.Error(w, "sign in required", 401); return }
 user := s.musicUserToken()
 if user == "" { http.Error(w, "sign in required", 401); return }
 query := url.Values{"limit": {"100"}, "offset": {strconv.Itoa(offset)}}
 if strings.HasPrefix(section, "library-") { query.Set("include", "catalog"); query.Set("extend", "inFavorites") }
 if section == "library" { query.Set("extend", "tags,inFavorites") }
 if section == "folder-root" { query = url.Values{"filter[identity]": {"playlistsroot"}} }
 if section == "recommendations" { query.Set("limit", "25") }
 if section == "songs" { query = url.Values{"ids": {id}, "include": {"albums,artists"}} }
 if section == "lyrics" {
  query = url.Values{"extend": {"ttmlLocalizations"}}
  if language := q.Get("lang"); language != "" {
   if !pluginLyricLanguage.MatchString(language) { http.Error(w, "invalid lyrics language", 400); return }
   query.Set("l", language)
  }
 }
 if section == "artist-albums" { query.Set("limit", "25") }
 if section == "playlist" || section == "artist" { query = url.Values{} }
 if section == "artist" {
  query.Set("views", "top-songs,featured-albums,full-albums,featured-playlists,singles,live-albums,compilation-albums,appears-on-albums,similar-artists")
  query.Set("include[songs]", "albums,artists")
  query.Set("include[albums]", "artists")
 }
 if isArtistView {
  query.Set("limit", "25")
  query.Set("include[songs]", "albums,artists")
  query.Set("include[albums]", "artists")
  query.Set("with", "attributes")
 }
 if section == "tracks" && strings.HasPrefix(id, "p.") { query.Set("include", "catalog") }
 req, _ := http.NewRequestWithContext(r.Context(), "GET", "https://amp-api.music.apple.com"+path+"?"+query.Encode(), nil)
 req.Header.Set("Music-User-Token", user)
 if section == "lyrics" { req.AddCookie(&http.Cookie{Name: "media-user-token", Value: user}) }
 req.Header.Set("Authorization", "Bearer "+token)
 req.Header.Set("Origin", "https://music.apple.com")
 client := *artworkClient
 client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
 response, err := client.Do(req)
 if err != nil { http.Error(w, "discovery unavailable", 502); return }
 defer response.Body.Close()
 if response.StatusCode == 404 && (section == "lyrics" || section == "songs") {
  w.Header().Set("Content-Type", "application/json")
  w.Header().Set("Cache-Control", "no-store")
  w.Write([]byte(`{"data":[]}`)); return
 }
 if response.StatusCode != 200 {
  w.Header().Set("Retry-After", response.Header.Get("Retry-After"))
  w.Header().Set("X-Plugin-Upstream-Status", strconv.Itoa(response.StatusCode))
  status := response.StatusCode
  if status != 401 && status != 403 && status != 429 { status = 502 }
  http.Error(w, "discovery unavailable", status); return
 }
 body, err := io.ReadAll(io.LimitReader(response.Body, 8*1024*1024+1))
 if err != nil || len(body) > 8*1024*1024 { http.Error(w, "discovery response too large", 502); return }
 w.Header().Set("Content-Type", "application/json")
 w.Header().Set("Cache-Control", "no-store")
 w.Write(body)
}
