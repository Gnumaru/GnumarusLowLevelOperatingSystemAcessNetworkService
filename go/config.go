package main

import (
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Multi-source configuration: CLI flag > env var > stdin > config.cfg > default.

var configKeys = []string{"host", "port", "token", "no_auth"}

const configFileName = "config.cfg"

// bindAllowed reports whether an address may be bound: loopback only
// (127.0.0.0/8, ::1, with optional brackets) or "localhost". Anything
// else (0.0.0.0, LAN IPs, public hostnames) is refused so the service
// can never listen off-machine.
func bindAllowed(host string) bool {
	h := strings.TrimSpace(host)
	if strings.HasPrefix(h, "[") && strings.HasSuffix(h, "]") && len(h) > 2 {
		h = h[1 : len(h)-1]
	}
	if strings.EqualFold(h, "localhost") {
		return true
	}
	return quadLoopback(h)
}
const configMaxBytes = 1024 * 1024
const configEnvPrefix = "GNUMARUS_"

type ConfigError struct{ Msg string }

func (e *ConfigError) Error() string { return e.Msg }

type ResolvedConfig struct {
	Host    string
	Port    int
	Token   string
	NoAuth  bool
	Sources map[string]string
}

func parseKv(data []byte) map[string]string {
	cfg := map[string]string{}
	for _, b := range data {
		if b == 0 {
			return map[string]string{}
		}
	}
	text := string(data)
	if !utf8.Valid(data) {
		return map[string]string{}
	}
	for _, rawLine := range strings.Split(text, "\n") {
		line := strings.TrimSpace(rawLine)
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";") {
			continue
		}
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			continue
		}
		eq := strings.Index(line, "=")
		if eq < 0 {
			continue
		}
		key := strings.ToLower(strings.TrimSpace(line[:eq]))
		value := strings.TrimSpace(line[eq+1:])
		if len(value) >= 2 && ((value[0] == '\'' && value[len(value)-1] == '\'') ||
			(value[0] == '"' && value[len(value)-1] == '"')) {
			value = value[1 : len(value)-1]
		}
		if slices.Contains(configKeys, key) {
			cfg[key] = value
		}
	}
	return cfg
}

// Candidate config.cfg locations: working dir, exe dir.
func configCandidates() []string {
	out := []string{filepath.Join(mustCwd(), configFileName)}
	if exe, err := os.Executable(); err == nil {
		out = append(out, filepath.Join(filepath.Dir(exe), configFileName))
	}
	return out
}

func mustCwd() string {
	cwd, err := os.Getwd()
	if err != nil {
		return "."
	}
	return cwd
}

func loadFileConfig() map[string]any {
	out := map[string]any{}
	for _, cand := range configCandidates() {
		fh, err := os.Open(cand)
		if err != nil {
			continue
		}
		buf := make([]byte, configMaxBytes)
		n, _ := io.ReadFull(fh, buf)
		fh.Close()
		for k, v := range parseKv(buf[:n]) {
			out[k] = v
		}
		return out
	}
	return out
}

func loadStdinConfig() map[string]any {
	out := map[string]any{}
	// Never block: a terminal (or nothing piped) means absent.
	fi, err := os.Stdin.Stat()
	if err != nil {
		return out
	}
	if fi.Mode()&os.ModeCharDevice != 0 {
		return out
	}
	buf := make([]byte, configMaxBytes)
	total := 0
	for total < len(buf) {
		n, err := os.Stdin.Read(buf[total:])
		total += n
		if err != nil || n == 0 {
			break
		}
	}
	data := buf[:total]
	if len(data) == 0 {
		return out
	}
	for k, v := range parseKv(data) {
		out[k] = v
	}
	return out
}

func parseBool(value any, source string) (bool, error) {
	if b, ok := value.(bool); ok {
		return b, nil
	}
	t := strings.ToLower(strings.TrimSpace(fmt.Sprint(value)))
	switch t {
	case "1", "true", "yes", "on":
		return true, nil
	case "0", "false", "no", "off":
		return false, nil
	}
	return false, &ConfigError{fmt.Sprintf("invalid boolean for no_auth from %s: '%v' (use true/false)", source, value)}
}

