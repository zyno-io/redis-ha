package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

func credential() (string, error) {
	value := os.Getenv("REDISCLI_AUTH")
	if value == "" {
		data, err := os.ReadFile(os.Getenv("REDIS_PASSWORD_FILE"))
		if err != nil {
			return "", errors.New("cannot read Redis credential file")
		}
		value = strings.TrimSuffix(string(data), "\n")
	}
	if value == "" || strings.ContainsAny(value, "\r\n\x00") {
		return "", errors.New("invalid Redis credential")
	}
	return value, nil
}
func transportTLS(caFile, serverName, certFile, keyFile string) (*tls.Config, error) {
	ca, err := os.ReadFile(caFile)
	if err != nil {
		return nil, errors.New("cannot read Redis CA")
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, errors.New("invalid Redis CA")
	}
	if serverName == "" {
		return nil, errors.New("Redis TLS server name is required")
	}
	cfg := &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool, ServerName: serverName}
	if certFile != "" || keyFile != "" {
		certificate, err := tls.LoadX509KeyPair(certFile, keyFile)
		if err != nil {
			return nil, errors.New("cannot load Redis client identity")
		}
		cfg.Certificates = []tls.Certificate{certificate}
	}
	return cfg, nil
}
func cliConfig(args []string) (config, []string, error) {
	cfg := config{username: environment("REDIS_USERNAME", "default"), timeout: 2 * time.Second}
	host, port := "127.0.0.1", "6379"
	secure := environment("REDIS_TLS_ENABLED", "no") == "yes"
	ca, name := os.Getenv("REDIS_TLS_CA_FILE"), os.Getenv("REDIS_TLS_SERVER_NAME")
	cert, key := os.Getenv("REDIS_TLS_CERT_FILE"), os.Getenv("REDIS_TLS_KEY_FILE")
	var err error
	for len(args) > 0 && strings.HasPrefix(args[0], "-") {
		option := args[0]
		args = args[1:]
		if option == "--" {
			break
		}
		if option == "--raw" || option == "-e" || option == "--no-auth-warning" {
			continue
		}
		if option == "--tls" {
			secure = true
			continue
		}
		if len(args) == 0 {
			return cfg, nil, errors.New("missing Redis CLI option value")
		}
		value := args[0]
		args = args[1:]
		switch option {
		case "-h", "--host":
			host = value
		case "-p", "--port":
			port = value
		case "--user", "--username":
			cfg.username = value
		case "--cacert":
			ca = value
		case "--sni":
			name = value
		case "--cert":
			cert = value
		case "--key":
			key = value
		case "-t":
			seconds, err := strconv.ParseFloat(value, 64)
			if err != nil || seconds <= 0 || seconds > 60 {
				return cfg, nil, errors.New("invalid Redis CLI timeout")
			}
			cfg.timeout = time.Duration(seconds * float64(time.Second))
		default:
			return cfg, nil, errors.New("unsupported Redis CLI option")
		}
	}
	if len(args) == 0 {
		return cfg, nil, errors.New("Redis command is required")
	}
	parsedPort, err := strconv.Atoi(port)
	if err != nil || parsedPort < 1 || parsedPort > 65535 {
		return cfg, nil, errors.New("invalid Redis port")
	}
	cfg.address = net.JoinHostPort(strings.Trim(host, "[]"), port)
	cfg.password, err = credential()
	if err != nil {
		return cfg, nil, err
	}
	if secure {
		cfg.tls, err = transportTLS(ca, name, cert, key)
		if err != nil {
			return cfg, nil, err
		}
	}
	return cfg, args, nil
}
func writeRaw(writer io.Writer, value interface{}) error {
	switch typed := value.(type) {
	case nil:
		_, err := io.WriteString(writer, "\n")
		return err
	case string:
		_, err := fmt.Fprintln(writer, typed)
		return err
	case int64:
		_, err := fmt.Fprintln(writer, typed)
		return err
	case []interface{}:
		for _, item := range typed {
			if err := writeRaw(writer, item); err != nil {
				return err
			}
		}
		return nil
	default:
		return errors.New("unsupported Redis output")
	}
}
func executeCLI(cfg config, args []string, writer io.Writer) error {
	ctx, cancel := context.WithTimeout(context.Background(), cfg.timeout)
	defer cancel()
	redis := &client{cfg: cfg}
	defer redis.close()
	result, err := redis.command(ctx, args...)
	if err != nil {
		return errors.New("Redis command failed")
	}
	return writeRaw(writer, result)
}
func runCLI(args []string, writer io.Writer) error {
	cfg, command, err := cliConfig(args)
	if err != nil {
		return err
	}
	return executeCLI(cfg, command, writer)
}
