package main

import (
	"bufio"
	"context"
	"crypto/tls"
	_ "embed"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

//go:embed launcher.sh
var launcher []byte

type config struct {
	target, address, listen, username, password, waiting, seed, master string
	tls                                                                *tls.Config
	interval, timeout, freshness, heartbeat                            time.Duration
	minReplicas                                                        int
	quorum                                                             bool
}

func environment(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func loadConfig(target string) (config, error) {
	c := config{target: target, username: environment("REDIS_USERNAME", "default"), interval: 5 * time.Second,
		timeout: 2 * time.Second, freshness: 10 * time.Second, heartbeat: 60 * time.Second,
		waiting: "/run/redis-config/" + target + ".waiting", seed: "/data/.recovery-needs-seed",
		master: environment("REDIS_SHUTDOWN_MASTER_SET", "redis"), quorum: environment("REDIS_HEALTH_REQUIRE_QUORUM", "yes") == "yes"}
	port, healthPort := "6379", "9122"
	if target == "sentinel" {
		port, healthPort = "26379", "9123"
	}
	if target != "redis" && target != "sentinel" {
		return c, errors.New("invalid health target")
	}
	c.address = environment("REDIS_HEALTH_ADDRESS", "127.0.0.1:"+port)
	c.listen = ":" + environment("REDIS_HEALTH_PORT", healthPort)
	var err error
	c.password, err = credential()
	if err != nil {
		return c, err
	}
	c.minReplicas, err = strconv.Atoi(environment("REDIS_HEALTH_MIN_REPLICAS", "0"))
	if err != nil || c.minReplicas < 0 {
		return c, errors.New("invalid health replica minimum")
	}
	if environment("REDIS_TLS_ENABLED", "no") == "yes" {
		c.tls, err = transportTLS(os.Getenv("REDIS_TLS_CA_FILE"), os.Getenv("REDIS_TLS_SERVER_NAME"), os.Getenv("REDIS_TLS_CERT_FILE"), os.Getenv("REDIS_TLS_KEY_FILE"))
		if err != nil {
			return c, err
		}
	}
	return c, nil
}

type redisError string

func (e redisError) Error() string { return string(e) }

// RESP is deliberately restricted to bounded RESP2 replies for our fixed commands.
func response(reader *bufio.Reader) (interface{}, error) {
	budget := 1048576
	return readResponse(reader, 0, &budget)
}
func readResponse(reader *bufio.Reader, depth int, budget *int) (interface{}, error) {
	if depth > 4 {
		return nil, errors.New("RESP nesting exceeded")
	}
	rawLine, err := reader.ReadSlice('\n')
	if err != nil {
		return nil, err
	}
	line := string(rawLine)
	*budget -= len(line)
	if *budget < 0 {
		return nil, errors.New("RESP reply budget exceeded")
	}
	if len(line) > 65536 || len(line) < 3 || !strings.HasSuffix(line, "\r\n") {
		return nil, errors.New("invalid RESP line")
	}
	body := line[1 : len(line)-2]
	switch line[0] {
	case '+':
		return body, nil
	case '-':
		return nil, redisError(body)
	case ':':
		return strconv.ParseInt(body, 10, 64)
	case '$':
		n, err := strconv.Atoi(body)
		if err == nil && n == -1 {
			return nil, nil
		}
		if err != nil || n < 0 || n > 1048576 {
			return nil, errors.New("invalid RESP bulk length")
		}
		*budget -= n + 2
		if *budget < 0 {
			return nil, errors.New("RESP reply budget exceeded")
		}
		data := make([]byte, n+2)
		if _, err = io.ReadFull(reader, data); err != nil {
			return nil, err
		}
		if string(data[n:]) != "\r\n" {
			return nil, errors.New("invalid RESP bulk terminator")
		}
		return string(data[:n]), nil
	case '*':
		n, err := strconv.Atoi(body)
		if err == nil && n == -1 {
			return nil, nil
		}
		if err != nil || n < 0 || n > 256 {
			return nil, errors.New("invalid RESP array length")
		}
		values := make([]interface{}, n)
		for i := range values {
			values[i], err = readResponse(reader, depth+1, budget)
			if err != nil {
				return nil, err
			}
		}
		return values, nil
	default:
		return nil, errors.New("unsupported RESP reply")
	}
}

type client struct {
	cfg        config
	connection net.Conn
	reader     *bufio.Reader
}

func (c *client) close() {
	if c.connection != nil {
		_ = c.connection.Close()
		c.connection = nil
		c.reader = nil
	}
}
func (c *client) connect(ctx context.Context) error {
	dialer := &net.Dialer{Timeout: c.cfg.timeout}
	var conn net.Conn
	var err error
	if c.cfg.tls != nil {
		conn, err = (&tls.Dialer{NetDialer: dialer, Config: c.cfg.tls}).DialContext(ctx, "tcp", c.cfg.address)
	} else {
		conn, err = dialer.DialContext(ctx, "tcp", c.cfg.address)
	}
	if err != nil {
		return err
	}
	c.connection = conn
	c.reader = bufio.NewReaderSize(conn, 8192)
	args := []string{"AUTH", c.cfg.username, c.cfg.password}
	if c.cfg.username == "" {
		args = []string{"AUTH", c.cfg.password}
	}
	_, err = c.command(ctx, args...)
	if err != nil {
		c.close()
	}
	return err
}
func (c *client) command(ctx context.Context, args ...string) (interface{}, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if c.connection == nil {
		if err := c.connect(ctx); err != nil {
			return nil, err
		}
	}
	deadline := time.Now().Add(c.cfg.timeout)
	if bound, ok := ctx.Deadline(); ok && bound.Before(deadline) {
		deadline = bound
	}
	if err := c.connection.SetDeadline(deadline); err != nil {
		c.close()
		return nil, err
	}
	var wire strings.Builder
	fmt.Fprintf(&wire, "*%d\r\n", len(args))
	for _, arg := range args {
		fmt.Fprintf(&wire, "$%d\r\n%s\r\n", len(arg), arg)
	}
	if _, err := io.WriteString(c.connection, wire.String()); err != nil {
		c.close()
		return nil, err
	}
	result, err := response(c.reader)
	if err != nil {
		var protocol redisError
		if !errors.As(err, &protocol) {
			c.close()
		}
	}
	return result, err
}

type status struct {
	at          time.Time
	live, ready bool
	category    string
}
type checker struct {
	cfg              config
	redis            *client
	mu               sync.RWMutex
	status           status
	logf             func(string, ...interface{})
	lastLog          time.Time
	lastLogSignature string
}

func fields(info string) map[string]string {
	result := map[string]string{}
	for _, line := range strings.Split(info, "\r\n") {
		key, value, ok := strings.Cut(line, ":")
		if ok {
			result[key] = value
		}
	}
	return result
}
func (h *checker) poll(ctx context.Context) {
	next := status{at: time.Now(), category: "connection"}
	defer func() {
		if h.redis.connection == nil {
			next.live = false
			next.ready = false
		}
		h.mu.Lock()
		h.status = next
		h.mu.Unlock()
		h.report(next, time.Now())
	}()
	pong, err := h.redis.command(ctx, "PING")
	if err != nil {
		next.category = failureCategory(err)
		return
	}
	if pong != "PONG" {
		next.category = "protocol"
		return
	}
	next.live = true
	if h.cfg.target == "sentinel" {
		if h.cfg.quorum {
			_, err = h.redis.command(ctx, "SENTINEL", "CKQUORUM", h.cfg.master)
		}
		next.ready = err == nil
		if err == nil {
			next.category = "ready"
		} else {
			next.category = failureCategory(err)
			var command redisError
			if errors.As(err, &command) && next.category == "connection" {
				next.category = "quorum"
			}
		}
		return
	}
	value, err := h.redis.command(ctx, "INFO", "replication")
	if err != nil {
		next.category = failureCategory(err)
		return
	}
	info, ok := value.(string)
	if !ok {
		next.category = "protocol"
		return
	}
	data := fields(info)
	ready := false
	if data["role"] == "master" {
		replicas, err := strconv.Atoi(data["connected_slaves"])
		ready = err == nil && replicas >= h.cfg.minReplicas
	} else {
		ready = data["role"] == "slave" && data["master_link_status"] == "up" && data["master_sync_in_progress"] == "0"
	}
	if !ready {
		next.category = "replication"
		return
	}
	priority, err := os.ReadFile(h.cfg.seed)
	next.category = "seed"
	if err != nil && !os.IsNotExist(err) {
		return
	}
	if err == nil {
		if data["role"] != "slave" {
			return
		}
		parsed, err := strconv.Atoi(strings.TrimSpace(string(priority)))
		if err != nil || parsed < 0 {
			return
		}
		reply, err := h.redis.command(ctx, "CONFIG", "SET", "replica-priority", strconv.Itoa(parsed))
		if err != nil || reply != "OK" {
			return
		}
		if err = os.Remove(h.cfg.seed); err != nil {
			return
		}
	}
	next.ready = true
	next.category = "ready"
}
func (h *checker) healthy(path string, now time.Time) bool {
	h.mu.RLock()
	current := h.status
	h.mu.RUnlock()
	fresh := !current.at.IsZero() && now.Sub(current.at) >= 0 && now.Sub(current.at) <= h.cfg.freshness
	if path == "/ready" {
		return fresh && current.ready
	}
	if path != "/live" && path != "/startup" {
		return false
	}
	if h.waiting(now) {
		return true
	}
	return fresh && current.live
}
func (h *checker) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	if request.URL.Path != "/ready" && request.URL.Path != "/live" && request.URL.Path != "/startup" {
		http.NotFound(writer, request)
		return
	}
	if !h.healthy(request.URL.Path, time.Now()) {
		writer.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(writer, "unhealthy\n")
		return
	}
	_, _ = io.WriteString(writer, "healthy\n")
}
func install(destination string) error {
	if err := os.MkdirAll(destination, 0755); err != nil {
		return err
	}
	self, err := os.Executable()
	if err != nil {
		return err
	}
	binary, err := os.ReadFile(self)
	if err != nil {
		return err
	}
	if err = installFile(filepath.Join(destination, "redis-health"), binary); err != nil {
		return err
	}
	return installFile(filepath.Join(destination, "launcher.sh"), launcher)
}
func installFile(path string, data []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".health-install-")
	if err != nil {
		return err
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if _, err = file.Write(data); err != nil {
		_ = file.Close()
		return err
	}
	if err = file.Chmod(0555); err != nil {
		_ = file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}
func main() {
	if len(os.Args) > 1 && os.Args[1] == "cli" {
		if err := runCLI(os.Args[2:], os.Stdout); err != nil {
			log.Fatal(err)
		}
		return
	}
	if len(os.Args) == 3 && os.Args[1] == "install" {
		if err := install(os.Args[2]); err != nil {
			log.Fatal("cannot install health binaries")
		}
		return
	}
	target := flag.String("target", "", "redis or sentinel")
	flag.Parse()
	cfg, err := loadConfig(*target)
	if err != nil {
		log.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	h := &checker{cfg: cfg, redis: &client{cfg: cfg}, logf: log.Printf}
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer h.redis.close()
		for {
			h.poll(ctx)
			select {
			case <-ctx.Done():
				return
			case <-time.After(cfg.interval):
			}
		}
	}()
	server := &http.Server{Addr: cfg.listen, Handler: h, ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second, IdleTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	if err = server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		stop()
		log.Fatal("health HTTP listener failed")
	}
	stop()
	<-done
}
