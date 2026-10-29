package server

import (
 "io"
 "net/http"
 "net/url"
 "regexp"
 "strconv"
)

var pluginGenreID = regexp.MustCompile(`^[0-9]{1,8}$`)

// I expose only bounded regional charts and genre metadata.
func (s *APIServer) handlePluginDiscover(w http.ResponseWriter, r *http.Request) {
 q := r.URL.Query()
 sf, section, genre := q.Get("sf"), q.Get("section"), q.Get("genre")
 offset, err := strconv.Atoi(q.Get("offset"))
 if !pluginStorefront.MatchString(sf) || (section != "charts" && section != "genres") || !pluginGenreID.MatchString(genre) || err != nil || offset < 0 || offset > 200 {
  http.Error(w, "invalid discovery parameters", 400); return
 }
 token := s.token()
 if token == "" { http.Error(w, "sign in required", 401); return }
 query := url.Values{"limit": {"50"}}
 if section == "charts" {
  query.Set("types", "songs,albums"); query.Set("chart", "most-played"); query.Set("offset", strconv.Itoa(offset))
  if genre != "0" { query.Set("genre", genre) }
 }
 req, _ := http.NewRequestWithContext(r.Context(), "GET", "https://amp-api.music.apple.com/v1/catalog/"+sf+"/"+section+"?"+query.Encode(), nil)
 req.Header.Set("Authorization", "Bearer "+token)
 req.Header.Set("Origin", "https://music.apple.com")
 client := *artworkClient
 client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
 response, err := client.Do(req)
 if err != nil { http.Error(w, "discovery unavailable", 502); return }
 defer response.Body.Close()
 if response.StatusCode != 200 {
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
