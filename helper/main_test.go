package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

type fakeServer struct {
	listener    net.Listener
	connections atomic.Int32
	mu          sync.Mutex
	active      []net.Conn
}

func fakeRedis(t *testing.T, listener net.Listener, reply func([]string) string) *fakeServer {
	t.Helper()
	if listener == nil {
		var err error
		listener, err = net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
	}
	s := &fakeServer{listener: listener}
	t.Cleanup(func() {
		_ = listener.Close()
		s.mu.Lock()
		defer s.mu.Unlock()
		for _, c := range s.active {
			_ = c.Close()
		}
	})
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			s.connections.Add(1)
			s.mu.Lock()
			s.active = append(s.active, conn)
			s.mu.Unlock()
			go func() {
				defer conn.Close()
				reader := bufio.NewReader(conn)
				for {
					request, err := response(reader)
					if err != nil {
						return
					}
					values, ok := request.([]interface{})
					if !ok {
						return
					}
					args := make([]string, len(values))
					for i, v := range values {
						args[i], ok = v.(string)
						if !ok {
							return
						}
					}
					answer := reply(args)
					if answer == "" {
						continue
					}
					if _, err = fmt.Fprint(conn, answer); err != nil {
						return
					}
				}
			}()
		}
	}()
	return s
}
func bulk(value string) string { return fmt.Sprintf("$%d\r\n%s\r\n", len(value), value) }
func baseConfig(t *testing.T, address, target string) config {
	t.Helper()
	dir := t.TempDir()
	return config{target: target, address: address, username: "operator", password: "test-password", timeout: 100 * time.Millisecond,
		interval: time.Second, freshness: time.Second, heartbeat: 60 * time.Second, waiting: filepath.Join(dir, "waiting"),
		seed: filepath.Join(dir, "seed"), master: "redis", quorum: true}
}
func authReply(args []string) string {
	if args[0] == "AUTH" {
		if len(args) != 3 || args[1] != "operator" || args[2] != "test-password" {
			return "-WRONGPASS authentication failed\r\n"
		}
		return "+OK\r\n"
	}
	if args[0] == "PING" {
		return "+PONG\r\n"
	}
	return "-ERR unsupported\r\n"
}
func TestPersistentConnectionAndReadiness(t *testing.T) {
	s := fakeRedis(t, nil, func(args []string) string {
		if args[0] == "INFO" {
			return bulk("role:slave\r\nmaster_link_status:up\r\nmaster_sync_in_progress:0\r\n")
		}
		return authReply(args)
	})
	cfg := baseConfig(t, s.listener.Addr().String(), "redis")
	h := &checker{cfg: cfg, redis: &client{cfg: cfg}}
	defer h.redis.close()
	for range 8 {
		h.poll(context.Background())
		if !h.healthy("/ready", time.Now()) {
			t.Fatal("synchronized replica unready")
		}
	}
	if s.connections.Load() != 1 {
		t.Fatalf("used %d connections", s.connections.Load())
	}
	for range 8 {
		writer := httptest.NewRecorder()
		h.ServeHTTP(writer, httptest.NewRequest("GET", "/ready", nil))
		if writer.Code != http.StatusOK {
			t.Fatal(writer.Code)
		}
	}
	if s.connections.Load() != 1 {
		t.Fatal("HTTP initiated Redis work")
	}
}
func TestQuorumFailureDoesNotFailLiveness(t *testing.T) {
	s := fakeRedis(t, nil, func(args []string) string {
		if args[0] == "SENTINEL" {
			return "-NOQUORUM no quorum\r\n"
		}
		return authReply(args)
	})
	cfg := baseConfig(t, s.listener.Addr().String(), "sentinel")
	h := &checker{cfg: cfg, redis: &client{cfg: cfg}}
	defer h.redis.close()
	h.poll(context.Background())
	if h.healthy("/ready", time.Now()) || !h.healthy("/live", time.Now()) {
		t.Fatal("quorum failure must affect readiness only")
	}
}
func TestTimeoutInvalidatesPositiveAndReconnects(t *testing.T) {
	var stall atomic.Bool
	s := fakeRedis(t, nil, func(args []string) string {
		if args[0] == "SENTINEL" {
			if stall.Load() {
				return ""
			}
			return "+OK quorum reached\r\n"
		}
		return authReply(args)
	})
	cfg := baseConfig(t, s.listener.Addr().String(), "sentinel")
	h := &checker{cfg: cfg, redis: &client{cfg: cfg}}
	defer h.redis.close()
	h.poll(context.Background())
	if !h.healthy("/ready", time.Now()) {
		t.Fatal("initial readiness")
	}
	stall.Store(true)
	h.poll(context.Background())
	if h.healthy("/live", time.Now()) || h.healthy("/ready", time.Now()) {
		t.Fatal("timeout retained successful health")
	}
	stall.Store(false)
	h.poll(context.Background())
	if !h.healthy("/ready", time.Now()) {
		t.Fatal("did not reconnect")
	}
	if s.connections.Load() != 2 {
		t.Fatal("timed-out connection was reused")
	}
}
func TestAuthenticationFailure(t *testing.T) {
	s := fakeRedis(t, nil, authReply)
	cfg := baseConfig(t, s.listener.Addr().String(), "sentinel")
	cfg.password = "wrong"
	h := &checker{cfg: cfg, redis: &client{cfg: cfg}}
	defer h.redis.close()
	h.poll(context.Background())
	if h.healthy("/live", time.Now()) || h.healthy("/ready", time.Now()) {
		t.Fatal("invalid authentication accepted")
	}
}
func TestSeedEligibilityAndReplicaStates(t *testing.T) {
	var synchronized atomic.Bool
	var priority atomic.Int32
	s := fakeRedis(t, nil, func(args []string) string {
		if args[0] == "INFO" {
			link, syncing := "down", "1"
			if synchronized.Load() {
				link, syncing = "up", "0"
			}
			return bulk("role:slave\r\nmaster_link_status:" + link + "\r\nmaster_sync_in_progress:" + syncing + "\r\n")
		}
		if args[0] == "CONFIG" {
			if strings.Join(args, " ") != "CONFIG SET replica-priority 100" {
				return "-ERR invalid priority\r\n"
			}
			priority.Store(100)
			return "+OK\r\n"
		}
		return authReply(args)
	})
	cfg := baseConfig(t, s.listener.Addr().String(), "redis")
	if err := os.WriteFile(cfg.seed, []byte("100\n"), 0600); err != nil {
		t.Fatal(err)
	}
	h := &checker{cfg: cfg, redis: &client{cfg: cfg}}
	defer h.redis.close()
	h.poll(context.Background())
	if h.healthy("/ready", time.Now()) || priority.Load() != 0 {
		t.Fatal("unsynchronized member eligible")
	}
	synchronized.Store(true)
	h.poll(context.Background())
	if !h.healthy("/ready", time.Now()) || priority.Load() != 100 {
		t.Fatal("synchronized member not eligible")
	}
	if _, err := os.Stat(cfg.seed); !os.IsNotExist(err) {
		t.Fatal("seed marker not removed")
	}
}
func TestReadinessRequirements(t *testing.T) {
	for _, tc := range []struct {
		name, info string
		minimum    int
		ready      bool
	}{
		{"master-no-replica", "role:master\r\nconnected_slaves:0\r\n", 1, false},
		{"master-with-replica", "role:master\r\nconnected_slaves:1\r\n", 1, true},
		{"master-default", "role:master\r\nconnected_slaves:0\r\n", 0, true},
		{"replica-sync", "role:slave\r\nmaster_link_status:up\r\nmaster_sync_in_progress:1\r\n", 0, false},
		{"unknown-role", "role:other\r\n", 0, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := fakeRedis(t, nil, func(args []string) string {
				if args[0] == "INFO" {
					return bulk(tc.info)
				}
				return authReply(args)
			})
			cfg := baseConfig(t, s.listener.Addr().String(), "redis")
			cfg.minReplicas = tc.minimum
			h := &checker{cfg: cfg, redis: &client{cfg: cfg}}
			defer h.redis.close()
			h.poll(context.Background())
			if h.healthy("/ready", time.Now()) != tc.ready {
				t.Fatal("unexpected readiness")
			}
		})
	}
}

