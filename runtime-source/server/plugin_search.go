package server

import (
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

var pluginStorefront = regexp.MustCompile(`^[a-z]{2}$`)

// handlePluginSearch exposes catalog entity search, never a URL proxy.
func (s *APIServer) handlePluginSearch(w http.ResponseWriter, r *http.Request) {
	sf, term := r.URL.Query().Get("sf"), strings.TrimSpace(r.URL.Query().Get("term"))
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if !pluginStorefront.MatchString(sf) || term == "" || len(term) > 1024 || err != nil || limit < 1 || limit > 25 {
		http.Error(w, "invalid search parameters", http.StatusBadRequest)
		return
	}
	token := s.token()
	if token == "" {
		http.Error(w, "sign in required", http.StatusUnauthorized)
		return
	}
	query := url.Values{"term": {term}, "types": {"songs,albums,artists,playlists"}, "limit": {strconv.Itoa(limit)}, "include[songs]": {"albums,artists"}, "include[albums]": {"artists"}}
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, "https://amp-api.music.apple.com/v1/catalog/"+sf+"/search?"+query.Encode(), nil)
	if err != nil {
		http.Error(w, "invalid search request", http.StatusBadRequest)
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Origin", "https://music.apple.com")
	req.Header.Set("User-Agent", "Music/4.9")
	// Reject redirects instead of forwarding account authorization to another host.
	client := *artworkClient
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	response, err := client.Do(req)
	if err != nil {
		http.Error(w, "catalog unavailable", http.StatusBadGateway)
		return
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		w.Header().Set("Retry-After", response.Header.Get("Retry-After"))
		status := response.StatusCode
		if status != 401 && status != 403 && status != 429 {
			status = http.StatusBadGateway
		}
		http.Error(w, "catalog search failed", status)
		return
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, 8*1024*1024+1))
	if err != nil || len(body) > 8*1024*1024 {
		http.Error(w, "catalog response too large", http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Write(body)
}

// handlePluginStorefront returns only the account's catalog region.
func (s *APIServer) handlePluginStorefront(w http.ResponseWriter, r *http.Request) {
	token, user := s.token(), s.mediaUserToken()
	if token == "" || user == "" {
		http.Error(w, "sign in required", 401)
		return
	}
	req, _ := http.NewRequestWithContext(r.Context(), "GET", "https://amp-api.music.apple.com/v1/me/storefront", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Music-User-Token", user)
	req.Header.Set("Origin", "https://music.apple.com")
	client := *artworkClient
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	response, err := client.Do(req)
	if err != nil {
		http.Error(w, "account region unavailable", 502)
		return
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		status := response.StatusCode
        if status != 401 && status != 403 && status != 429 { status = 502 }
        w.Header().Set("Retry-After", response.Header.Get("Retry-After"))
        http.Error(w, "account region unavailable", status)
        return
    }
    var result struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 65536)).Decode(&result); err != nil || len(result.Data) != 1 || !pluginStorefront.MatchString(result.Data[0].ID) {
		http.Error(w, "invalid account region", 502)
		return
	}
	writeJSON(w, 200, map[string]string{"storefront": result.Data[0].ID})
}
