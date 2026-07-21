package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type cpaAuthConfig struct {
	AccessToken string            `json:"access_token"`
	BaseURL     string            `json:"base_url"`
	Email       string            `json:"email"`
	Headers     map[string]string `json:"headers"`
}

type cpaClaims struct {
	Exp    int64  `json:"exp"`
	Sub    string `json:"sub"`
	TeamID string `json:"team_id"`
}

type checkResult struct {
	File       string
	Email      string
	HTTPStatus int
	Elapsed    time.Duration
	Sub        string
	TeamID     string
	Exp        int64
	Summary    string
	Action     string
	Err        error
}

func main() {
	pattern := flag.String("pattern", "cpa/xai-*.json", "auth json file glob")
	workers := flag.Int("workers", 8, "concurrent workers")
	proxy := flag.String("proxy", defaultCPAProxy(), "HTTP proxy URL; empty disables explicit proxy")
	timeout := flag.Duration("timeout", 20*time.Second, "per-account timeout")
	model := flag.String("model", "grok-4.5", "model to test")
	prompt := flag.String("prompt", "ping", "test prompt")
	clientVersion := flag.String("client-version", "0.2.93", "x-grok-client-version value")
	maxOutputTokens := flag.Int("max-output-tokens", 1, "max output tokens")
	deleteStatusesText := flag.String("delete-statuses", "", "comma-separated HTTP statuses to delete; empty disables deletion candidates")
	dryRun := flag.Bool("dry-run", true, "print deletion candidates without removing files")
	yes := flag.Bool("yes", false, "delete candidates without prompting")
	flag.Parse()

	files, err := filepath.Glob(*pattern)
	if err != nil {
		fatalf("bad pattern: %v", err)
	}
	sort.Strings(files)
	if len(files) == 0 {
		fatalf("no files matched %q", *pattern)
	}
	if *workers < 1 {
		*workers = 1
	}

	deleteStatuses, err := parseStatusSet(*deleteStatusesText)
	if err != nil {
		fatalf("bad delete-statuses: %v", err)
	}
	client, err := newCPAHTTPClient(*proxy, *workers)
	if err != nil {
		fatalf("bad proxy: %v", err)
	}

	fmt.Printf("pattern=%s files=%d workers=%d proxy=%s dry_run=%t delete_statuses=%s\n",
		*pattern, len(files), *workers, proxyLabel(*proxy), *dryRun, *deleteStatusesText)

	jobs := make(chan string)
	results := make(chan checkResult)
	var waitGroup sync.WaitGroup
	for workerID := 0; workerID < *workers; workerID++ {
		waitGroup.Add(1)
		go func() {
			defer waitGroup.Done()
			for file := range jobs {
				results <- checkCPAAuth(client, file, *model, *prompt, *clientVersion, *maxOutputTokens, *timeout, deleteStatuses)
			}
		}()
	}
	go func() {
		for _, file := range files {
			jobs <- file
		}
		close(jobs)
		waitGroup.Wait()
		close(results)
	}()

	var collected []checkResult
	for result := range results {
		collected = append(collected, result)
	}
	sort.Slice(collected, func(left, right int) bool {
		return collected[left].File < collected[right].File
	})

	var candidates []checkResult
	var okCount, candidateCount, keptCount, errorCount int
	for _, result := range collected {
		printCPAResult(result)
		switch result.Action {
		case "OK":
			okCount++
		case "DELETE_CANDIDATE":
			candidateCount++
			candidates = append(candidates, result)
		case "ERROR":
			errorCount++
		default:
			keptCount++
		}
	}

	fmt.Printf("summary ok=%d delete_candidates=%d kept=%d errors=%d\n",
		okCount, candidateCount, keptCount, errorCount)
	if len(candidates) == 0 {
		return
	}

	fmt.Println("delete candidates:")
	for _, candidate := range candidates {
		fmt.Printf("- %s http=%d email=%s summary=%s\n", candidate.File, candidate.HTTPStatus, candidate.Email, candidate.Summary)
	}
	if *dryRun {
		fmt.Println("dry-run enabled; no files deleted")
		return
	}
	if !*yes && !confirmDeletion(len(candidates)) {
		fmt.Println("delete cancelled")
		return
	}

	deleted, failed := deleteCandidates(candidates)
	fmt.Printf("delete summary deleted=%d failed=%d\n", deleted, failed)
}

