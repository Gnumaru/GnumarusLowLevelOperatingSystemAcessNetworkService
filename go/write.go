package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

func contentType(r *http.Request) string {
	return r.Header.Get("Content-Type")
}

// parseWriteBody returns file content from a POST/PUT body: raw bytes
// for non-JSON content types, else { content, encoding } (text|base64).
func parseWriteBody(r *http.Request) ([]byte, string, bool) {
	body, err := readBody(r)
	if err != nil {
		return nil, "Cannot read body.", false
	}
	if !strings.Contains(contentType(r), "application/json") {
		return body, "", true
	}
	var obj map[string]any
	if len(bytes.TrimSpace(body)) == 0 {
		obj = map[string]any{}
	} else {
		dec := json.NewDecoder(bytes.NewReader(body))
		if err := dec.Decode(&obj); err != nil {
			return nil, "Invalid JSON body.", false
		}
		if obj == nil {
			return nil, "JSON body must be an object.", false
		}
	}
	content, hasContent := obj["content"]
	encoding := "text"
	if rawEnc, ok := obj["encoding"]; ok && rawEnc != nil {
		encStr, ok := rawEnc.(string)
		if !ok {
			return nil, "Invalid encoding: (use text|base64).", false
		}
		encoding = strings.ToLower(encStr)
	}
	if encoding == "text" {
		if !hasContent || content == nil {
			return []byte{}, "", true
		}
		s, ok := content.(string)
		if !ok {
			return nil, "'content' must be a string for text encoding.", false
		}
		return []byte(s), "", true
	}
	if encoding == "base64" {
		if !hasContent || content == nil {
			return []byte{}, "", true
		}
		s, ok := content.(string)
		if !ok {
			return nil, "'content' must be a base64 string.", false
		}
		data, err := base64.StdEncoding.DecodeString(s)
		if err != nil {
			return nil, "Invalid base64 'content'.", false
		}
		return data, "", true
	}
	return nil, fmt.Sprintf("Invalid encoding: '%s' (use text|base64).", encoding), false
}

func handleCreateFile(w http.ResponseWriter, r *http.Request, q map[string][]string, opts Opts) {
	raw := firstQuery(q, "path")
	if raw == "" {
		sendErr(w, 400, "Missing required query parameter: path", "")
		return
	}
	abs := resolvePath(raw, opts.Cwd)
	if lexists(abs) {
		sendErr(w, 409, "Path already exists (use PUT to overwrite).", abs)
		return
	}
	parent := filepath.Dir(abs)
	if st, err := os.Stat(parent); err != nil || !st.IsDir() {
		sendErr(w, 404, "Parent directory does not exist.", abs)
		return
	}
	data, errmsg, ok := parseWriteBody(r)
	if !ok {
		sendErr(w, 400, errmsg, "")
		return
	}
	fh, err := os.OpenFile(abs, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0666)
	if err != nil {
		if lexists(abs) {
			sendErr(w, 409, "Path already exists (use PUT to overwrite).", abs)
			return
		}
		if os.IsNotExist(err) {
			sendErr(w, 404, "Parent directory does not exist.", abs)
			return
		}
		if os.IsPermission(err) {
			sendErr(w, 403, "Permission denied", abs)
			return
		}
		sendErr(w, 400, fmt.Sprintf("Cannot create file: %v", err), abs)
		return
	}
	_, werr := fh.Write(data)
	cerr := fh.Close()
	if werr != nil || cerr != nil {
		sendErr(w, 400, "Cannot create file", abs)
		return
	}
	sendJSON(w, 201, map[string]any{
		"path": raw, "absolute_path": abs,
		"size": mustEnc(int64(len(data))), "mime_type": guessMime(abs),
	})
}