func parsePort(value any, source string) (int, error) {
	switch v := value.(type) {
	case bool:
		return 0, &ConfigError{fmt.Sprintf("invalid port from %s: '%v' (1-65535)", source, value)}
	case int:
		return checkPort(int64(v), source, value)
	case int64:
		return checkPort(v, source, value)
	case string:
		s := strings.TrimSpace(v)
		neg := false
		if strings.HasPrefix(s, "+") {
			s = s[1:]
		} else if strings.HasPrefix(s, "-") {
			neg = true
			s = s[1:]
		}
		if s == "" {
			return 0, &ConfigError{fmt.Sprintf("invalid port from %s: '%v' (1-65535)", source, value)}
		}
		for _, c := range []byte(s) {
			if c < '0' || c > '9' {
				return 0, &ConfigError{fmt.Sprintf("invalid port from %s: '%v' (1-65535)", source, value)}
			}
		}
		n, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			return 0, &ConfigError{fmt.Sprintf("invalid port from %s: '%v' (1-65535)", source, value)}
		}
		if neg {
			n = -n
		}
		return checkPort(n, source, value)
	default:
		return 0, &ConfigError{fmt.Sprintf("invalid port from %s: '%v' (1-65535)", source, value)}
	}
}

func checkPort(n int64, source string, value any) (int, error) {
	if n < 1 || n > 65535 {
		return 0, &ConfigError{fmt.Sprintf("port from %s out of range: %d (1-65535)", source, n)}
	}
	return int(n), nil
}

func resolveConfig(cli, stdinCfg, fileCfg map[string]any) (ResolvedConfig, error) {
	envCfg := map[string]any{}
	for _, key := range configKeys {
		if v, ok := os.LookupEnv(configEnvPrefix + strings.ToUpper(key)); ok && v != "" {
			envCfg[key] = v
		}
	}
	layers := []struct {
		source string
		layer  map[string]any
	}{
		{"cli", cli}, {"env", envCfg}, {"stdin", stdinCfg}, {"file", fileCfg},
	}
	raw := map[string]any{}
	sources := map[string]string{}
	for _, key := range configKeys {
		sources[key] = "default"
		for _, l := range layers {
			if v, ok := l.layer[key]; ok && v != nil {
				raw[key] = v
				sources[key] = l.source
				break
			}
		}
	}
	host := "127.0.0.1"
	if v, ok := raw["host"]; ok {
		host = strings.TrimSpace(fmt.Sprint(v))
	}
	if host == "" {
		return ResolvedConfig{}, &ConfigError{fmt.Sprintf("host from %s must not be empty", sources["host"])}
	}
	if !bindAllowed(host) {
		return ResolvedConfig{}, &ConfigError{fmt.Sprintf("host from %s must be a loopback address (127.0.0.1, ::1 or localhost), got '%s'", sources["host"], host)}
	}
	port := 1423
	if v, ok := raw["port"]; ok {
		p, err := parsePort(v, sources["port"])
		if err != nil {
			return ResolvedConfig{}, err
		}
		port = p
	}
	noAuth := false
	if v, ok := raw["no_auth"]; ok {
		b, err := parseBool(v, sources["no_auth"])
		if err != nil {
			return ResolvedConfig{}, err
		}
		noAuth = b
	}
	var token string
	if noAuth {
		sources["token"] = "disabled"
	} else if v, ok := raw["token"]; ok {
		token = fmt.Sprint(v)
		if token == "" {
			return ResolvedConfig{}, &ConfigError{fmt.Sprintf("token from %s must not be empty", sources["token"])}
		}
	} else {
		rb := make([]byte, 32)
		if _, err := rand.Read(rb); err != nil {
			return ResolvedConfig{}, err
		}
		token = base64.RawURLEncoding.EncodeToString(rb)
		sources["token"] = "generated"
	}
	return ResolvedConfig{host, port, token, noAuth, sources}, nil
}
