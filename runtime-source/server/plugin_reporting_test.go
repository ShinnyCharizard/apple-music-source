package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestPluginReportingOnlySendsMeasuredActivityToFixedAccountEndpoint(t *testing.T) {
	previous := artworkClient
	defer func() { artworkClient = previous }()
	now := time.Now().UnixMilli()
	event := pluginListen{Account: pluginAccount("fixture-user"), ID: "10000000-0000-4000-8000-000000000001", Song: "123",
		Duration: 120000, Reason: "finished", Spans: []pluginListenSpan{{Start: 80000, End: 81000, At: now - 1000, Until: now}}}
	calls := 0
	status := 204
	artworkClient = &http.Client{Transport: pluginSearchTransport(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.Method != "POST" || r.URL.String() != "https://universal-activity-service.itunes.apple.com/play" ||
			r.Header.Get("Authorization") != "Bearer fixture-developer" || r.Header.Get("media-user-token") != "fixture-user" {
			t.Fatal("report lost its fixed destination or credentials")
		}
		var payload struct {
			Client string           `json:"client_id"`
			Data   []map[string]any `json:"data"`
		}
		if json.NewDecoder(r.Body).Decode(&payload) != nil || payload.Client != "JSCLIENT" || len(payload.Data) != 2 {
			t.Fatal("report envelope is invalid")
		}
		end := payload.Data[1]
		if end["start-position-in-milliseconds"] != float64(80000) || end["end-position-in-milliseconds"] != float64(81000) || end["end-reason-type"] != float64(7) {
			t.Fatal("report counted the seek distance or lost its finish reason")
		}
		return &http.Response{StatusCode: status, Header: make(http.Header), Body: io.NopCloser(strings.NewReader("private-upstream-body"))}, nil
	})}
	server := &APIServer{cachedToken: "fixture-developer", mkMusicToken: "fixture-user"}
	send := func() *httptest.ResponseRecorder {
		body, _ := json.Marshal(event)
		w := httptest.NewRecorder()
		server.handlePluginReportingPlay(w, httptest.NewRequest("POST", "/reporting/play", bytes.NewReader(body)))
		if strings.Contains(w.Body.String(), "private") || strings.Contains(w.Body.String(), "fixture-user") {
			t.Fatal("private response leaked")
		}
		return w
	}
	if w := send(); w.Code != 200 || !strings.Contains(w.Body.String(), `"accepted":true`) {
		t.Fatal(w.Code, w.Body.String())
	}
	status = 429
	if w := send(); w.Code != 429 {
		t.Fatal("rate limit was acknowledged")
	}
	event.Account = "another-account"
	if w := send(); w.Code != 200 || !strings.Contains(w.Body.String(), `"discard":true`) || calls != 2 {
		t.Fatal("another account's event reached upstream")
	}
	event.Account = pluginAccount("fixture-user")
	event.Spans[0].Until++
	if w := send(); w.Code != 400 || calls != 2 {
		t.Fatal("unmeasured time reached upstream")
	}
}

func TestPluginActivityIDsRemainStableAcrossRetries(t *testing.T) {
	event := pluginListen{ID: "10000000-0000-4000-8000-000000000001", Song: "123", Duration: 10000,
		Spans: []pluginListenSpan{{Start: 0, End: 1000, At: 1000, Until: 2000}}}
	first, second := pluginPlayActivities(event, 3000, "143467"), pluginPlayActivities(event, 4000, "143467")
	if first[0]["persistent-id"] != second[0]["persistent-id"] || first[1]["persistent-id"] != second[1]["persistent-id"] || first[0]["persistent-id"] == first[1]["persistent-id"] {
		t.Fatal("activity IDs changed on retry or collapsed distinct events")
	}
	if second[1]["milliseconds-since-play"] != int64(2000) {
		t.Fatal("retry changed the time of listening")
	}
}
