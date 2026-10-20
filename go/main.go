package main

import (
	"fmt"
	"net/http"
	"os"
	"strings"
)

// Gnumarus native-operations HTTP service (Go port).
//
// Usage:
//     ./GnumarusOSBridge [--host H] [--port P] [--token TEXT] [--no-auth | --auth]
//
// Settings resolve as: CLI flag > GNUMARUS_* env var > stdin key=value
// lines > go/config.cfg (also checked next to the working dir and the
// executable) > default.

func usage() {
	fmt.Fprintln(os.Stderr,
		"Usage: ./GnumarusOSBridge [--host HOST] [--port PORT] [--token TOKEN] [--no-auth | --auth]\n"+
			"\nEvery setting can also come from a GNUMARUS_* environment variable\n"+
			"(GNUMARUS_HOST/PORT/TOKEN/NO_AUTH), from stdin key=value lines, or from\n"+
			"go/config.cfg. Precedence (highest first): CLI flag > env var >\n"+
			"stdin > config.cfg > default.")
}

func fail(msg string) {
	fmt.Fprintln(os.Stderr, "config error: "+msg)
	os.Exit(2)
}

func main() {
	cli := map[string]any{}
	args := os.Args[1:]
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case a == "--host":
			if i+1 >= len(args) {
				fail("--host needs a value")
			}
			i++
			cli["host"] = args[i]
		case a == "--port":
			if i+1 >= len(args) {
				fail("--port needs a value")
			}
			i++
			cli["port"] = args[i]
		case a == "--token":
			if i+1 >= len(args) {
				fail("--token needs a value")
			}
			i++
			cli["token"] = args[i]
		case a == "--no-auth":
			if v, ok := cli["no_auth"]; ok && v == false {
				fail("--auth and --no-auth are mutually exclusive")
			}
			cli["no_auth"] = true
		case a == "--auth":
			if v, ok := cli["no_auth"]; ok && v == true {
				fail("--auth and --no-auth are mutually exclusive")
			}
			cli["no_auth"] = false
		case a == "-h" || a == "--help":
			usage()
			return
		case strings.HasPrefix(a, "--port="):
			cli["port"] = a[7:]
		case strings.HasPrefix(a, "--host="):
			cli["host"] = a[7:]
		case strings.HasPrefix(a, "--token="):
			cli["token"] = a[8:]
		default:
			fail("unknown argument: " + a)
		}
	}

	cwd, err := os.Getwd()
	if err != nil {
		cwd = "."
	}
	cfg, err := resolveConfig(cli, loadStdinConfig(), loadFileConfig())
	if err != nil {
		fail(err.Error())
	}
	s := cfg.Sources
	fmt.Printf("config sources: host=%s port=%s token=%s no_auth=%s\n",
		s["host"], s["port"], s["token"], s["no_auth"])

	opts := Opts{AuthRequired: !cfg.NoAuth, AuthToken: cfg.Token, Cwd: cwd}
	addr := fmt.Sprintf("%s:%d", cfg.Host, cfg.Port)
	server := &http.Server{Addr: addr, Handler: Handler(opts)}
	fmt.Printf("GnumarusNativeOps v%s serving on http://%s (Ctrl+C to stop)\n", version, addr)
	if !cfg.NoAuth {
		fmt.Printf("API token: %s\n", cfg.Token)
		fmt.Println("Send it as 'Authorization: Bearer <token>' (or ?token=). /api/help stays open.")
	} else {
		fmt.Printf("WARNING: authentication DISABLED (no_auth from %s). Anyone able to reach this server has full access, including shell execution.\n", s["no_auth"])
	}
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "cannot listen: "+err.Error())
		os.Exit(1)
	}
}
