// SQLite endpoints (included by api.cpp; uses its includes and
// shared helpers). Native binding via the C API (? + :named both
// native, no translation needed).
namespace {

// --- minimal statement scan (count + classify + placeholder check) ---

struct SqlScan {
    int statements = 0;
    std::string first;
    bool returning = false;
    bool has_q = false;
    bool has_named = false;
    std::vector<std::string> named_order;  // :named placeholders in order
};

bool is_word(char c) {
    return c == '_' || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
}

bool is_word_char(char c) {
    return is_word(c) || (c >= '0' && c <= '9');
}

SqlScan scan_sql(const std::string& sql) {
    SqlScan out;
    std::string cur_word;
    bool in_stmt = false;
    auto flush = [&]() {
        if (!cur_word.empty()) {
            std::string w = cur_word;
            for (auto& c : w) c = std::toupper((unsigned char)c);
            if (out.first.empty()) out.first = w;
            if (w == "RETURNING") out.returning = true;
            cur_word.clear();
        }
    };
    int state = 0;  // 0 normal, 1 squote, 2 dquote, 3 bquote, 4 line, 5 block
    size_t i = 0, n = sql.size();
    while (i < n) {
        char c = sql[i];
        if (state == 0) {
            if (c == '\'') {
                state = 1;
                i++;
            } else if (c == '"') {
                state = 2;
                i++;
            } else if (c == '`') {
                state = 3;
                i++;
            } else if (c == '-' && i + 1 < n && sql[i + 1] == '-') {
                state = 4;
                i += 2;
            } else if (c == '/' && i + 1 < n && sql[i + 1] == '*') {
                state = 5;
                i += 2;
            } else if (c == ';') {
                flush();
                out.statements++;
                in_stmt = false;
                i++;
            } else if (c == '?') {
                out.has_q = true;
                in_stmt = true;
                i++;
            } else if ((c == ':' || c == '@' || c == '$') && i + 1 < n &&
                       is_word(sql[i + 1])) {
                out.has_named = true;
                in_stmt = true;
                size_t j = i + 1;
                while (j < n && is_word_char(sql[j])) ++j;
                out.named_order.push_back(sql.substr(i + 1, j - i - 1));
                i = j;
            } else {
                if (is_word(c)) {
                    cur_word.push_back(c);
                    in_stmt = true;
                } else if (!std::isalnum((unsigned char)c) && c != '_' &&
                           c != '$') {
                    flush();
                }
                ++i;
            }
        } else if (state == 1) {
            if (c == '\'' && i + 1 < n && sql[i + 1] == '\'') {
                i += 2;
            } else {
                if (c == '\'') state = 0;
                ++i;
            }
        } else if (state == 2) {
            if (c == '"' && i + 1 < n && sql[i + 1] == '"') {
                i += 2;
            } else {
                if (c == '"') state = 0;
                ++i;
            }
        } else if (state == 3) {
            if (c == '`') state = 0;
            ++i;
        } else if (state == 4) {
            if (c == '\n') state = 0;
            ++i;
        } else {
            if (c == '*' && i + 1 < n && sql[i + 1] == '/') {
                state = 0;
                i += 2;
            } else {
                ++i;
            }
        }
    }
    flush();
    if (in_stmt) out.statements++;
    // Trailing empty statement ("SELECT 1;") does not count.
    return out;
}

bool row_first(const std::string& w) {
    return w == "SELECT" || w == "VALUES" || w == "WITH" ||
           w == "EXPLAIN" || w == "PRAGMA" || w == "TABLE";
}

bool ddl_first(const std::string& w) {
    return w == "CREATE" || w == "DROP" || w == "ALTER" ||
           w == "VACUUM" || w == "ANALYZE" || w == "REINDEX" ||
           w == "ATTACH" || w == "DETACH" || w == "BEGIN" ||
           w == "COMMIT" || w == "ROLLBACK" || w == "SAVEPOINT" ||
           w == "END";
}

std::string cell_to_tag(int col_type, sqlite3_stmt* stmt, int i) {
    switch (col_type) {
        case SQLITE_NULL:
            return "null";
        case SQLITE_INTEGER:
            return jenc_int(sqlite3_column_int64(stmt, i));
        case SQLITE_FLOAT:
            return jenc_double(sqlite3_column_double(stmt, i));
        case SQLITE_TEXT: {
            const char* t =
                reinterpret_cast<const char*>(sqlite3_column_text(stmt, i));
            return jtag_str(t ? t : "");
        }
        case SQLITE_BLOB: {
            const void* b = sqlite3_column_blob(stmt, i);
            int n = sqlite3_column_bytes(stmt, i);
            return jtag_str(b64_encode(std::string(
                reinterpret_cast<const char*>(b ? b : ""), (size_t)std::max(n, 0))));
        }
        default:
            return "null";
    }
}

// Bind a decoded tag to parameter idx (1-based). Returns false + message
// on type violation (all inputs are pre-validated; defensive).
bool bind_tag(sqlite3_stmt* stmt, int idx, const Tagged& t,
              std::string& errmsg) {
    int rc = SQLITE_OK;
    if (std::holds_alternative<std::nullptr_t>(t))
        rc = sqlite3_bind_null(stmt, idx);
    else if (auto* b = std::get_if<bool>(&t))
        rc = sqlite3_bind_int64(stmt, idx, *b ? 1 : 0);
    else if (auto* n = std::get_if<int64_t>(&t))
        rc = sqlite3_bind_int64(stmt, idx, *n);
    else if (auto* f = std::get_if<double>(&t))
        rc = sqlite3_bind_double(stmt, idx, *f);
    else if (auto* s = std::get_if<std::string>(&t))
        rc = sqlite3_bind_text(stmt, idx, s->c_str(), (int)s->size(),
                               SQLITE_TRANSIENT);
    else {
        errmsg = "Params must be tagged scalars (i:/f:/s:/b:/null).";
        return false;
    }
    if (rc != SQLITE_OK) {
        errmsg = std::string("SQLite error: ") +
                 sqlite3_errstr(rc);
        return false;
    }
    return true;
}

HttpResponse req_obj(const HttpRequest& req, json& obj_out) {
    auto ctit = req.headers.find("content-type");
    std::string ct = ctit == req.headers.end() ? "" : ctit->second;
    std::string lower = ct;
    for (auto& c : lower) c = std::tolower((unsigned char)c);
    if (lower.find("application/json") == std::string::npos)
        return send_json(
            400, jerr(400, "Content-Type must be application/json."));
    if (req.body.empty()) {
        obj_out = json::object();
        return HttpResponse{200, {}, ""};
    }
    try {
        obj_out = json::parse(req.body);
    } catch (...) {
        return send_json(400, jerr(400, "Invalid JSON body."));
    }
    if (!obj_out.is_object())
        return send_json(400, jerr(400, "JSON body must be an object."));
    return HttpResponse{200, {}, ""};
}

bool req_ok(const HttpResponse& r) { return r.status == 200; }

HttpResponse handle_sqlite_create(const HttpRequest& req,
                                  const ApiState& st) {
    auto it = req.query.find("path");
    if (it == req.query.end() || it->second.empty())
        return send_json(
            400, jerr(400, "Missing required query parameter: path"));
    std::string abs = resolve_path(it->second, st.cwd);
    if (lexists(abs))
        return send_json(
            409, jerr(409, "Database file already exists.", abs));
    std::string parent =
        std::filesystem::path(abs).parent_path().string();
    if (!is_dir_follow(parent))
        return send_json(
            404, jerr(404, "Parent directory does not exist.", abs));
    sqlite3* db = nullptr;
    std::string version;
    if (sqlite3_open(abs.c_str(), &db) != SQLITE_OK || !db) {
        if (db) sqlite3_close(db);
        remove(abs.c_str());
        return send_json(400, jerr(400, "Cannot create database", abs));
    }
    sqlite3_stmt* stmt = nullptr;
    if (sqlite3_prepare_v2(db, "SELECT sqlite_version()", -1, &stmt,
                           nullptr) != SQLITE_OK ||
        !stmt) {
        sqlite3_close(db);
        remove(abs.c_str());
        return send_json(400, jerr(400, "Cannot create database", abs));
    }
    if (sqlite3_step(stmt) == SQLITE_ROW) {
        const char* v =
            reinterpret_cast<const char*>(sqlite3_column_text(stmt, 0));
        version = v ? v : "";
    }
    sqlite3_finalize(stmt);
    sqlite3_close(db);
    if (version.empty()) {
        remove(abs.c_str());
        return send_json(400, jerr(400, "Cannot create database", abs));
    }
    int64_t size = 0;
    struct stat stbuf;
    if (stat(abs.c_str(), &stbuf) == 0) size = (int64_t)stbuf.st_size;
    json o;
    o["path"] = it->second;
    o["absolute_path"] = abs;
    o["size"] = jenc_int(size);
    o["sqlite_version"] = version;
    return send_json(201, o);
}

HttpResponse handle_sqlite_query(const HttpRequest& req,
                                 const ApiState& st) {
    auto itp = req.query.find("path");
    if (itp == req.query.end() || itp->second.empty())
        return send_json(
            400, jerr(400, "Missing required query parameter: path"));
    std::string raw = itp->second;
    std::string abs = resolve_path(raw, st.cwd);
    if (!lexists(abs))
        return send_json(404, jerr(404, "Database file does not exist", abs));
    json obj;
    {
        // Inline body parse with the sqlite-specific error text.
        auto ctit = req.headers.find("content-type");
        std::string ct = ctit == req.headers.end() ? "" : ctit->second;
        std::string lower = ct;
        for (auto& c : lower) c = std::tolower((unsigned char)c);
        if (lower.find("application/json") == std::string::npos)
            return send_json(400, jerr(400, "Content-Type must be application/json "
                                            "with body { sql, params?, timeout?, "
                                            "readonly? }."));
        if (req.body.empty()) {
            obj = json::object();
        } else {
            try {
                obj = json::parse(req.body);
            } catch (...) {
                return send_json(400, jerr(400, "Invalid JSON body."));
            }
            if (!obj.is_object())
                return send_json(400, jerr(400, "JSON body must be an object."));
        }
    }
    auto its = obj.find("sql");
    if (its == obj.end() || !its->is_string() ||
        its->get<std::string>().find_first_not_of(" \t\n\r") ==
            std::string::npos)
        return send_json(
            400, jerr(400, "Missing or empty required field: sql."));
    std::string sql = its->get<std::string>();

    // Params: fully-tagged values -> driver values.
    enum class PK { None, Pos, Named };
    PK pkind = PK::None;
    std::vector<Tagged> pos;
    std::map<std::string, Tagged> named;
    {
        auto itp2 = obj.find("params");
        if (itp2 != obj.end() && !itp2->is_null()) {
            if (itp2->is_array()) {
                pkind = PK::Pos;
                for (auto& v : *itp2) {
                    if (!v.is_string())
                        return send_json(400, jerr(400, "Array params must be tagged "
                                                        "scalars (i:/f:/s:/b:/null)."));
                    try {
                        pos.push_back(jdec(v.get<std::string>()));
                    } catch (const BadTag&) {
                        return send_json(400, jerr(400, "Array params must be tagged "
                                                        "scalars (i:/f:/s:/b:/null)."));
                    }
                }
            } else if (itp2->is_object()) {
                pkind = PK::Named;
                for (auto& [k, v] : itp2->items()) {
                    if (!v.is_string())
                        return send_json(400, jerr(400, "Object params must be tagged "
                                                        "scalars (i:/f:/s:/b:/null)."));
                    try {
                        named[k] = jdec(v.get<std::string>());
                    } catch (const BadTag&) {
                        return send_json(400, jerr(400, "Object params must be tagged "
                                                        "scalars (i:/f:/s:/b:/null)."));
                    }
                }
            } else {
                return send_json(
                    400, jerr(400, "Field 'params' must be an array or an object."));
            }
        }
    }
    double timeout = 5;
    {
        auto it = obj.find("timeout");
        if (it != obj.end() && !it->is_null()) {
            if (!it->is_string())
                return send_json(400, jerr(400, "Field 'timeout' must be a tagged "
                                                "number (i:<int> or f:<float>)."));
            try {
                Tagged t = jdec(it->get<std::string>());
                if (auto* n = std::get_if<int64_t>(&t))
                    timeout = (double)*n;
                else if (auto* f = std::get_if<double>(&t))
                    timeout = *f;
                else
                    return send_json(400, jerr(400, "Field 'timeout' must be a tagged "
                                                    "number (i:<int> or f:<float>)."));
            } catch (const BadTag&) {
                return send_json(400, jerr(400, "Field 'timeout' must be a tagged "
                                                "number (i:<int> or f:<float>)."));
            }
            if (!(timeout > 0 && timeout <= 300))
                return send_json(
                    400, jerr(400, "Field 'timeout' must be within (0, 300]."));
        }
    }
    bool readonly = false;
    {
        auto it = obj.find("readonly");
        if (it != obj.end() && !it->is_null()) {
            if (!it->is_string())
                return send_json(
                    400, jerr(400, "Field 'readonly' must be b:true or b:false."));
            try {
                Tagged t = jdec(it->get<std::string>());
                if (auto* b = std::get_if<bool>(&t))
                    readonly = *b;
                else
                    return send_json(
                        400, jerr(400, "Field 'readonly' must be b:true or b:false."));
            } catch (const BadTag&) {
                return send_json(
                    400, jerr(400, "Field 'readonly' must be b:true or b:false."));
            }
        }
    }

    SqlScan scan = scan_sql(sql);
    if (scan.statements != 1)
        return send_json(
            400, jerr(400, "Send exactly one SQL statement per request.", abs));
    bool rows_mode = row_first(scan.first) || scan.returning;

    int flags = readonly ? SQLITE_OPEN_READONLY
                         : (SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE);
    sqlite3* db = nullptr;
    if (sqlite3_open_v2(abs.c_str(), &db, flags, nullptr) != SQLITE_OK ||
        !db) {
        if (db) sqlite3_close(db);
        return send_json(
            400, jerr(400, std::string("Cannot open database"), abs));
    }
    sqlite3_busy_timeout(db, (int)(timeout * 1000));
    // NOTE: connection closes on every return path below (stateless).
    json payload;
    sqlite3_stmt* stmt = nullptr;
    const char* tail = nullptr;
    int prc = sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, &tail);
    if (prc != SQLITE_OK || !stmt) {
        std::string msg = sqlite3_errmsg(db);
        sqlite3_close(db);
        return send_json(400, jerr(400, "SQLite error: " + msg, abs));
    }
    // Bind natively (? by index; :named via parameter index lookup).
    std::string bind_err;
    bool bind_ok = true;
    if (pkind == PK::Pos) {
        for (size_t i = 0; i < pos.size(); ++i) {
            if (!bind_tag(stmt, (int)i + 1, pos[i], bind_err)) {
                bind_ok = false;
                break;
            }
        }
        if (bind_ok && scan.has_named) {
            bind_ok = false;
            bind_err = "Named placeholder with array params.";
        }
    } else if (pkind == PK::Named) {
        if (scan.has_q) {
            bind_ok = false;
            bind_err = "Unnamed placeholder with object params.";
        } else {
            for (auto& name : scan.named_order) {
                auto it = named.find(name);
                if (it == named.end()) {
                    bind_ok = false;
                    bind_err = "Missing named param: " + name + ".";
                    break;
                }
                int idx = sqlite3_bind_parameter_index(
                    stmt, (":" + name).c_str());
                if (idx <= 0) idx = sqlite3_bind_parameter_index(
                                       stmt, ("@" + name).c_str());
                if (idx <= 0) idx = sqlite3_bind_parameter_index(
                                       stmt, ("$" + name).c_str());
                if (idx <= 0 || !bind_tag(stmt, idx, it->second, bind_err)) {
                    if (bind_err.empty())
                        bind_err = "Missing named param: " + name + ".";
                    bind_ok = false;
                    break;
                }
            }
        }
    }
    if (!bind_ok) {
        sqlite3_finalize(stmt);
        sqlite3_close(db);
        return send_json(400, jerr(400, bind_err, abs));
    }
    if (rows_mode) {
        json columns = json::array();
        for (int i = 0; i < sqlite3_column_count(stmt); ++i) {
            const char* name = sqlite3_column_name(stmt, i);
            columns.push_back(name ? name : "");
        }
        json rows = json::array();
        while (true) {
            int rc = sqlite3_step(stmt);
            if (rc == SQLITE_ROW) {
                json row = json::array();
                for (int i = 0; i < sqlite3_column_count(stmt); ++i)
                    row.push_back(cell_to_tag(sqlite3_column_type(stmt, i),
                                             stmt, i));
                rows.push_back(row);
            } else if (rc == SQLITE_DONE) {
                break;
            } else {
                std::string msg = sqlite3_errmsg(db);
                sqlite3_finalize(stmt);
                sqlite3_close(db);
                return send_json(400, jerr(400, "SQLite error: " + msg, abs));
            }
        }
        sqlite3_finalize(stmt);
        sqlite3_close(db);
        payload["columns"] = columns;
        payload["rows"] = rows;
        payload["rowcount"] = jenc_int((int64_t)rows.size());
    } else {
        int rc = sqlite3_step(stmt);
        if (rc != SQLITE_DONE) {
            std::string msg = sqlite3_errmsg(db);
            sqlite3_finalize(stmt);
            sqlite3_close(db);
            return send_json(400, jerr(400, "SQLite error: " + msg, abs));
        }
        int64_t affected = sqlite3_changes64(db);
        int64_t last_id = sqlite3_last_insert_rowid(db);
        sqlite3_finalize(stmt);
        sqlite3_close(db);
        int64_t rcv = ddl_first(scan.first) ? -1 : affected;
        payload["rowcount"] = jenc_int(rcv);
        payload["lastrowid"] = jenc_int(last_id);
    }
    payload["path"] = raw;
    payload["absolute_path"] = abs;
    return send_json(200, payload);
}

}  // namespace
