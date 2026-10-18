require "net/http"
require "cgi"
require "uri"
require "json"
require "securerandom"
require "digest"
require "base64"
require "thread"

module Gnumarus
  module OAuth2
    DEFAULT_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
    DEFAULT_SCOPE = "https://mail.google.com/"
    CALLBACK_PATH = "/api/oauth2/callback"
    IDLE_TIMEOUT = 600
    MAX_SESSIONS = 64

    @@sessions = {}
    @@mutex = Mutex.new

    def self.valid_url?(raw)
      u = URI.parse(raw) rescue nil
      return false unless u && u.scheme && u.host
      u.scheme == "http" || u.scheme == "https"
    rescue
      false
    end

    def self.post_form(token_url, form)
      uri = URI.parse(token_url)
      http = Net::HTTP.new(uri.host, uri.port)
      http.use_ssl = (uri.scheme == "https")
      http.open_timeout = 20
      http.read_timeout = 20
      req = Net::HTTP::Post.new(uri.request_uri)
      req["Content-Type"] = "application/x-www-form-urlencoded"
      req["Accept"] = "application/json"
      req.body = URI.encode_www_form(form)
      resp = http.request(req)
      payload = begin
        JSON.parse(resp.body)
      rescue
        {}
      end
      get = ->(k) { v = payload[k]; v.is_a?(String) ? v : "" }
      unless resp.code.to_i == 200
        msg = get.call("error_description")
        msg = get.call("error") if msg.empty?
        msg = "HTTP #{resp.code}" if msg.empty?
        return [nil, msg]
      end
      tok = {
        "access" => get.call("access_token"),
        "refresh" => get.call("refresh_token"),
        "token_type" => get.call("token_type"),
        "scope" => get.call("scope"),
        "expires" => payload["expires_in"].is_a?(Numeric) ? payload["expires_in"].to_i : 0,
      }
      if tok["access"].empty?
        return [nil, "token response without an access_token"]
      end
      tok["token_type"] = "Bearer" if tok["token_type"].empty?
      tok["expires"] = 3600 if tok["expires"] == 0
      [tok, nil]
    rescue => e
      [nil, e.message]
    end

    def self.write_page(conn, status, title, message)
      body = "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">" +
        "<title>#{CGI.escapeHTML(title)}</title></head>\n<body style=\"font-family:system-ui,sans-serif;padding:2rem;max-width:32rem\">\n" +
        "<h1>#{CGI.escapeHTML(title)}</h1>\n<p>#{CGI.escapeHTML(message)}</p>\n" +
        "<script>setTimeout(function(){try{window.close();}catch(e){}},900);</script>\n</body></html>\n"
      Http.send_raw(conn, status, body, "text/html; charset=utf-8")
    end

    def self.start(conn, req, opts)
      ctype = req.header("content-type") || ""
      unless ctype.include?("application/json")
        Http.send_err(conn, 400, "Content-Type must be application/json with body { client_id, client_secret?, auth_url?, token_url?, scope? }.")
        return
      end
      obj = begin
        JSON.parse(req.body.empty? ? "{}" : req.body)
      rescue
        nil
      end
      unless obj.is_a?(Hash)
        Http.send_err(conn, 400, "JSON body must be an object.")
        return
      end
      get_str = ->(key) {
        return ["", true] unless obj.key?(key) && !obj[key].nil?
        return ["", false] unless obj[key].is_a?(String)
        [obj[key], true]
      }
      client_id, ok = get_str.call("client_id")
      unless ok
        Http.send_err(conn, 400, "Field 'client_id' must be a string.")
        return
      end
      client_id = client_id.strip
      if client_id.empty?
        Http.send_err(conn, 400, "Missing or empty required field: client_id.")
        return
      end
      client_secret, ok = get_str.call("client_secret")
      unless ok
        Http.send_err(conn, 400, "Field 'client_secret' must be a string.")
        return
      end
      auth_url, ok = get_str.call("auth_url")
      unless ok
        Http.send_err(conn, 400, "Field 'auth_url' must be a string.")
        return
      end
      token_url, ok = get_str.call("token_url")
      unless ok
        Http.send_err(conn, 400, "Field 'token_url' must be a string.")
        return
      end
      scope, ok = get_str.call("scope")
      unless ok
        Http.send_err(conn, 400, "Field 'scope' must be a string.")
        return
      end
      auth_url = auth_url.strip
      token_url = token_url.strip
      scope = scope.strip
      auth_url = DEFAULT_AUTH_URL if auth_url.empty?
      token_url = DEFAULT_TOKEN_URL if token_url.empty?
      scope = DEFAULT_SCOPE if scope.empty?
      unless valid_url?(auth_url)
        Http.send_err(conn, 400, "Field 'auth_url' must be an absolute http(s) URL.")
        return
      end
      unless valid_url?(token_url)
        Http.send_err(conn, 400, "Field 'token_url' must be an absolute http(s) URL.")
        return
      end
      host = req.header("host") || ""
      redirect_uri = "http://#{host}#{CALLBACK_PATH}"
      state = SecureRandom.urlsafe_base64(24, padding: false)
      verifier = SecureRandom.urlsafe_base64(32, padding: false)
      challenge = Base64.urlsafe_encode64(Digest::SHA256.digest(verifier), padding: false)
      params = {
        "client_id" => client_id,
        "redirect_uri" => redirect_uri,
        "response_type" => "code",
        "scope" => scope,
        "state" => state,
        "code_challenge" => challenge,
        "code_challenge_method" => "S256",
        "access_type" => "offline",
        "prompt" => "consent",
      }
      sep = auth_url.include?("?") ? "&" : "?"
      full = auth_url + sep + URI.encode_www_form(params)
      @@mutex.synchronize do
        if @@sessions.size >= MAX_SESSIONS
          Http.send_err(conn, 400, "Too many open OAuth sessions (#{MAX_SESSIONS}).")
          return
        end
        id = SecureRandom.urlsafe_base64(16, padding: false)
        @@sessions[id] = {
          "client_id" => client_id,
          "client_secret" => client_secret,
          "auth_url" => auth_url,
          "token_url" => token_url,
          "scope" => scope,
          "redirect_uri" => redirect_uri,
          "state" => state,
          "code_verifier" => verifier,
          "status" => "pending",
          "access_token" => "",
          "refresh_token" => "",
          "token_type" => "",
          "expires_in" => 0,
          "scope_out" => "",
          "err_text" => "",
          "last" => Time.now.to_f,
        }
        Http.send_json(conn, 201, {
          "id" => id, "auth_url" => full, "redirect_uri" => redirect_uri, "scope" => scope,
        })
      end
    end

    def self.poll(conn, req, opts)
      id = Util.first_query(req.query, "id")
      if id.empty?
        Http.send_err(conn, 400, "Missing required query parameter: id")
        return
      end
      @@mutex.synchronize do
        sess = @@sessions[id]
        unless sess
          Http.send_err(conn, 404, "Unknown OAuth session: #{id}")
          return
        end
        sess["last"] = Time.now.to_f
        out = { "id" => id, "status" => sess["status"] }
        if sess["status"] == "authorized"
          out["access_token"] = sess["access_token"]
          out["token_type"] = sess["token_type"]
          out["expires_in"] = Tags.jenc(sess["expires_in"])
          out["scope"] = sess["scope_out"]
        elsif sess["status"] == "error"
          out["error"] = sess["err_text"]
        end
        Http.send_json(conn, 200, out)
      end
    end

    def self.refresh(conn, req, opts)
      id = Util.first_query(req.query, "id")
      if id.empty?
        Http.send_err(conn, 400, "Missing required query parameter: id")
        return
      end
      rt = ""
      status = ""
      client_id = ""
      client_secret = ""
      token_url = ""
      @@mutex.synchronize do
        sess = @@sessions[id]
        unless sess
          Http.send_err(conn, 404, "Unknown OAuth session: #{id}")
          return
        end
        sess["last"] = Time.now.to_f
        rt = sess["refresh_token"]
        status = sess["status"]
        client_id = sess["client_id"]
        client_secret = sess["client_secret"]
        token_url = sess["token_url"]
        if rt.empty? || status == "pending" || status == "exchanging"
          Http.send_err(conn, 400, "No refresh token for this session; authorize again.")
          return
        end
      end
      form = {
        "grant_type" => "refresh_token",
        "refresh_token" => rt,
        "client_id" => client_id,
      }
      form["client_secret"] = client_secret unless client_secret.empty?
      tok, err = post_form(token_url, form)
      if err
        Http.send_err(conn, 400, "Token refresh failed: #{err}")
        return
      end
      @@mutex.synchronize do
        sess = @@sessions[id]
        sess["access_token"] = tok["access"]
        sess["token_type"] = tok["token_type"]
        sess["expires_in"] = tok["expires"]
        sess["scope_out"] = tok["scope"] unless tok["scope"].empty?
        sess["status"] = "authorized"
        sess["err_text"] = ""
        sess["last"] = Time.now.to_f
        Http.send_json(conn, 200, {
          "id" => id,
          "access_token" => sess["access_token"],
          "token_type" => sess["token_type"],
          "expires_in" => Tags.jenc(sess["expires_in"]),
          "scope" => sess["scope_out"],
        })
      end
    end

    def self.close(conn, req, opts)
      id = Util.first_query(req.query, "id")
      if id.empty?
        Http.send_err(conn, 400, "Missing required query parameter: id")
        return
      end
      @@mutex.synchronize do
        sess = @@sessions[id]
        unless sess
          Http.send_err(conn, 404, "Unknown OAuth session: #{id}")
          return
        end
        @@sessions.delete(id)
      end
      Http.send_json(conn, 200, { "id" => id, "closed" => "b:true" })
    end

    def self.callback(conn, req, opts)
      query = req.query
      state = Util.first_query(query, "state")
      code = Util.first_query(query, "code")
      err_param = Util.first_query(query, "error")
      err_desc = Util.first_query(query, "error_description")

      sess = nil
      sid = nil
      @@mutex.synchronize do
        @@sessions.each do |k, s|
          if !state.empty? && s["state"] == state
            sess = s
            sid = k
            break
          end
        end
        sess["last"] = Time.now.to_f if sess
      end
      if sess.nil?
        write_page(conn, 400, "Authorization failed",
          "Unknown or expired authorization request. Start the sign-in again from the app.")
        return
      end
      if !err_param.empty?
        msg = "The provider reported: #{err_param}"
        msg += " (#{err_desc})" unless err_desc.empty?
        @@mutex.synchronize do
          @@sessions[sid]["status"] = "error"
          @@sessions[sid]["err_text"] = msg
        end
        write_page(conn, 400, "Authorization denied", msg)
        return
      end
      if code.empty?
        @@mutex.synchronize do
          @@sessions[sid]["status"] = "error"
          @@sessions[sid]["err_text"] = "authorization response without a code"
        end
        write_page(conn, 400, "Authorization failed", "The provider did not return an authorization code.")
        return
      end
      already = false
      client_id = ""
      client_secret = ""
      token_url = ""
      redirect_uri = ""
      verifier = ""
      @@mutex.synchronize do
        already = @@sessions[sid]["status"] != "pending"
        unless already
          @@sessions[sid]["status"] = "exchanging"
        end
        client_id = @@sessions[sid]["client_id"]
        client_secret = @@sessions[sid]["client_secret"]
        token_url = @@sessions[sid]["token_url"]
        redirect_uri = @@sessions[sid]["redirect_uri"]
        verifier = @@sessions[sid]["code_verifier"]
      end
      if already
        write_page(conn, 200, "Already processed", "This authorization request was already handled. You can close this window.")
        return
      end
      form = {
        "grant_type" => "authorization_code",
        "code" => code,
        "client_id" => client_id,
        "redirect_uri" => redirect_uri,
        "code_verifier" => verifier,
      }
      form["client_secret"] = client_secret unless client_secret.empty?
      tok, err = post_form(token_url, form)
      if err
        @@mutex.synchronize do
          @@sessions[sid]["status"] = "error"
          @@sessions[sid]["err_text"] = err
        end
        write_page(conn, 400, "Token exchange failed", err)
        return
      end
      @@mutex.synchronize do
        @@sessions[sid]["status"] = "authorized"
        @@sessions[sid]["access_token"] = tok["access"]
        @@sessions[sid]["refresh_token"] = tok["refresh"]
        @@sessions[sid]["token_type"] = tok["token_type"]
        @@sessions[sid]["expires_in"] = tok["expires"]
        @@sessions[sid]["scope_out"] = tok["scope"] unless tok["scope"].empty?
        @@sessions[sid]["err_text"] = ""
      end
      write_page(conn, 200, "Authorization complete", "You can close this window and return to the app.")
    end
  end
end
