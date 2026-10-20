package main

import (
	"net/http"
	"os"
	"runtime"
	"strings"
)

func readOsRelease() map[string]string {
	info := map[string]string{}
	data, err := os.ReadFile("/etc/os-release")
	if err != nil {
		return info
	}
	for _, line := range strings.Split(string(data), "\n") {
		t := strings.TrimSpace(line)
		if t == "" || strings.HasPrefix(t, "#") || !strings.Contains(t, "=") {
			continue
		}
		eq := strings.Index(t, "=")
		info[t[:eq]] = strings.Trim(strings.TrimSpace(t[eq+1:]), "\"'")
	}
	return info
}

func handleSysinfo(w http.ResponseWriter) {
	platform := runtime.GOOS
	var system string
	switch platform {
	case "windows":
		system = "Windows"
	case "darwin":
		system = "Darwin"
	default:
		system = "Linux"
	}
	var distro any = "null"
	var windows any = "null"
	var mac any = "null"
	if system == "Linux" {
		if rel := readOsRelease(); len(rel) > 0 {
			distro = map[string]any{
				"id":          rel["ID"],
				"name":        rel["NAME"],
				"version":     rel["VERSION"],
				"version_id":  rel["VERSION_ID"],
				"pretty_name": rel["PRETTY_NAME"],
			}
		}
	} else if system == "Windows" {
		windows = map[string]any{
			"release": platform, "version": "",
			"service_pack": "", "edition": "",
		}
	} else if system == "Darwin" {
		mac = map[string]any{"version": ""}
	}
	hostname, _ := os.Hostname()
	cpu := runtime.NumCPU()
	if cpu < 1 {
		cpu = 1
	}
	systemName := system
	if system == "Darwin" {
		systemName = "macOS"
	}
	kernelRelease, kernelVersion := kernelInfo()
	// NOTE: no runtime-version field here. Managed runtimes report the
	// version of the runtime that executes them (python_version,
	// node_version, php_version, ruby_version, jvm_version,
	// dotnet_version); native ones (Go, Rust, C++) do not report one.
	sendJSON(w, 200, map[string]any{
		"system":      system,
		"system_name": systemName,
		"release":     kernelRelease,
		"version":     kernelVersion,
		"machine":     runtime.GOARCH,
		"processor":   "",
		"hostname":    hostname,
		"cpu_count":   mustEnc(int64(cpu)),
		"distro":      distro,
		"windows":     windows,
		"mac":         mac,
	})
}

// kernelInfo returns (release, version), best effort per OS.
func kernelInfo() (string, string) {
	if runtime.GOOS == "linux" {
		release := ""
		version := ""
		if data, err := os.ReadFile("/proc/sys/kernel/osrelease"); err == nil {
			release = strings.TrimSpace(string(data))
		}
		if data, err := os.ReadFile("/proc/sys/kernel/version"); err == nil {
			version = strings.TrimSpace(string(data))
		}
		return release, version
	}
	return "", ""
}
