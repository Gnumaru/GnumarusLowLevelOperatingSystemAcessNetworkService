package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

// cappedAcc accumulates at most cap bytes, discarding the rest.
type cappedAcc struct {
	buf       bytes.Buffer
	cap       int
	truncated bool
}

func (c *cappedAcc) Write(p []byte) (int, error) {
	room := c.cap - c.buf.Len()
	orig := len(p)
	if room > 0 {
		if orig > room {
			p = p[:room]
		}
		c.buf.Write(p)
	}
	if orig > max(room, 0) {
		c.truncated = true
	}
	return orig, nil
}

func handleExecShell(w http.ResponseWriter, r *http.Request, opts Opts) {
	if !strings.Contains(r.Header.Get("Content-Type"), "application/json") {
		sendErr(w, 400, "Content-Type must be application/json with body { command, timeout?, cwd?, max_stdout?, max_stderr?, stdin?, stdin_encoding? }.", "")
		return
	}
	body, err := readBody(r)
	if err != nil {
		sendErr(w, 400, "Cannot read body.", "")
		return
	}
	var obj map[string]any
	if len(bytes.TrimSpace(body)) == 0 {
		obj = map[string]any{}
	} else {
		dec := json.NewDecoder(bytes.NewReader(body))
		if err := dec.Decode(&obj); err != nil {
			sendErr(w, 400, "Invalid JSON body.", "")
			return
		}
		if obj == nil {
			sendErr(w, 400, "JSON body must be an object.", "")
			return
		}
	}
	cmdStr, _ := obj["command"].(string)
	if strings.TrimSpace(cmdStr) == "" {
		sendErr(w, 400, "Missing or empty required field: command.", "")
		return
	}
	command := cmdStr

	timeout := shellDefaultTimeout
	if v, ok := obj["timeout"]; ok && v != nil {
		s, isStr := v.(string)
		if !isStr {
			sendErr(w, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).", "")
			return
		}
		t, err := JDec(s)
		if err != nil {
			sendErr(w, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).", "")
			return
		}
		f, ok := toFloat(t)
		if !ok {
			sendErr(w, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).", "")
			return
		}
		timeout = clamp(f, 1, shellMaxTimeout)
	}

	cwd := opts.Cwd
	if v, ok := obj["cwd"]; ok && v != nil {
		s, isStr := v.(string)
		if !isStr {
			sendErr(w, 400, "Field 'cwd' must be a string.", "")
			return
		}
		cwd = resolvePath(s, opts.Cwd)
		if st, err := os.Stat(cwd); err != nil || !st.IsDir() {
			sendErr(w, 400, "Working directory does not exist.", cwd)
			return
		}
	}

	limitOf := func(key string) (int, bool) {
		v, ok := obj[key]
		if !ok || v == nil {
			return shellDefaultMaxOut, true
		}
		s, isStr := v.(string)
		if !isStr {
			sendErr(w, 400, fmt.Sprintf("Field '%s' must be an integer >= 0 (i:<int>).", key), "")
			return 0, false
		}
		t, err := JDec(s)
		if err != nil {
			sendErr(w, 400, fmt.Sprintf("Field '%s' must be an integer >= 0 (i:<int>).", key), "")
			return 0, false
		}
		n, isInt := toInt(t)
		if !isInt || n < 0 {
			sendErr(w, 400, fmt.Sprintf("Field '%s' must be an integer >= 0 (i:<int>).", key), "")
			return 0, false
		}
		if n > shellMaxOutput {
			sendErr(w, 400, fmt.Sprintf("Field '%s' exceeds the %d byte cap.", key, shellMaxOutput), "")
			return 0, false
		}
		return n, true
	}
	maxOut, ok := limitOf("max_stdout")
	if !ok {
		return
	}
	maxErr, ok := limitOf("max_stderr")
	if !ok {
		return
	}

	var stdinRaw string
	if v, ok := obj["stdin"]; ok && v != nil {
		s, isStr := v.(string)
		if !isStr {
			sendErr(w, 400, "Field 'stdin' must be a string.", "")
			return
		}
		stdinRaw = s
	}
	stdinEnc := "text"
	if v, ok := obj["stdin_encoding"]; ok && v != nil {
		s, isStr := v.(string)
		if !isStr {
			sendErr(w, 400, "Field 'stdin_encoding' must be text|base64.", "")
			return
		}
		stdinEnc = strings.ToLower(s)
	}
	var stdinData []byte
	switch stdinEnc {
	case "text":
		stdinData = []byte(stdinRaw)
	case "base64":
		decoded, err := base64.StdEncoding.DecodeString(stdinRaw)
		if err != nil {
			sendErr(w, 400, "Invalid base64 'stdin'.", "")
			return
		}
		stdinData = decoded
	default:
		sendErr(w, 400, "Field 'stdin_encoding' must be text|base64.", "")
		return
	}

	cmd := exec.Command("bash", "-c", command)
	cmd.Dir = cwd
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// bytes.Reader is copied to the child in the background by os/exec
	// and closed at EOF; a non-reading command cannot block the server.
	cmd.Stdin = bytes.NewReader(stdinData)
	outAcc := &cappedAcc{cap: maxOut}
	errAcc := &cappedAcc{cap: maxErr}
	cmd.Stdout = outAcc
	cmd.Stderr = errAcc
	if err := cmd.Start(); err != nil {
		sendErr(w, 400, fmt.Sprintf("Cannot start shell: %v", err), "")
		return
	}
	timedOut := false
	returncode := 0
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			if exitErr, ok := err.(*exec.ExitError); ok {
				returncode = exitErr.ExitCode()
			}
		}
	case <-time.After(time.Duration(timeout * float64(time.Second))):
		timedOut = true
		pgid := cmd.Process.Pid
		_ = syscall.Kill(-pgid, syscall.SIGKILL)
		tryWait(cmd, done)
		returncode = 124
	}
	sendJSON(w, 200, map[string]any{
		"command": command, "cwd": cwd,
		"returncode": mustEnc(int64(returncode)),
		"stdout":         outAcc.buf.String(),
		"stderr":         errAcc.buf.String(),
		"timed_out":      mustEnc(timedOut),
		"stdout_truncated": mustEnc(outAcc.truncated),
		"stderr_truncated": mustEnc(errAcc.truncated),
		"max_stdout":       mustEnc(int64(maxOut)),
		"max_stderr":       mustEnc(int64(maxErr)),
		"stdin_bytes":      mustEnc(int64(len(stdinData))),
	})
}

// tryWait waits for an already-signalled process, bounded.
func tryWait(cmd *exec.Cmd, done <-chan error) {
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		_ = cmd.Process.Kill()
		<-done
	}
}

func toFloat(v any) (float64, bool) {
	switch t := v.(type) {
	case int64:
		return float64(t), true
	case float64:
		return t, true
	default:
		return 0, false
	}
}

func toInt(v any) (int, bool) {
	n, ok := v.(int64)
	if !ok {
		return 0, false
	}
	return int(n), true
}

func clamp(f, lo, hi float64) float64 {
	if f < lo {
		return lo
	}
	if f > hi {
		return hi
	}
	return f
}