func checkCPAAuth(client *http.Client, file string, model string, prompt string, clientVersion string, maxOutputTokens int, timeout time.Duration, deleteStatuses map[int]bool) checkResult {
	started := time.Now()
	auth, err := readCPAAuth(file)
	if err != nil {
		return checkResult{File: file, Elapsed: time.Since(started).Round(time.Millisecond), Action: "ERROR", Err: err}
	}
	claims := decodeCPAClaims(auth.AccessToken)
	result := checkResult{
		File:   file,
		Email:  auth.Email,
		Sub:    claims.Sub,
		TeamID: claims.TeamID,
		Exp:    claims.Exp,
	}
	if auth.AccessToken == "" {
		result.Action = "ERROR"
		result.Err = fmt.Errorf("missing access_token")
		result.Elapsed = time.Since(started).Round(time.Millisecond)
		return result
	}

	endpoint := strings.TrimRight(auth.BaseURL, "/")
	if endpoint == "" {
		endpoint = "https://cli-chat-proxy.grok.com/v1"
	}
	endpoint += "/responses"
	body, err := json.Marshal(map[string]any{
		"model":             model,
		"input":             prompt,
		"max_output_tokens": maxOutputTokens,
		"store":             false,
	})
	if err != nil {
		result.Action = "ERROR"
		result.Err = err
		result.Elapsed = time.Since(started).Round(time.Millisecond)
		return result
	}

	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		result.Action = "ERROR"
		result.Err = err
		result.Elapsed = time.Since(started).Round(time.Millisecond)
		return result
	}
	applyCPAHeaders(req, auth, clientVersion)

	resp, err := client.Do(req)
	result.Elapsed = time.Since(started).Round(time.Millisecond)
	if err != nil {
		result.Action = "KEEP"
		result.Summary = "request failed: " + err.Error()
		return result
	}
	defer resp.Body.Close()
	result.HTTPStatus = resp.StatusCode
	result.Summary = compactCPAResponse(resp.Body)

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		result.Action = "OK"
		return result
	}
	if deleteStatuses[resp.StatusCode] {
		result.Action = "DELETE_CANDIDATE"
		return result
	}
	result.Action = "KEEP"
	return result
}

func applyCPAHeaders(req *http.Request, auth cpaAuthConfig, clientVersion string) {
	for key, value := range auth.Headers {
		key = strings.TrimSpace(key)
		if key == "" {
			continue
		}
		req.Header.Set(key, value)
	}
	req.Header.Set("Authorization", "Bearer "+auth.AccessToken)
	req.Header.Set("Content-Type", "application/json")
	if clientVersion != "" {
		req.Header.Set("x-grok-client-version", clientVersion)
	}
}

func confirmDeletion(count int) bool {
	fmt.Printf("Delete %d candidate file(s)? Type y to confirm: ", count)
	reader := bufio.NewReader(os.Stdin)
	answer, err := reader.ReadString('\n')
	if err != nil {
		return false
	}
	answer = strings.TrimSpace(strings.ToLower(answer))
	return answer == "y" || answer == "yes"
}

func deleteCandidates(candidates []checkResult) (int, int) {
	deleted := 0
	failed := 0
	for _, candidate := range candidates {
		if err := os.Remove(candidate.File); err != nil {
			failed++
			fmt.Printf("DELETE_FAILED file=%s err=%v\n", candidate.File, err)
			continue
		}
		deleted++
		fmt.Printf("DELETED file=%s\n", candidate.File)
	}
	return deleted, failed
}

func readCPAAuth(file string) (cpaAuthConfig, error) {
	var auth cpaAuthConfig
	data, err := os.ReadFile(file)
	if err != nil {
		return auth, err
	}
	if err := json.Unmarshal(data, &auth); err != nil {
		return auth, err
	}
	return auth, nil
}

func newCPAHTTPClient(proxyValue string, workers int) (*http.Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConns = workers * 2
	transport.MaxIdleConnsPerHost = workers * 2
	if proxyValue != "" {
		proxyURL, err := url.Parse(proxyValue)
		if err != nil {
			return nil, err
		}
		transport.Proxy = http.ProxyURL(proxyURL)
	}
	return &http.Client{Transport: transport}, nil
}

func defaultCPAProxy() string {
	for _, key := range []string{"http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"} {
		if value := os.Getenv(key); value != "" {
			return value
		}
	}
	return "http://localhost:7897"
}

func parseStatusSet(text string) (map[int]bool, error) {
	statuses := map[int]bool{}
	for _, part := range strings.Split(text, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		status, err := strconv.Atoi(part)
		if err != nil {
			return nil, err
		}
		statuses[status] = true
	}
	return statuses, nil
}

func decodeCPAClaims(token string) cpaClaims {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return cpaClaims{}
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return cpaClaims{}
	}
	var claims cpaClaims
	_ = json.Unmarshal(raw, &claims)
	return claims
}

func compactCPAResponse(reader io.Reader) string {
	data, err := io.ReadAll(io.LimitReader(reader, 2048))
	if err != nil {
		return "read failed: " + err.Error()
	}
	text := strings.ReplaceAll(string(data), "\r", "")
	text = strings.Join(strings.Fields(text), " ")
	if len(text) > 300 {
		return text[:300] + "..."
	}
	return text
}

func printCPAResult(result checkResult) {
	status := "-"
	if result.HTTPStatus > 0 {
		status = strconv.Itoa(result.HTTPStatus)
	}
	exp := "-"
	if result.Exp > 0 {
		exp = time.Unix(result.Exp, 0).UTC().Format(time.RFC3339)
	}
	errText := ""
	if result.Err != nil {
		errText = " err=" + result.Err.Error()
	}
	fmt.Printf("%s file=%s email=%s http=%s elapsed=%s exp=%s sub=%s team=%s summary=%s%s\n",
		result.Action,
		result.File,
		result.Email,
		status,
		result.Elapsed,
		exp,
		shortCPAID(result.Sub),
		shortCPAID(result.TeamID),
		result.Summary,
		errText,
	)
}

func shortCPAID(value string) string {
	if value == "" {
		return "-"
	}
	if len(value) <= 8 {
		return value
	}
	return value[:8]
}

func proxyLabel(proxy string) string {
	if proxy == "" {
		return "<disabled>"
	}
	return proxy
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
