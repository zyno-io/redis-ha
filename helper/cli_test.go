package main

import (
	"bytes"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCLIUsesCredentialEnvironmentAndRawOutput(t *testing.T) {
	s := fakeRedis(t, nil, func(args []string) string {
		if args[0] == "SENTINEL" {
			return "*4\r\n$5\r\nnode0\r\n:6379\r\n$-1\r\n*2\r\n+up\r\n:2\r\n"
		}
		if args[0] == "FAIL" {
			return "-ERR deliberately rejected\r\n"
		}
		return authReply(args)
	})
	host, port, err := net.SplitHostPort(s.listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("REDISCLI_AUTH", "test-password")
	t.Setenv("REDIS_USERNAME", "operator")
	t.Setenv("REDIS_TLS_ENABLED", "no")
	var output bytes.Buffer
	if err = runCLI([]string{"--raw", "-e", "-h", host, "-p", port, "SENTINEL", "MASTER", "redis"}, &output); err != nil {
		t.Fatal(err)
	}
	if output.String() != "node0\n6379\n\nup\n2\n" {
		t.Fatal("incorrect raw output", output.String())
	}
	output.Reset()
	if err = runCLI([]string{"-h", host, "-p", port, "FAIL"}, &output); err == nil {
		t.Fatal("Redis error returned success")
	}
	if output.Len() != 0 {
		t.Fatal("error returned output")
	}
}
func TestCLIWholeCommandDeadline(t *testing.T) {
	s := fakeRedis(t, nil, func(args []string) string {
		if args[0] == "PING" {
			return ""
		}
		return authReply(args)
	})
	cfg := baseConfig(t, s.listener.Addr().String(), "redis")
	started := time.Now()
	if err := executeCLI(cfg, []string{"PING"}, &bytes.Buffer{}); err == nil {
		t.Fatal("stalled command passed")
	}
	if time.Since(started) > time.Second {
		t.Fatal("whole-command deadline not enforced")
	}
}
func TestCLIRejectsInvalidOptionsAndTrust(t *testing.T) {
	dir := t.TempDir()
	credential := filepath.Join(dir, "password")
	if err := os.WriteFile(credential, []byte("test-password\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("REDISCLI_AUTH", "")
	t.Setenv("REDIS_PASSWORD_FILE", credential)
	t.Setenv("REDIS_TLS_ENABLED", "no")
	for _, args := range [][]string{{"--pass", "visible", "PING"}, {"-p", "invalid", "PING"}, {"-p", "65536", "PING"}, {"--unknown", "value", "PING"}, {"-t", "0", "PING"}, {"--tls", "--cacert", "missing", "--sni", "redis.fixture", "PING"}, {"-h"}, nil} {
		if _, _, err := cliConfig(args); err == nil {
			t.Fatal("invalid CLI accepted", strings.Join(args, " "))
		}
	}
	cfg, args, err := cliConfig([]string{"-h", "::1", "-p", "26379", "--user", "monitor", "PING"})
	if err != nil || cfg.address != "[::1]:26379" || cfg.username != "monitor" || len(args) != 1 {
		t.Fatal("CLI option parsing")
	}
}
