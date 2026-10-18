// Shell execution handler (included by api.cpp; uses its includes
// and shared helpers).
namespace {

// cappedAcc accumulates at most cap bytes, discarding the rest.
struct CappedAcc {
    std::string buf;
    size_t cap;
    bool truncated = false;
    void push(const char* data, size_t len) {
        size_t room = cap > buf.size() ? cap - buf.size() : 0;
        if (room > 0) buf.append(data, std::min(len, room));
        if (len > room) truncated = true;
    }
};

struct DrainState {
    CappedAcc acc;
    std::atomic<bool> done{false};
};

void drain_fd(int fd, DrainState& st) {
    char tmp[65536];
    while (true) {
        ssize_t n = read(fd, tmp, sizeof(tmp));
        if (n <= 0) break;
        st.acc.push(tmp, (size_t)n);
    }
    close(fd);
    st.done.store(true);
}

void feed_fd(int fd, const std::string& data, std::atomic<bool>& done) {
    size_t off = 0;
    while (off < data.size()) {
        ssize_t n = write(fd, data.data() + off, data.size() - off);
        if (n <= 0) break;  // EOF/EPIPE: child gone or not reading
        off += (size_t)n;
    }
    close(fd);
    done.store(true);
}

// Wait until pred() or 5s; join finished threads, detach the rest
// (a setsid-escaped grandchild can hold a pipe open; bounded leak,
// same as the other ports' daemon/detached readers).
template <typename Pred>
void wait_flag(Pred pred) {
    auto end = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!pred()) {
        if (std::chrono::steady_clock::now() >= end) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}

HttpResponse handle_exec_shell(const HttpRequest& req, const ApiState& st) {
    auto ctit = req.headers.find("content-type");
    std::string ct = ctit == req.headers.end() ? "" : ctit->second;
    std::string lower = ct;
    for (auto& c : lower) c = std::tolower((unsigned char)c);
    if (lower.find("application/json") == std::string::npos)
        return send_json(
            400, jerr(400, "Content-Type must be application/json with body "
                           "{ command, timeout?, cwd?, max_stdout?, max_stderr?, "
                           "stdin?, stdin_encoding? }."));
    json obj;
    try {
        obj = req.body.empty() ? json::object() : json::parse(req.body);
    } catch (...) {
        return send_json(400, jerr(400, "Invalid JSON body."));
    }
    if (!obj.is_object())
        return send_json(400, jerr(400, "JSON body must be an object."));
    auto itc = obj.find("command");
    if (itc == obj.end() || !itc->is_string() ||
        itc->get<std::string>().find_first_not_of(" \t\n\r") ==
            std::string::npos)
        return send_json(
            400, jerr(400, "Missing or empty required field: command."));
    std::string command = itc->get<std::string>();

    auto tagged_number = [&](const char* key, double dflt, double& out) {
        auto it = obj.find(key);
        if (it == obj.end() || it->is_null()) {
            out = dflt;
            return true;
        }
        if (!it->is_string()) return false;
        try {
            Tagged t = jdec(it->get<std::string>());
            if (auto* n = std::get_if<int64_t>(&t))
                out = (double)*n;
            else if (auto* f = std::get_if<double>(&t))
                out = *f;
            else
                return false;
            return true;
        } catch (const BadTag&) {
            return false;
        }
    };
    double timeout = 30;
    if (!tagged_number("timeout", 30, timeout))
        return send_json(400, jerr(400, "Field 'timeout' must be a tagged "
                                        "number (i:<int> or f:<float>)."));
    timeout = std::clamp(timeout, 1.0, 300.0);

    std::string cwd = st.cwd;
    {
        auto it = obj.find("cwd");
        if (it != obj.end() && !it->is_null()) {
            if (!it->is_string())
                return send_json(400, jerr(400, "Field 'cwd' must be a string."));
            cwd = resolve_path(it->get<std::string>(), st.cwd);
            struct stat std;
            if (stat(cwd.c_str(), &std) != 0 || !S_ISDIR(std.st_mode))
                return send_json(
                    400, jerr(400, "Working directory does not exist.", cwd));
        }
    }
    auto tagged_limit = [&](const char* key, int64_t& out) {
        auto it = obj.find(key);
        if (it == obj.end() || it->is_null()) {
            out = 1024 * 1024;
            return true;
        }
        if (!it->is_string()) return false;
        try {
            Tagged t = jdec(it->get<std::string>());
            auto* n = std::get_if<int64_t>(&t);
            if (!n || *n < 0 || *n > 100LL * 1024 * 1024) return false;
            out = *n;
            return true;
        } catch (const BadTag&) {
            return false;
        }
    };
    int64_t max_out = 0, max_err = 0;
    if (!tagged_limit("max_stdout", max_out))
        return send_json(400, jerr(400, "Field 'max_stdout' must be an integer "
                                        ">= 0 (i:<int>)."));
    if (max_out > 100LL * 1024 * 1024)
        return send_json(400, jerr(400, "Field 'max_stdout' exceeds the "
                                        "104857600 byte cap."));
    if (!tagged_limit("max_stderr", max_err))
        return send_json(400, jerr(400, "Field 'max_stderr' must be an integer "
                                        ">= 0 (i:<int>)."));
    if (max_err > 100LL * 1024 * 1024)
        return send_json(400, jerr(400, "Field 'max_stderr' exceeds the "
                                        "104857600 byte cap."));

    std::string stdin_raw;
    {
        auto it = obj.find("stdin");
        if (it != obj.end() && !it->is_null()) {
            if (!it->is_string())
                return send_json(400, jerr(400, "Field 'stdin' must be a string."));
            stdin_raw = it->get<std::string>();
        }
    }
    std::string stdin_enc = "text";
    {
        auto it = obj.find("stdin_encoding");
        if (it != obj.end() && !it->is_null()) {
            if (!it->is_string())
                return send_json(
                    400, jerr(400, "Field 'stdin_encoding' must be text|base64."));
            stdin_enc = it->get<std::string>();
            for (auto& c : stdin_enc) c = std::tolower((unsigned char)c);
        }
    }
    std::string stdin_data;
    if (stdin_enc == "text") {
        stdin_data = stdin_raw;
    } else if (stdin_enc == "base64") {
        if (!b64_decode(stdin_raw, stdin_data))
            return send_json(400, jerr(400, "Invalid base64 'stdin'."));
    } else {
        return send_json(400, jerr(400, "Field 'stdin_encoding' must be "
                                        "text|base64."));
    }

    int out_pipe[2], err_pipe[2], in_pipe[2];
    if (pipe(out_pipe) != 0 || pipe(err_pipe) != 0 || pipe(in_pipe) != 0)
        return send_json(400, jerr(400, "Cannot start shell: pipe failed"));
    pid_t pid = fork();
    if (pid < 0) {
        for (int fd : {out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1],
                       in_pipe[0], in_pipe[1]})
            close(fd);
        return send_json(400, jerr(400, "Cannot start shell: fork failed"));
    }
    if (pid == 0) {
        // Child: new session (killpg parity), redirect, exec bash.
        setsid();
        dup2(in_pipe[0], STDIN_FILENO);
        dup2(out_pipe[1], STDOUT_FILENO);
        dup2(err_pipe[1], STDERR_FILENO);
        for (int fd : {out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1],
                       in_pipe[0], in_pipe[1]})
            close(fd);
        if (chdir(cwd.c_str()) != 0) _exit(127);
        execl("/bin/bash", "bash", "-c", command.c_str(), nullptr);
        _exit(127);
    }
    close(out_pipe[1]);
    close(err_pipe[1]);
    close(in_pipe[0]);
    DrainState out_st;
    out_st.acc.cap = (size_t)max_out;
    DrainState err_st;
    err_st.acc.cap = (size_t)max_err;
    std::atomic<bool> feed_done{false};
    std::thread out_th(drain_fd, out_pipe[0], std::ref(out_st));
    std::thread err_th(drain_fd, err_pipe[0], std::ref(err_st));
    std::thread in_th(feed_fd, in_pipe[1], std::cref(stdin_data),
                      std::ref(feed_done));

    bool timed_out = false;
    int returncode = 0;
    {
        auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::duration<double>(std::max(0.0, timeout));
        int status = 0;
        while (true) {
            pid_t r = waitpid(pid, &status, WNOHANG);
            if (r == pid) {
                if (WIFEXITED(status))
                    returncode = WEXITSTATUS(status);
                else if (WIFSIGNALED(status))
                    returncode = 128 + WTERMSIG(status);
                break;
            }
            if (r < 0) break;  // ECHILD etc.
            if (std::chrono::steady_clock::now() >= deadline) {
                timed_out = true;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    }
    if (timed_out) {
        kill(-pid, SIGKILL);  // whole process group (setsid parity)
        auto end = std::chrono::steady_clock::now() + std::chrono::seconds(5);
        int status = 0;
        while (std::chrono::steady_clock::now() < end) {
            pid_t r = waitpid(pid, &status, WNOHANG);
            if (r != 0) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        returncode = 124;
    }
    wait_flag([&] { return out_st.done.load(); });
    wait_flag([&] { return err_st.done.load(); });
    wait_flag([&] { return feed_done.load(); });
    auto join_or_detach = [](std::thread& th, bool done) {
        if (done) {
            if (th.joinable()) th.join();
        } else if (th.joinable()) {
            th.detach();  // bounded leak, same as other ports' daemons
        }
    };
    join_or_detach(out_th, out_st.done.load());
    join_or_detach(err_th, err_st.done.load());
    join_or_detach(in_th, feed_done.load());
    json o;
    o["command"] = command;
    o["cwd"] = cwd;
    o["returncode"] = jenc_int(returncode);
    o["stdout"] = utf8_sanitize(out_st.acc.buf);
    o["stderr"] = utf8_sanitize(err_st.acc.buf);
    o["timed_out"] = jenc_bool(timed_out);
    o["stdout_truncated"] = jenc_bool(out_st.acc.truncated);
    o["stderr_truncated"] = jenc_bool(err_st.acc.truncated);
    o["max_stdout"] = jenc_int(max_out);
    o["max_stderr"] = jenc_int(max_err);
    o["stdin_bytes"] = jenc_int((int64_t)stdin_data.size());
    return send_json(200, o);
}

}  // namespace
