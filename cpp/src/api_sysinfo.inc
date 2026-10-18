// System information endpoint (included by api.cpp).
namespace {

std::map<std::string, std::string> read_os_release() {
    std::map<std::string, std::string> info;
    FILE* fh = fopen("/etc/os-release", "r");
    if (!fh) return info;
    char* line = nullptr;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, fh)) >= 0) {
        std::string t(line, (size_t)n);
        while (!t.empty() && (t.back() == '\n' || t.back() == '\r'))
            t.pop_back();
        size_t s = t.find_first_not_of(" \t");
        if (s == std::string::npos) continue;
        t = t.substr(s);
        if (t.empty() || t[0] == '#' || t.find('=') == std::string::npos)
            continue;
        size_t eq = t.find('=');
        std::string v = t.substr(eq + 1);
        // trim + strip one layer of quotes
        size_t a = v.find_first_not_of(" \t");
        size_t b = v.find_last_not_of(" \t");
        v = a == std::string::npos ? "" : v.substr(a, b - a + 1);
        if (v.size() >= 2 &&
            ((v.front() == '"' && v.back() == '"') ||
             (v.front() == '\'' && v.back() == '\'')))
            v = v.substr(1, v.size() - 2);
        info[t.substr(0, eq)] = v;
    }
    free(line);
    fclose(fh);
    return info;
}

HttpResponse handle_sysinfo() {
#if defined(_WIN32)
    const std::string system = "Windows";
#elif defined(__APPLE__)
    const std::string system = "Darwin";
#else
    const std::string system = "Linux";
#endif
    json distro = "null", windows = "null", mac = "null";
    if (system == "Linux") {
        auto rel = read_os_release();
        if (!rel.empty()) {
            auto get = [&](const char* k) {
                auto it = rel.find(k);
                return it == rel.end() ? "" : it->second;
            };
            json o;
            o["id"] = get("ID");
            o["name"] = get("NAME");
            o["version"] = get("VERSION");
            o["version_id"] = get("VERSION_ID");
            o["pretty_name"] = get("PRETTY_NAME");
            distro = o;
        }
    } else if (system == "Windows") {
        json o;
        o["release"] = system;
        o["version"] = "";
        o["service_pack"] = "";
        o["edition"] = "";
        windows = o;
    } else if (system == "Darwin") {
        json o;
        o["version"] = "";
        mac = o;
    }
    std::string release, version;
    {
        FILE* fh = fopen("/proc/sys/kernel/osrelease", "r");
        if (fh) {
            char buf[256] = {0};
            if (fgets(buf, sizeof(buf), fh)) release = buf;
            fclose(fh);
        }
    }
    {
        FILE* fh = fopen("/proc/sys/kernel/version", "r");
        if (fh) {
            char buf[1024] = {0};
            if (fgets(buf, sizeof(buf), fh)) version = buf;
            fclose(fh);
        }
    }
    auto trim_nl = [](std::string s) {
        while (!s.empty() && (s.back() == '\n' || s.back() == '\r'))
            s.pop_back();
        return s;
    };
    release = trim_nl(release);
    version = trim_nl(version);
    char hostname[256] = {0};
    gethostname(hostname, sizeof(hostname) - 1);
    std::string machine;
    {
        struct utsname un;
        if (uname(&un) == 0)
            machine = un.machine;
        else
            machine = "";
    }
    long cpu = sysconf(_SC_NPROCESSORS_ONLN);
    if (cpu < 1) cpu = 1;
    json o;
    o["system"] = system;
    o["system_name"] = system == "Darwin" ? "macOS" : system;
    o["release"] = release;
    o["version"] = version;
    o["machine"] = machine;
    o["processor"] = "";
    o["hostname"] = hostname;
    o["cpp_version"] = __VERSION__;
    o["cpu_count"] = jenc_int(cpu);
    o["distro"] = distro;
    o["windows"] = windows;
    o["mac"] = mac;
    return send_json(200, o);
}

}  // namespace
