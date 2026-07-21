package main

import (
	"net/http"
	"testing"
)

func TestApplyCPAHeadersUsesAuthFileHeaders(t *testing.T) {
	req, err := http.NewRequest(http.MethodPost, "https://example.com/v1/responses", nil)
	if err != nil {
		t.Fatal(err)
	}
	auth := cpaAuthConfig{
		AccessToken: "access-token",
		Headers: map[string]string{
			"X-XAI-Token-Auth":         "xai-grok-cli",
			"x-grok-client-identifier": "grok-pager",
		},
	}

	applyCPAHeaders(req, auth, "0.2.93")

	if got := req.Header.Get("X-XAI-Token-Auth"); got != "xai-grok-cli" {
		t.Fatalf("X-XAI-Token-Auth = %q", got)
	}
	if got := req.Header.Get("x-grok-client-identifier"); got != "grok-pager" {
		t.Fatalf("x-grok-client-identifier = %q", got)
	}
	if got := req.Header.Get("Authorization"); got != "Bearer access-token" {
		t.Fatalf("Authorization = %q", got)
	}
}
