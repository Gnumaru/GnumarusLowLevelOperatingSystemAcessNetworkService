package main

// OAuth 2.0 (Authorization Code + PKCE) helper for the local service.
//
// A browser page cannot complete Google's OAuth flow by itself: the
// consent page sends no CORS headers and the token exchange needs a
// client secret (Desktop-app clients are public, but Google still
// rejects a secret-less exchange). The local service therefore acts
// like a native OAuth client, exactly as a desktop mail app would:
//
//   1. POST /api/oauth2/start    builds the authorization URL, mints
//      state + a PKCE verifier/challenge (S256) and returns the URL.
//   2. The page opens that URL; the user consents at Google.
//   3. Google redirects the browser to the loopback callback on this
//      very service (http://<Host>/api/oauth2/callback?code=&state=).
//   4. The callback correlates by state and exchanges the code for
//      tokens server-side (the secret never reaches the page).
//   5. The page polls /api/oauth2/poll to collect the short-lived
//      access token, which it uses for IMAP AUTHENTICATE XOAUTH2.
//      The refresh token stays in the service (never leaves it).
//
// The authorization/token endpoints are overridable per request so a
// test can point them at a local fake Google; defaults are Google's.

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

const (
	oauthDefaultAuthURL  = "https://accounts.google.com/o/oauth2/v2/auth"
	oauthDefaultTokenURL = "https://oauth2.googleapis.com/token"
	oauthDefaultScope    = "https://mail.google.com/"
	oauthCallbackPath    = "/api/oauth2/callback"
	oauthIdleTimeout     = 600 * time.Second
	oauthMaxSessions     = 64
	oauthExchangeTimeout = 20 * time.Second
)

type oauthSession struct {
	clientID     string
	clientSecret string
	authURL      string
	tokenURL     string
	scope        string
	redirectURI  string
	state        string
	codeVerifier string

	// status is pending | exchanging | authorized | error.
	status       string
	accessToken  string
	refreshToken string
	tokenType    string
	expiresIn    int64
	scopeOut     string
	errText      string

	last time.Time
}

var (
	oauthSessions = map[string]*oauthSession{}
	oauthMu       sync.Mutex
	oauthSweep    sync.Once
)

func oauthSweepExpired() {
	now := time.Now()
	oauthMu.Lock()
	defer oauthMu.Unlock()
	for id, sess := range oauthSessions {
		if now.Sub(sess.last) > oauthIdleTimeout {
			delete(oauthSessions, id)
		}
	}
}

func oauthEnsureSweeper() {
	oauthSweep.Do(func() {
		go func() {
			for {
				time.Sleep(60 * time.Second)
				oauthSweepExpired()
			}
		}()
	})
}

func oauthLookup(w http.ResponseWriter, q map[string][]string) (string, *oauthSession, bool) {
	id := firstQuery(q, "id")
	if id == "" {
		sendErr(w, 400, "Missing required query parameter: id", "")
		return "", nil, false
	}
	oauthMu.Lock()
	sess, ok := oauthSessions[id]
	if ok {
		sess.last = time.Now()
	}
	oauthMu.Unlock()
	if !ok {
		sendErr(w, 404, "Unknown OAuth session: "+id, "")
		return "", nil, false
	}
	return id, sess, true
}

func oauthRandom(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

func oauthStr(obj map[string]any, key string) (string, bool) {
	v, present := obj[key]
	if !present || v == nil {
		return "", true
	}
	s, ok := v.(string)
	if !ok {
		return "", false
	}
	return s, true
}

// oauthValidURL accepts absolute http(s) URLs with a host. It keeps a
// typo ("accounts.google.com", "htp://x") from being appended as a
// broken authorization URL.
func oauthValidURL(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return false
	}
	return u.Host != ""
}

