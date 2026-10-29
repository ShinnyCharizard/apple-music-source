package server

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"time"
)

func pluginAccount(user string) string {
	sum := sha256.Sum256([]byte(user))
	return hex.EncodeToString(sum[:])
}

func (s *APIServer) handlePluginReportingAccount(w http.ResponseWriter, r *http.Request) {
	user := s.musicUserToken()
	if user == "" {
		http.Error(w, "sign in required", 401)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"account": pluginAccount(user)})
}

type pluginListenSpan struct {
	Start int64 `json:"start"`
	End   int64 `json:"end"`
	At    int64 `json:"at"`
	Until int64 `json:"until"`
}
type pluginListen struct {
	Account  string             `json:"account"`
	ID       string             `json:"id"`
	Song     string             `json:"song"`
	Duration int64              `json:"duration_ms"`
	Reason   string             `json:"reason"`
	Spans    []pluginListenSpan `json:"spans"`
}

var pluginReportingSongID = regexp.MustCompile(`^[0-9]{1,24}$`)
var pluginEventID = regexp.MustCompile(`^[a-f0-9-]{36}$`)

// I keep account tokens inside the runtime and accept no caller-selected URL.
func (s *APIServer) handlePluginReportingPlay(w http.ResponseWriter, r *http.Request) {
	var event pluginListen
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 32768))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&event) != nil || !pluginReportingSongID.MatchString(event.Song) || !pluginEventID.MatchString(event.ID) ||
		event.Duration <= 0 || len(event.Spans) == 0 || len(event.Spans) > 96 {
		http.Error(w, "invalid listening event", 400)
		return
	}
	now := time.Now().UnixMilli()
	for _, span := range event.Spans {
		if span.Start < 0 || span.End <= span.Start || span.End > event.Duration+1000 || span.At <= 0 ||
			span.Until < span.At || span.Until > now+60000 || span.Until-span.At != span.End-span.Start {
			http.Error(w, "invalid listening interval", 400)
			return
		}
	}
	user, token := s.musicUserToken(), s.token()
	if user == "" || token == "" {
		http.Error(w, "sign in required", 401)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	if event.Account != pluginAccount(user) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]bool{"discard": true})
		return
	}
	data := pluginPlayActivities(event, now, s.storefront())
	for _, activity := range data {
		activity["developer-token"] = token
		activity["user-token"] = user
	}
	body, _ := json.Marshal(map[string]any{"client_id": "JSCLIENT", "event_type": "JSPLAY", "data": data})
	req, _ := http.NewRequestWithContext(r.Context(), "POST", "https://universal-activity-service.itunes.apple.com/play", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("media-user-token", user)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "Plugin/0.1.18")
	client := *artworkClient
	client.Timeout = 20 * time.Second
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	response, err := client.Do(req)
	if err != nil {
		http.Error(w, "listening delivery unavailable", 502)
		return
	}
	defer response.Body.Close()
	io.Copy(io.Discard, io.LimitReader(response.Body, 65536))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		status := response.StatusCode
		if status != 401 && status != 403 && status != 429 {
			status = 502
		}
		w.Header().Set("Retry-After", response.Header.Get("Retry-After"))
		http.Error(w, "listening delivery failed", status)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]bool{"accepted": true})
}

func pluginPlayActivities(event pluginListen, now int64, storefront string) []map[string]any {
	data := make([]map[string]any, 0, len(event.Spans)*2)
	for index, span := range event.Spans {
		for _, kind := range []int{1, 0} {
			stamp, end := span.At, span.Start
			if kind == 0 {
				stamp, end = span.Until, span.End
			}
			// Stable activity IDs make transport retries refer to the same listen.
			sum := sha256.Sum256([]byte(event.ID + ":" + strconv.Itoa(index*2+kind)))
			id := hex.EncodeToString(sum[:16])
			id = id[:8] + "-" + id[8:12] + "-" + id[12:16] + "-" + id[16:20] + "-" + id[20:]
			activity := map[string]any{
				"event-type": kind, "type": 1, "ids": map[string]string{"subscription-adam-id": event.Song},
				"start-position-in-milliseconds": span.Start, "end-position-in-milliseconds": end,
				"media-duration-in-milliseconds": event.Duration, "persistent-id": id,
				"source-type": 10, "media-type": 0, "store-front": storefront,
				"offline": false, "sb-enabled": true, "private-enabled": false, "siri-initiated": false,
				"bundle-id": "io.github.shinnycharizard.apple-music", "build-version": "0.1.18", "user-agent": "Plugin/0.1.18",
				"milliseconds-since-play": max(int64(0), now-stamp),
			}
			if kind == 0 {
				reason := 1
				if index < len(event.Spans)-1 {
					reason = 3
				} else if event.Reason == "finished" {
					reason = 7
				} else if event.Reason == "shutdown" {
					reason = 17
				}
				activity["end-reason-type"] = reason
			}
			data = append(data, activity)
		}
	}
	return data
}
