package main

import (
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestHealthDiagnosticsSanitizedAndRateLimited(t *testing.T) {
	cfg := baseConfig(t, "unused", "redis")
	var lines []string
	h := &checker{cfg: cfg, logf: func(format string, args ...interface{}) { lines = append(lines, fmt.Sprintf(format, args...)) }}
	start := time.Now()
	failed := status{category: failureCategory(redisError("WRONGPASS do-not-print-this-secret"))}
	h.report(failed, start)
	for i := 1; i < 10; i++ {
		h.report(status{category: "ready", ready: true, live: true}, start.Add(time.Duration(i)*time.Second))
	}
	if len(lines) != 1 {
		t.Fatal("transition flapping was not rate limited")
	}
	h.report(failed, start.Add(59*time.Second))
	if len(lines) != 1 {
		t.Fatal("repeated failure logging was not bounded")
	}
	h.report(failed, start.Add(60*time.Second))
	h.report(status{category: "ready", ready: true, live: true}, start.Add(70*time.Second))
	if len(lines) != 3 {
		t.Fatal("failure reminder/recovery transition missing")
	}
	joined := strings.Join(lines, "\n")
	if strings.Contains(joined, "do-not-print") || !strings.Contains(joined, "reason=authentication") || !strings.Contains(joined, "reason=ready") {
		t.Fatal("unsafe or missing diagnostics")
	}
	if failureCategory(errors.New("sensitive-payload")) != "connection" {
		t.Fatal("unknown payload surfaced")
	}
}