// handleOAuth2Start creates an OAuth session and returns the
// authorization URL to open in a browser.
func handleOAuth2Start(w http.ResponseWriter, r *http.Request) {
	oauthEnsureSweeper()
	if !strings.Contains(r.Header.Get("Content-Type"), "application/json") {
		sendErr(w, 400, "Content-Type must be application/json with body { client_id, client_secret?, auth_url?, token_url?, scope? }.", "")
		return
	}
	obj, ok := sockParseJSON(w, r)
	if !ok {
		return
	}
	clientID, ok := oauthStr(obj, "client_id")
	if !ok {
		sendErr(w, 400, "Field 'client_id' must be a string.", "")
		return
	}
	clientID = strings.TrimSpace(clientID)
	if clientID == "" {
		sendErr(w, 400, "Missing or empty required field: client_id.", "")
		return
	}
	clientSecret, ok := oauthStr(obj, "client_secret")
	if !ok {
		sendErr(w, 400, "Field 'client_secret' must be a string.", "")
		return
	}
	authURL, ok := oauthStr(obj, "auth_url")
	if !ok {
		sendErr(w, 400, "Field 'auth_url' must be a string.", "")
		return
	}
	tokenURL, ok := oauthStr(obj, "token_url")
	if !ok {
		sendErr(w, 400, "Field 'token_url' must be a string.", "")
		return
	}
	scope, ok := oauthStr(obj, "scope")
	if !ok {
		sendErr(w, 400, "Field 'scope' must be a string.", "")
		return
	}
	authURL = strings.TrimSpace(authURL)
	tokenURL = strings.TrimSpace(tokenURL)
	scope = strings.TrimSpace(scope)
	if authURL == "" {
		authURL = oauthDefaultAuthURL
	}
	if tokenURL == "" {
		tokenURL = oauthDefaultTokenURL
	}
	if scope == "" {
		scope = oauthDefaultScope
	}
	if !oauthValidURL(authURL) {
		sendErr(w, 400, "Field 'auth_url' must be an absolute http(s) URL.", "")
		return
	}
	if !oauthValidURL(tokenURL) {
		sendErr(w, 400, "Field 'token_url' must be an absolute http(s) URL.", "")
		return
	}
	// The redirect lands back on this same service (loopback); the
	// Host header was already checked against the loopback allowlist.
	redirectURI := "http://" + r.Host + oauthCallbackPath
	state := oauthRandom(24)
	verifier := oauthRandom(32)
	sum := sha256.Sum256([]byte(verifier))
	challenge := base64.RawURLEncoding.EncodeToString(sum[:])

	vals := url.Values{}
	vals.Set("client_id", clientID)
	vals.Set("redirect_uri", redirectURI)
	vals.Set("response_type", "code")
	vals.Set("scope", scope)
	vals.Set("state", state)
	vals.Set("code_challenge", challenge)
	vals.Set("code_challenge_method", "S256")
	vals.Set("access_type", "offline")
	vals.Set("prompt", "consent")
	sep := "?"
	if strings.Contains(authURL, "?") {
		sep = "&"
	}
	full := authURL + sep + vals.Encode()

	oauthSweepExpired()
	oauthMu.Lock()
	fullSessions := len(oauthSessions) >= oauthMaxSessions
	oauthMu.Unlock()
	if fullSessions {
		sendErr(w, 400, fmt.Sprintf("Too many open OAuth sessions (%d).", oauthMaxSessions), "")
		return
	}
	id := sockMintID()
	sess := &oauthSession{
		clientID: clientID, clientSecret: clientSecret,
		authURL: authURL, tokenURL: tokenURL, scope: scope,
		redirectURI: redirectURI, state: state, codeVerifier: verifier,
		status: "pending", last: time.Now(),
	}
	oauthMu.Lock()
	oauthSessions[id] = sess
	oauthMu.Unlock()
	sendJSON(w, 201, map[string]any{
		"id": id, "auth_url": full, "redirect_uri": redirectURI, "scope": scope,
	})
}

