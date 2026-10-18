// Filesystem endpoint handlers (included by api.cpp; uses its
// includes and shared helpers).
namespace {

HttpResponse handle_read_dir(const HttpRequest& req, const ApiState& st) {
    bool missing = false;
    std::string raw = req_path(req, missing);
    if (missing)
        return send_json(400, jerr(400, "Missing required query parameter: path"));
    std::string abs = resolve_path(raw, st.cwd);
    if (!lexists(abs))
        return send_json(404, jerr(404, "Path does not exist", abs));
    if (!is_dir_follow(abs))
        return send_json(400, jerr(400, "Path is not a directory", abs));
    std::vector<std::string> names;
    bool list_ok = false;
    // opendir/readdir to distinguish permission errors from empty.
    {
        DIR* dir = opendir(abs.c_str());
        if (!dir) {
            std::string msg = (errno == EACCES) ? "Permission denied"
                                                : "Cannot list directory";
            int code = (errno == EACCES) ? 403 : 403;
            (void)code;
            return send_json(403, jerr(403, msg, abs));
        }
        struct dirent* ent;
        while ((ent = readdir(dir)) != nullptr) {
            std::string name(ent->d_name);
            if (name != "." && name != "..") names.push_back(name);
        }
        closedir(dir);
        list_ok = true;
    }
    (void)list_ok;
    std::sort(names.begin(), names.end());
    json entries = json::array();
    for (auto& name : names) {
        std::string full = abs + "/" + name;
        struct stat lst;
        if (lstat(full.c_str(), &lst) != 0) continue;
        json e;
        e["name"] = name;
        e["path"] = full;
        e["type"] = entry_type(full);
        e["size"] = jenc_int((int64_t)lst.st_size);
        e["atime"] = jenc_double(file_atime(full));
        e["mtime"] = jenc_double(file_mtime(full));
        e["ctime"] = jenc_double(file_ctime(full));
        auto birth = file_birth(full);
        e["crtime"] = birth ? jenc_double(*birth) : "null";
        e["mode"] = mode_string(full);
        entries.push_back(e);
    }
    json obj;
    obj["path"] = raw;
    obj["absolute_path"] = abs;
    obj["count"] = jenc_int((int64_t)entries.size());
    obj["entries"] = entries;
    return send_json(200, obj);
}

HttpResponse handle_read_file(const HttpRequest& req, const ApiState& st) {
    bool missing = false;
    std::string raw = req_path(req, missing);
    if (missing)
        return send_json(400, jerr(400, "Missing required query parameter: path"));
    std::string abs = resolve_path(raw, st.cwd);
    if (!lexists(abs))
        return send_json(404, jerr(404, "Path does not exist", abs));
    if (is_dir_follow(abs) && !is_symlink(abs))
        return send_json(400, jerr(400, "Path is a directory, not a file", abs));
    std::string encoding = "auto";
    auto eit = req.query.find("encoding");
    if (eit != req.query.end()) {
        encoding = eit->second;
        for (auto& c : encoding) c = std::tolower((unsigned char)c);
    }
    if (encoding != "auto" && encoding != "text" && encoding != "base64")
        return send_json(400, jerr(400,
                                   "Invalid encoding param: '" + encoding +
                                       "' (use auto|text|base64)"));
    bool raw_mode = false;
    auto rit = req.query.find("raw");
    if (rit != req.query.end() && rit->second == "1") raw_mode = true;
    std::string err_kind;
    std::string data = read_file_bytes(abs, err_kind);
    if (!err_kind.empty()) {
        if (err_kind == "missing")
            return send_json(404, jerr(404, "Path does not exist", abs));
        return send_json(403, jerr(403, "Permission denied", abs));
    }
    // Distinguish unreadable-directory: read_file_bytes on a dir fails;
    // re-check to emit the precise 400 the other ports emit.
    if (is_dir_follow(abs) && !is_symlink(abs))
        return send_json(400, jerr(400, "Path is a directory, not a file", abs));
    std::string mtime = "null";
    {
        struct stat lst;
        if (lstat(abs.c_str(), &lst) == 0) {
#if defined(__APPLE__)
            mtime = jenc_double(ts_to_epoch(lst.st_mtime, lst.st_mtimespec.tv_nsec));
#else
            mtime = jenc_double(ts_to_epoch(lst.st_mtime, lst.st_mtim.tv_nsec));
#endif
        }
    }
    if (raw_mode) return send_raw(200, data, guess_mime(abs));
    std::string mime = guess_mime(abs);
    auto meta = [&](bool binary, const std::string& enc,
                    const std::string& content) {
        json o;
        o["path"] = raw;
        o["absolute_path"] = abs;
        o["size"] = jenc_int((int64_t)data.size());
        o["mtime"] = mtime;
        o["mime_type"] = mime;
        o["is_binary"] = jenc_bool(binary);
        o["encoding"] = enc;
        o["content"] = content;
        return send_json(200, o);
    };
    if (encoding == "text") return meta(false, "utf-8", utf8_sanitize(data));
    if (encoding == "base64") return meta(true, "base64", b64_encode(data));
    // auto: strict UTF-8 + control heuristic, else base64.
    bool valid = true;
    try {
        size_t i = 0;
        while (i < data.size()) {
            unsigned c = (unsigned char)data[i];
            size_t size = 0;
            if (c < 0x80)
                size = 1;
            else if ((c >> 5) == 0x6)
                size = 2;
            else if ((c >> 4) == 0xE)
                size = 3;
            else if ((c >> 3) == 0x1E)
                size = 4;
            else {
                valid = false;
                break;
            }
            if (i + size > data.size()) {
                valid = false;
                break;
            }
            for (size_t j = 1; j < size; ++j)
                if (((unsigned char)data[i + j] >> 6) != 0x2) valid = false;
            if (!valid) break;
            i += size;
        }
    } catch (...) {
        valid = false;
    }
    if (valid && !has_binary_controls(data))
        return meta(false, "utf-8", data);
    return meta(true, "base64", b64_encode(data));
}

HttpResponse parse_write_body(const HttpRequest& req, std::string& data_out) {
    auto it = req.headers.find("content-type");
    std::string ct = it == req.headers.end() ? "" : it->second;
    std::string lower = ct;
    for (auto& c : lower) c = std::tolower((unsigned char)c);
    if (lower.find("application/json") == std::string::npos) {
        data_out = req.body;
        return HttpResponse{200, {}, ""};  // ok marker (status 200)
    }
    json obj;
    try {
        obj = req.body.empty() ? json::object() : json::parse(req.body);
    } catch (...) {
        return send_json(400, jerr(400, "Invalid JSON body."));
    }
    if (!obj.is_object())
        return send_json(400, jerr(400, "JSON body must be an object."));
    bool has_content = obj.contains("content");
    std::string content;
    bool content_is_null = !has_content || obj["content"].is_null();
    if (!content_is_null) {
        if (!obj["content"].is_string())
            return send_json(400, jerr(400, "'content' must be a string."));
        content = obj["content"].get<std::string>();
        // Refine message per encoding below (matches other ports).
    }
    std::string encoding = "text";
    if (obj.contains("encoding") && !obj["encoding"].is_null()) {
        if (!obj["encoding"].is_string())
            return send_json(400, jerr(400, "Invalid encoding: (use text|base64)."));
        encoding = obj["encoding"].get<std::string>();
        for (auto& c : encoding) c = std::tolower((unsigned char)c);
    }
    if (encoding == "text") {
        if (content_is_null) {
            data_out = "";
            return HttpResponse{200, {}, ""};
        }
        if (!obj["content"].is_string())
            return send_json(400, jerr(400, "'content' must be a string for text encoding."));
        data_out = content;
        return HttpResponse{200, {}, ""};
    }
    if (encoding == "base64") {
        if (content_is_null) {
            data_out = "";
            return HttpResponse{200, {}, ""};
        }
        if (!obj["content"].is_string())
            return send_json(400, jerr(400, "'content' must be a base64 string."));
        if (!b64_decode(content, data_out))
            return send_json(400, jerr(400, "Invalid base64 'content'."));
        return HttpResponse{200, {}, ""};
    }
    return send_json(
        400, jerr(400, "Invalid encoding: '" + encoding + "' (use text|base64)."));
}

bool write_body_ok(const HttpResponse& r) { return r.status == 200; }

HttpResponse handle_create_file(const HttpRequest& req, const ApiState& st) {
    bool missing = false;
    std::string raw = req_path(req, missing);
    if (missing)
        return send_json(400, jerr(400, "Missing required query parameter: path"));
    std::string abs = resolve_path(raw, st.cwd);
    if (lexists(abs))
        return send_json(409, jerr(409, "Path already exists (use PUT to overwrite).", abs));
    std::string parent = fs::path(abs).parent_path().string();
    if (!is_dir_follow(parent))
        return send_json(404, jerr(404, "Parent directory does not exist.", abs));
    std::string data;
    HttpResponse pr = parse_write_body(req, data);
    if (!write_body_ok(pr)) return pr;
    // O_EXCL for race-safe exclusive creation.
    int fd = open(abs.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0666);
    if (fd < 0) {
        if (errno == EEXIST || lexists(abs))
            return send_json(409, jerr(409, "Path already exists (use PUT to overwrite).", abs));
        if (errno == ENOENT)
            return send_json(404, jerr(404, "Parent directory does not exist.", abs));
        if (errno == EACCES || errno == EPERM)
            return send_json(403, jerr(403, "Permission denied", abs));
        return send_json(400, jerr(400, std::string("Cannot create file: ") + strerror(errno), abs));
    }
    bool wok = true;
    size_t off = 0;
    while (off < data.size()) {
        ssize_t n = write(fd, data.data() + off, data.size() - off);
        if (n <= 0) {
            wok = false;
            break;
        }
        off += (size_t)n;
    }
    close(fd);
    if (!wok)
        return send_json(400, jerr(400, "Cannot create file", abs));
    json o;
    o["path"] = raw;
    o["absolute_path"] = abs;
    o["size"] = jenc_int((int64_t)data.size());
    o["mime_type"] = guess_mime(abs);
    return send_json(201, o);
}

HttpResponse handle_overwrite_file(const HttpRequest& req, const ApiState& st) {
    bool missing = false;
    std::string raw = req_path(req, missing);
    if (missing)
        return send_json(400, jerr(400, "Missing required query parameter: path"));
    std::string abs = resolve_path(raw, st.cwd);
    if (!lexists(abs))
        return send_json(404, jerr(404, "Path does not exist", abs));
    if (is_dir_follow(abs) && !is_symlink(abs))
        return send_json(400, jerr(400, "Path is a directory, not a file", abs));
    std::string data;
    HttpResponse pr = parse_write_body(req, data);
    if (!write_body_ok(pr)) return pr;
    FILE* fh = fopen(abs.c_str(), "wb");
    if (!fh) {
        if (errno == EACCES || errno == EPERM)
            return send_json(403, jerr(403, "Permission denied", abs));
        if (errno == ENOENT)
            return send_json(404, jerr(404, "Path does not exist", abs));
        if (is_dir_follow(abs))
            return send_json(400, jerr(400, "Path is a directory, not a file", abs));
        return send_json(400, jerr(400, std::string("Cannot write file: ") + strerror(errno), abs));
    }
    bool wok = data.empty() ||
               fwrite(data.data(), 1, data.size(), fh) == data.size();
    fclose(fh);
    if (!wok)
        return send_json(400, jerr(400, "Cannot write file", abs));
    json o;
    o["path"] = raw;
    o["absolute_path"] = abs;
    o["size"] = jenc_int((int64_t)data.size());
    o["mime_type"] = guess_mime(abs);
    return send_json(200, o);
}

HttpResponse handle_delete_file(const HttpRequest& req, const ApiState& st) {
    bool missing = false;
    std::string raw = req_path(req, missing);
    if (missing)
        return send_json(400, jerr(400, "Missing required query parameter: path"));
    std::string abs = resolve_path(raw, st.cwd);
    if (!lexists(abs))
        return send_json(404, jerr(404, "Path does not exist", abs));
    if (is_dir_follow(abs) && !is_symlink(abs))
        return send_json(400,
                         jerr(400, "Path is a directory, not a file (use DELETE /api/dir).", abs));
    if (unlink(abs.c_str()) != 0) {
        if (errno == ENOENT)
            return send_json(404, jerr(404, "Path does not exist", abs));
        if (errno == EACCES || errno == EPERM)
            return send_json(403, jerr(403, "Permission denied", abs));
        return send_json(400, jerr(400, std::string("Cannot delete file: ") + strerror(errno), abs));
    }
    json o;
    o["path"] = raw;
    o["absolute_path"] = abs;
    o["deleted"] = jenc_bool(true);
    return send_json(200, o);
}

HttpResponse handle_delete_dir(const HttpRequest& req, const ApiState& st) {
    bool missing = false;
    std::string raw = req_path(req, missing);
    if (missing)
        return send_json(400, jerr(400, "Missing required query parameter: path"));
    std::string abs = resolve_path(raw, st.cwd);
    if (!lexists(abs))
        return send_json(404, jerr(404, "Path does not exist", abs));
    if (is_symlink(abs) || !is_dir_follow(abs))
        return send_json(400, jerr(400, "Path is not a directory", abs));
    if (rmdir(abs.c_str()) != 0) {
        if (errno == ENOENT)
            return send_json(404, jerr(404, "Path does not exist", abs));
        if (errno == EACCES || errno == EPERM)
            return send_json(403, jerr(403, "Permission denied", abs));
        // Non-empty?
        DIR* dir = opendir(abs.c_str());
        bool nonempty = false;
        if (dir) {
            struct dirent* ent;
            while ((ent = readdir(dir)) != nullptr) {
                std::string name(ent->d_name);
                if (name != "." && name != "..") {
                    nonempty = true;
                    break;
                }
            }
            closedir(dir);
        }
        if (nonempty)
            return send_json(400,
                             jerr(400, "Cannot delete directory: Directory not empty: '" + abs + "'",
                                  abs));
        return send_json(400,
                         jerr(400, std::string("Cannot delete directory: ") + strerror(errno), abs));
    }
    json o;
    o["path"] = raw;
    o["absolute_path"] = abs;
    o["deleted"] = jenc_bool(true);
    return send_json(200, o);
}

}  // namespace