func TestSeedAuthorizationFailureRetainsMarker(t *testing.T) {
	s := fakeRedis(t, nil, func(args []string) string {
		if args[0] == "INFO" {
			return bulk("role:slave\r\nmaster_link_status:up\r\nmaster_sync_in_progress:0\r\n")
		}
		if args[0] == "CONFIG" {
			return "-NOPERM command denied\r\n"
		}
		return authReply(args)
	})
	cfg := baseConfig(t, s.listener.Addr().String(), "redis")
	if err := os.WriteFile(cfg.seed, []byte("100\n"), 0600); err != nil {
		t.Fatal(err)
	}
	h := &checker{cfg: cfg, redis: &client{cfg: cfg}}
	defer h.redis.close()
	h.poll(context.Background())
	if h.healthy("/ready", time.Now()) || !h.healthy("/live", time.Now()) {
		t.Fatal("seed transition failure health")
	}
	if _, err := os.Stat(cfg.seed); err != nil {
		t.Fatal("failed seed transition removed marker")
	}
}

func TestAtomicInstallCanRetryReadOnlyOutput(t *testing.T) {
	path := filepath.Join(t.TempDir(), "binary")
	if err := installFile(path, []byte("first")); err != nil {
		t.Fatal(err)
	}
	if err := installFile(path, []byte("second")); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "second" {
		t.Fatal("retry copy failed")
	}
	stat, err := os.Stat(path)
	if err != nil || stat.Mode().Perm() != 0555 {
		t.Fatal("installed file mode")
	}
}
func TestStartupHeartbeatAndExpiredCache(t *testing.T) {
	cfg := baseConfig(t, "unused", "redis")
	h := &checker{cfg: cfg}
	if h.healthy("/live", time.Now()) || h.healthy("/ready", time.Now()) {
		t.Fatal("startup retained success")
	}
	if err := os.WriteFile(cfg.waiting, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if !h.healthy("/live", time.Now()) || h.healthy("/ready", time.Now()) {
		t.Fatal("heartbeat readiness/liveness")
	}
	old := time.Now().Add(-2 * time.Minute)
	if err := os.Chtimes(cfg.waiting, old, old); err != nil {
		t.Fatal(err)
	}
	h.status = status{at: time.Now().Add(-2 * time.Second), ready: true, live: true}
	if h.healthy("/live", time.Now()) || h.healthy("/ready", time.Now()) {
		t.Fatal("stale cache accepted")
	}
}
func TestTLSIdentityVerification(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "redis.fixture"}, DNSNames: []string{"redis.fixture"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	private, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: private})
	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12})
	if err != nil {
		t.Fatal(err)
	}
	s := fakeRedis(t, listener, func(args []string) string {
		if args[0] == "SENTINEL" {
			return "+OK quorum\r\n"
		}
		return authReply(args)
	})
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(certPEM)
	for _, tc := range []struct {
		name  string
		pool  *x509.CertPool
		ready bool
	}{{"redis.fixture", pool, true}, {"wrong.fixture", pool, false}, {"redis.fixture", x509.NewCertPool(), false}} {
		cfg := baseConfig(t, s.listener.Addr().String(), "sentinel")
		cfg.tls = &tls.Config{RootCAs: tc.pool, ServerName: tc.name, MinVersion: tls.VersionTLS12}
		h := &checker{cfg: cfg, redis: &client{cfg: cfg}}
		h.poll(context.Background())
		h.redis.close()
		if h.healthy("/ready", time.Now()) != tc.ready {
			t.Fatal("TLS identity verification", tc.name)
		}
		var raw bytes.Buffer
		cliError := executeCLI(cfg, []string{"PING"}, &raw)
		if (cliError == nil) != tc.ready {
			t.Fatal("CLI TLS identity verification", tc.name)
		}
		if tc.ready && raw.String() != "PONG\n" {
			t.Fatal("CLI TLS output")
		}
	}
}
func TestRESPBounds(t *testing.T) {
	for _, source := range []string{"$1048577\r\n", "*257\r\n", "+" + strings.Repeat("x", 10000) + "\r\n", "$3\r\nx\r\n"} {
		if _, err := response(bufio.NewReader(strings.NewReader(source))); err == nil {
			t.Fatal("accepted oversized/invalid reply")
		}
	}
}
func TestLauncherLifecycles(t *testing.T) {
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash required for launcher regression")
	}
	for _, tc := range []struct {
		name, target, helper string
		exit                 int
		signal               bool
	}{
		{"target-exit", "sleep 0.15; exit 3", "trap 'echo helper >> \"$SIGNAL_FILE\"; exit 0' TERM; while :; do sleep .02; done", 3, false},
		{"helper-exit", "trap 'echo target >> \"$SIGNAL_FILE\"; exit 0' TERM; while :; do sleep .02; done", "sleep .15; exit 7", 7, false},
		{"termination", "trap 'echo target >> \"$SIGNAL_FILE\"; exit 0' TERM; while :; do sleep .02; done", "trap 'echo helper >> \"$SIGNAL_FILE\"; exit 0' TERM; while :; do sleep .02; done", 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			helper := filepath.Join(dir, "helper")
			target := filepath.Join(dir, "target")
			signals := filepath.Join(dir, "signals")
			for path, source := range map[string]string{helper: tc.helper, target: tc.target} {
				if err := os.WriteFile(path, []byte("#!/bin/bash\n"+source+"\n"), 0700); err != nil {
					t.Fatal(err)
				}
			}
			cmd := exec.Command("bash", "launcher.sh", "redis")
			cmd.Env = append(os.Environ(), "REDIS_HEALTH_BIN="+helper, "REDIS_STARTUP_SCRIPT="+target, "SIGNAL_FILE="+signals)
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			if tc.signal {
				time.Sleep(100 * time.Millisecond)
				if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
					t.Fatal(err)
				}
			}
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			select {
			case err := <-done:
				code := 0
				if err != nil {
					exit, ok := err.(*exec.ExitError)
					if !ok {
						t.Fatal(err)
					}
					code = exit.ExitCode()
				}
				if code != tc.exit {
					t.Fatalf("exit %d expected %d", code, tc.exit)
				}
			case <-time.After(3 * time.Second):
				_ = cmd.Process.Kill()
				t.Fatal("launcher did not stop children")
			}
			data, err := os.ReadFile(signals)
			if err != nil || len(data) == 0 {
				t.Fatal("child did not receive TERM")
			}
			if tc.signal && (!strings.Contains(string(data), "target") || !strings.Contains(string(data), "helper")) {
				t.Fatal("TERM did not reach both children")
			}
		})
	}
}