// handleOAuth2Poll reports the session state and, once authorized,
// the short-lived access token the page needs for XOAUTH2. The
// refresh token is deliberately never exposed.
func handleOAuth2Poll(w http.ResponseWriter, q map[string][]string) {
	id, sess, ok := oauthLookup(w, q)
	if !ok {
		return
	}
	oauthMu.Lock()
	out := map[string]any{"id": id, "status": sess.status}
	switch sess.status {
	case "authorized":
		out["access_token"] = sess.accessToken
		out["token_type"] = sess.tokenType
		out["expires_in"] = mustEnc(sess.expiresIn)
		out["scope"] = sess.scopeOut
	case "error":
		out["error"] = sess.errText
	}
	oauthMu.Unlock()
	sendJSON(w, 200, out)
}

// handleOAuth2Refresh mints a fresh access token from the stored
// refresh token (the access token expires after about an hour).
func handleOAuth2Refresh(w http.ResponseWriter, q map[string][]string) {
	id, sess, ok := oauthLookup(w, q)
	if !ok {
		return
	}
	oauthMu.Lock()
	rt := sess.refreshToken
	status := sess.status
	clientID := sess.clientID
	clientSecret := sess.clientSecret
	tokenURL := sess.tokenURL
	oauthMu.Unlock()
	if rt == "" || status == "pending" || status == "exchanging" {
		sendErr(w, 400, "No refresh token for this session; authorize again.", "")
		return
	}
	form := url.Values{}
	form.Set("grant_type", "refresh_token")
	form.Set("refresh_token", rt)
	form.Set("client_id", clientID)
	if clientSecret != "" {
		form.Set("client_secret", clientSecret)
	}
	tok, err := oauthPostForm(tokenURL, form)
	if err != nil {
		sendErr(w, 400, "Token refresh failed: "+err.Error(), "")
		return
	}
	oauthMu.Lock()
	sess.accessToken = tok.access
	sess.tokenType = tok.tokenType
	sess.expiresIn = tok.expires
	if tok.scope != "" {
		sess.scopeOut = tok.scope
	}
	sess.status = "authorized"
	sess.errText = ""
	sess.last = time.Now()
	access := sess.accessToken
	ttype := sess.tokenType
	expires := sess.expiresIn
	scopeOut := sess.scopeOut
	oauthMu.Unlock()
	sendJSON(w, 200, map[string]any{
		"id": id, "access_token": access, "token_type": ttype,
		"expires_in": mustEnc(expires), "scope": scopeOut,
	})
}

// handleOAuth2Callback is the loopback redirect target. It is an open
// route (Google cannot send our API token); the random state is the
// bearer of trust, exactly like the OAuth "state" parameter is meant
// to be. It always answers with a small self-contained HTML page.
func handleOAuth2Callback(w http.ResponseWriter, q map[string][]string) {
	state := firstQuery(q, "state")
	code := firstQuery(q, "code")
	errParam := firstQuery(q, "error")
	errDesc := firstQuery(q, "error_description")

	oauthMu.Lock()
	var sess *oauthSession
	for _, s := range oauthSessions {
		if state != "" && s.state == state {
			sess = s
			break
		}
	}
	if sess != nil {
		sess.last = time.Now()
	}
	oauthMu.Unlock()
	if sess == nil {
		oauthWritePage(w, 400, "Authorization failed",
			"Unknown or expired authorization request. Start the sign-in again from the app.")
		return
	}
	if errParam != "" {
		msg := "The provider reported: " + errParam
		if errDesc != "" {
			msg += " (" + errDesc + ")"
		}
		oauthMu.Lock()
		sess.status = "error"
		sess.errText = msg
		oauthMu.Unlock()
		oauthWritePage(w, 400, "Authorization denied", msg)
		return
	}
	if code == "" {
		oauthMu.Lock()
		sess.status = "error"
		sess.errText = "authorization response without a code"
		oauthMu.Unlock()
		oauthWritePage(w, 400, "Authorization failed", "The provider did not return an authorization code.")
		return
	}
	oauthMu.Lock()
	already := sess.status != "pending"
	if !already {
		sess.status = "exchanging"
	}
	clientID := sess.clientID
	clientSecret := sess.clientSecret
	tokenURL := sess.tokenURL
	redirectURI := sess.redirectURI
	verifier := sess.codeVerifier
	oauthMu.Unlock()
	if already {
		oauthWritePage(w, 200, "Already processed",
			"This authorization request was already handled. You can close this window.")
		return
	}
	form := url.Values{}
	form.Set("grant_type", "authorization_code")
	form.Set("code", code)
	form.Set("client_id", clientID)
	if clientSecret != "" {
		form.Set("client_secret", clientSecret)
	}
	form.Set("redirect_uri", redirectURI)
	form.Set("code_verifier", verifier)
	tok, err := oauthPostForm(tokenURL, form)
	if err != nil {
		oauthMu.Lock()
		sess.status = "error"
		sess.errText = err.Error()
		oauthMu.Unlock()
		oauthWritePage(w, 400, "Token exchange failed", err.Error())
		return
	}
	oauthMu.Lock()
	sess.status = "authorized"
	sess.accessToken = tok.access
	sess.refreshToken = tok.refresh
	sess.tokenType = tok.tokenType
	sess.expiresIn = tok.expires
	if tok.scope != "" {
		sess.scopeOut = tok.scope
	}
	sess.errText = ""
	oauthMu.Unlock()
	oauthWritePage(w, 200, "Authorization complete",
		"You can close this window and return to the app.")
}