func handleOverwriteFile(w http.ResponseWriter, r *http.Request, q map[string][]string, opts Opts) {
	raw := firstQuery(q, "path")
	if raw == "" {
		sendErr(w, 400, "Missing required query parameter: path", "")
		return
	}
	abs := resolvePath(raw, opts.Cwd)
	if !lexists(abs) {
		sendErr(w, 404, "Path does not exist", abs)
		return
	}
	if st, err := os.Stat(abs); err == nil {
		if li, err2 := os.Lstat(abs); err2 == nil {
			if st.IsDir() && li.Mode()&os.ModeSymlink == 0 {
				sendErr(w, 400, "Path is a directory, not a file", abs)
				return
			}
		} else if st.IsDir() {
			sendErr(w, 400, "Path is a directory, not a file", abs)
			return
		}
	} else {
		sendErr(w, 404, "Path does not exist", abs)
		return
	}
	data, errmsg, ok := parseWriteBody(r)
	if !ok {
		sendErr(w, 400, errmsg, "")
		return
	}
	if err := os.WriteFile(abs, data, 0666); err != nil {
		if os.IsPermission(err) {
			sendErr(w, 403, "Permission denied", abs)
			return
		}
		if os.IsNotExist(err) {
			sendErr(w, 404, "Path does not exist", abs)
			return
		}
		sendErr(w, 400, fmt.Sprintf("Cannot write file: %v", err), abs)
		return
	}
	sendJSON(w, 200, map[string]any{
		"path": raw, "absolute_path": abs,
		"size": mustEnc(int64(len(data))), "mime_type": guessMime(abs),
	})
}

func handleDeleteFile(w http.ResponseWriter, q map[string][]string, opts Opts) {
	raw := firstQuery(q, "path")
	if raw == "" {
		sendErr(w, 400, "Missing required query parameter: path", "")
		return
	}
	abs := resolvePath(raw, opts.Cwd)
	if !lexists(abs) {
		sendErr(w, 404, "Path does not exist", abs)
		return
	}
	if st, err := os.Stat(abs); err == nil {
		if li, err2 := os.Lstat(abs); err2 == nil {
			if st.IsDir() && li.Mode()&os.ModeSymlink == 0 {
				sendErr(w, 400, "Path is a directory, not a file (use DELETE /api/dir).", abs)
				return
			}
		} else if st.IsDir() {
			sendErr(w, 400, "Path is a directory, not a file (use DELETE /api/dir).", abs)
			return
		}
	} else {
		sendErr(w, 404, "Path does not exist", abs)
		return
	}
	if err := os.Remove(abs); err != nil {
		if os.IsNotExist(err) {
			sendErr(w, 404, "Path does not exist", abs)
			return
		}
		if os.IsPermission(err) {
			sendErr(w, 403, "Permission denied", abs)
			return
		}
		sendErr(w, 400, fmt.Sprintf("Cannot delete file: %v", err), abs)
		return
	}
	sendJSON(w, 200, map[string]any{
		"path": raw, "absolute_path": abs, "deleted": mustEnc(true),
	})
}

func handleDeleteDir(w http.ResponseWriter, q map[string][]string, opts Opts) {
	raw := firstQuery(q, "path")
	if raw == "" {
		sendErr(w, 400, "Missing required query parameter: path", "")
		return
	}
	abs := resolvePath(raw, opts.Cwd)
	if !lexists(abs) {
		sendErr(w, 404, "Path does not exist", abs)
		return
	}
	if li, err := os.Lstat(abs); err != nil || li.Mode()&os.ModeSymlink != 0 {
		sendErr(w, 400, "Path is not a directory", abs)
		return
	} else if st, err := os.Stat(abs); err != nil || !st.IsDir() {
		sendErr(w, 400, "Path is not a directory", abs)
		return
	}
	if err := os.Remove(abs); err != nil {
		if os.IsNotExist(err) {
			sendErr(w, 404, "Path does not exist", abs)
			return
		}
		if os.IsPermission(err) {
			sendErr(w, 403, "Permission denied", abs)
			return
		}
		if isNotEmpty(abs) {
			sendErr(w, 400, fmt.Sprintf("Cannot delete directory: Directory not empty: '%s'", abs), abs)
			return
		}
		sendErr(w, 400, fmt.Sprintf("Cannot delete directory: %v", err), abs)
		return
	}
	sendJSON(w, 200, map[string]any{
		"path": raw, "absolute_path": abs, "deleted": mustEnc(true),
	})
}

func isNotEmpty(dir string) bool {
	fh, err := os.Open(dir)
	if err != nil {
		return false
	}
	defer fh.Close()
	names, err := fh.Readdirnames(1)
	return err == nil && len(names) > 0
}
