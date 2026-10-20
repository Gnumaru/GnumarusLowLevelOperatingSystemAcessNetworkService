// Entry point: CLI, config resolution, server boot.
//
// Usage:
//     ./bin/GnumarusOSBridge [--host H] [--port P] [--token TEXT] [--no-auth | --auth]
//
// Settings resolve as: CLI flag > GNUMARUS_* env var > stdin key=value
// lines > cpp/config.cfg (also checked next to the working dir and the
// executable) > default.

#include <iostream>
#include <csignal>
#include <unistd.h>

#include "api.h"
#include "config.h"
#include "routes.h"

namespace {

void usage() {
    std::cerr
        << "Usage: ./bin/GnumarusOSBridge [--host HOST] [--port PORT] [--token TOKEN] [--no-auth | --auth]\n"
        << "\nEvery setting can also come from a GNUMARUS_* environment variable\n"
        << "(GNUMARUS_HOST/PORT/TOKEN/NO_AUTH), from stdin key=value lines, or from\n"
        << "config.cfg (working dir or executable dir).\n"
        << "Precedence (highest first): CLI flag > env var > stdin > config.cfg > default.\n";
}

[[noreturn]] void fail(const std::string& msg) {
    std::cerr << "config error: " << msg << "\n";
    exit(2);
}

}  // namespace

int main(int argc, char** argv) {
    std::map<std::string, std::string> cli;
    bool no_auth_set = false;
    bool no_auth = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto need_value = [&](const std::string& flag) {
            if (i + 1 >= argc) fail(flag + " needs a value");
            return std::string(argv[++i]);
        };
        if (a == "--host")
            cli["host"] = need_value(a);
        else if (a == "--port")
            cli["port"] = need_value(a);
        else if (a == "--token")
            cli["token"] = need_value(a);
        else if (a == "--no-auth") {
            if (no_auth_set && !no_auth)
                fail("--auth and --no-auth are mutually exclusive");
            no_auth_set = true;
            no_auth = true;
        } else if (a == "--auth") {
            if (no_auth_set && no_auth)
                fail("--auth and --no-auth are mutually exclusive");
            no_auth_set = true;
            no_auth = false;
        } else if (a == "-h" || a == "--help") {
            usage();
            return 0;
        } else if (a.rfind("--port=", 0) == 0) {
            cli["port"] = a.substr(7);
        } else if (a.rfind("--host=", 0) == 0) {
            cli["host"] = a.substr(7);
        } else if (a.rfind("--token=", 0) == 0) {
            cli["token"] = a.substr(8);
        } else {
            fail("unknown argument: " + a);
        }
    }
    if (no_auth_set) cli["no_auth"] = no_auth ? "true" : "false";

    char cwd_buf[4096] = {'.', 0};
    if (!getcwd(cwd_buf, sizeof(cwd_buf))) cwd_buf[0] = 0;
    std::string cwd = cwd_buf[0] ? cwd_buf : ".";

    ResolvedConfig cfg;
    try {
        cfg = resolve_config(cli, load_stdin_config(), load_file_config());
    } catch (const ConfigError& e) {
        fail(e.msg);
    }
    std::cout << "config sources: host=" << cfg.src_host
              << " port=" << cfg.src_port << " token=" << cfg.src_token
              << " no_auth=" << cfg.src_no_auth << "\n"
              << std::flush;

    ApiState st;
    st.auth_required = !cfg.no_auth;
    st.auth_token = cfg.token;
    st.cwd = cwd;
    // A command that exits without reading faults our stdin write with
    // EPIPE instead of killing the server (default SIGPIPE would).
    signal(SIGPIPE, SIG_IGN);
    std::cout << "GnumarusNativeOps v" << version_str() << " serving on http://"
              << cfg.host << ":" << cfg.port << " (Ctrl+C to stop)\n";
    if (!cfg.no_auth) {
        std::cout << "API token: " << cfg.token << "\n";
        std::cout << "Send it as 'Authorization: Bearer <token>' (or ?token=). "
                     "/api/help stays open.\n";
    } else {
        std::cout << "WARNING: authentication DISABLED (no_auth from "
                  << cfg.src_no_auth
                  << "). Anyone able to reach this server has full access, "
                     "including shell execution.\n";
    }
    std::cout << std::flush;
    try {
        http_serve(cfg.host, cfg.port,
                   [&](const HttpRequest& req) { return api_handle(req, st); },
                   [&](int fd, const HttpRequest& req) {
                       if (!sockws::ws_is_upgrade_request(req)) return false;
                       sockws::handle_socket_ws_upgrade(fd, req, st);
                       return true;
                   });
    } catch (const std::exception& e) {
        std::cerr << "cannot listen: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