func handleOAuth2Close(w http.ResponseWriter, q map[string][]string) {
	id, sess, ok := oauthLookup(w, q)
	if !ok {
		return
	}
	oauthMu.Lock()
	if cur, present := oauthSessions[id]; present && cur == sess {
		delete(oauthSessions, id)
	}
	oauthMu.Unlock()
	sendJSON(w, 200, map[string]any{"id": id, "closed": mustEnc(true)})
}

// ---------------------------------------------------------------------------
// token endpoint client
// ---------------------------------------------------------------------------

type oauthToken struct {
	access    string
	refresh   string
	tokenType string
	expires   int64
	scope     string
}

// oauthPostForm posts an application/x-www-form-urlencoded request to
// an OAuth token endpoint and parses the JSON response.
func oauthPostForm(tokenURL string, form url.Values) (oauthToken, error) {
	req, err := http.NewRequest("POST", tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return oauthToken{}, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	client := &http.Client{Timeout: oauthExchangeTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return oauthToken{}, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	payload := map[string]any{}
	_ = json.Unmarshal(body, &payload)
	get := func(k string) string {
		if s, isStr := payload[k].(string); isStr {
			return s
		}
		return ""
	}
	if resp.StatusCode != 200 {
		msg := get("error_description")
		if msg == "" {
			msg = get("error")
		}
		if msg == "" {
			msg = fmt.Sprintf("HTTP %d", resp.StatusCode)
		}
		return oauthToken{}, fmt.Errorf("%s", msg)
	}
	tok := oauthToken{
		access: get("access_token"), refresh: get("refresh_token"),
		tokenType: get("token_type"), scope: get("scope"),
	}
	if f, isNum := payload["expires_in"].(float64); isNum {
		tok.expires = int64(f)
	}
	if tok.access == "" {
		return oauthToken{}, fmt.Errorf("token response without an access_token")
	}
	if tok.tokenType == "" {
		tok.tokenType = "Bearer"
	}
	if tok.expires == 0 {
		tok.expires = 3600
	}
	return tok, nil
}

// oauthWritePage renders the small self-contained page the browser
// lands on after the provider redirect (no external resources).
func oauthWritePage(w http.ResponseWriter, status int, title, message string) {
	body := "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">" +
		"<title>" + html.EscapeString(title) + "</title></head>\n" +
		"<body style=\"font-family:system-ui,sans-serif;padding:2rem;max-width:32rem\">\n" +
		"<h1>" + html.EscapeString(title) + "</h1>\n" +
		"<p>" + html.EscapeString(message) + "</p>\n" +
		"<script>setTimeout(function(){try{window.close();}catch(e){}},900);</script>\n" +
		"</body></html>\n"
	sendRaw(w, status, []byte(body), "text/html; charset=utf-8")
}
