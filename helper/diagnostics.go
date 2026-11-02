package main

import (
	"context"
	"crypto/x509"
	"errors"
	"net"
	"os"
	"strings"
	"time"
)

// Return only fixed categories. Server replies, TLS payloads, addresses and
// credentials must never flow into diagnostic output.
func failureCategory(err error) string {
	var command redisError
	if errors.As(err, &command) {
		code, _, _ := strings.Cut(string(command), " ")
		switch code {
		case "WRONGPASS", "NOAUTH":
			return "authentication"
		case "NOPERM":
			return "authorization"
		case "NOQUORUM", "NOGOODSLAVE":
			return "quorum"
		}
	}
	var host x509.HostnameError
	var authority x509.UnknownAuthorityError
	var certificate x509.CertificateInvalidError
	if errors.As(err, &host) || errors.As(err, &authority) || errors.As(err, &certificate) {
		return "tls_identity"
	}
	var timeout net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &timeout) && timeout.Timeout()) {
		return "timeout"
	}
	if errors.Is(err, context.Canceled) {
		return "stopping"
	}
	return "connection"
}
func (h *checker) waiting(now time.Time) bool {
	info, err := os.Stat(h.cfg.waiting)
	if err != nil {
		return false
	}
	age := now.Sub(info.ModTime())
	return age >= 0 && age < h.cfg.heartbeat
}
func (h *checker) report(current status, now time.Time) {
	if h.logf == nil {
		return
	}
	category, live := current.category, current.live
	if !current.ready && h.waiting(now) {
		category = "waiting"
		live = true
	}
	signature := category
	if current.ready {
		signature += "/ready"
	}
	if live {
		signature += "/live"
	}
	age := now.Sub(h.lastLog)
	if !h.lastLog.IsZero() && (age < 10*time.Second || (signature == h.lastLogSignature && (current.ready || age < time.Minute))) {
		return
	}
	h.lastLog = now
	h.lastLogSignature = signature
	h.logf("%s health live=%t ready=%t reason=%s", h.cfg.target, live, current.ready, category)
}
