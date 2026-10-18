#pragma once

#include <map>
#include <string>

// Multi-source configuration: CLI flag > env var > stdin > config.cfg > default.

struct ConfigError {
    std::string msg;
};

struct ResolvedConfig {
    std::string host = "127.0.0.1";
    int port = 1423;
    std::string token;      // empty when no_auth
    bool no_auth = false;
    bool token_generated = false;
    std::string src_host = "default";
    std::string src_port = "default";
    std::string src_token = "default";
    std::string src_no_auth = "default";
};

// Parse key=value lines (binary/UTF-8 guarded by callers via raw bytes).
std::map<std::string, std::string> parse_kv(const std::string& data);

// Candidate config.cfg locations: working dir, exe dir.
std::map<std::string, std::string> load_file_config();
std::map<std::string, std::string> load_stdin_config();

// Throws ConfigError on invalid values.
ResolvedConfig resolve_config(const std::map<std::string, std::string>& cli,
                              const std::map<std::string, std::string>& stdin_cfg,
                              const std::map<std::string, std::string>& file_cfg);
