import java.io.File

/** Multi-source configuration: CLI flag > env var > stdin > config.cfg > default. */

val CONFIG_KEYS = listOf("host", "port", "token", "no_auth")
const val CONFIG_FILE_NAME = "config.cfg"
const val CONFIG_MAX_BYTES = 1024 * 1024
const val CONFIG_ENV_PREFIX = "GNUMARUS_"

class ConfigError(msg: String) : Exception(msg)

data class ResolvedConfig(
    val host: String,
    val port: Int,
    val token: String?,
    val noAuth: Boolean,
    val sources: Map<String, String>
)

fun parseKvBytes(data: ByteArray): Map<String, String> {
    if (data.any { it == 0.toByte() }) return mapOf()
    val text = try {
        data.toString(Charsets.UTF_8)
    } catch (e: Exception) {
        return mapOf()
    }
    // Strict UTF-8 check: decoder with REPORT (toString(charset) replaces).
    try {
        Charsets.UTF_8.newDecoder()
            .onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
            .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
            .decode(java.nio.ByteBuffer.wrap(data))
    } catch (e: java.nio.charset.CharacterCodingException) {
        return mapOf()
    }
    val cfg = LinkedHashMap<String, String>()
    for (rawLine in text.lines()) {
        val line = rawLine.trim()
        if (line.isEmpty() || line.startsWith("#") || line.startsWith(";")) continue
        if (line.startsWith("[") && line.endsWith("]")) continue
        val eq = line.indexOf('=')
        if (eq < 0) continue
        val key = line.substring(0, eq).trim().lowercase()
        var value = line.substring(eq + 1).trim()
        if (value.length >= 2 && (value.startsWith("'") && value.endsWith("'") ||
                value.startsWith("\"") && value.endsWith("\""))) {
            value = value.substring(1, value.length - 1)
        }
        if (key in CONFIG_KEYS) cfg[key] = value
    }
    return cfg
}

/** Candidate config.cfg locations: working dir, jar dir, install root.
 *  First existing file wins (a binary/unreadable one parses to {}). */
fun configCandidates(): List<File> {
    val out = ArrayList<File>()
    out.add(File(System.getProperty("user.dir"), CONFIG_FILE_NAME))
    try {
        val loc = object {}.javaClass.protectionDomain?.codeSource?.location
            ?.toURI()?.let { File(it) }
        val jarDir = if (loc != null && loc.isFile) loc.parentFile else null
        if (jarDir != null) {
            out.add(File(jarDir, CONFIG_FILE_NAME))
            if (jarDir.name == "lib" && jarDir.parentFile != null) {
                out.add(File(jarDir.parentFile, CONFIG_FILE_NAME))
            }
        }
    } catch (e: Exception) {
    }
    return out
}

fun loadFileConfig(): Map<String, String> {
    for (cand in configCandidates()) {
        try {
            if (!cand.isFile) continue
            return cand.inputStream().use { it.readNBytes(CONFIG_MAX_BYTES) }
                .let { parseKvBytes(it) }
        } catch (e: Exception) {
            continue
        }
    }
    return mapOf()
}

fun loadStdinConfig(): Map<String, String> {
    // System.console() != null means an interactive terminal: never block on it.
    if (System.console() != null) return mapOf()
    return try {
        val data = System.`in`.readNBytes(CONFIG_MAX_BYTES)
        if (data.isEmpty()) mapOf() else parseKvBytes(data)
    } catch (e: Exception) {
        mapOf()
    }
}

fun parseBool(value: Any?, source: String): Boolean {
    if (value is Boolean) return value
    val t = value.toString().trim().lowercase()
    if (t in setOf("1", "true", "yes", "on")) return true
    if (t in setOf("0", "false", "no", "off")) return false
    throw ConfigError("invalid boolean for no_auth from $source: '$value' (use true/false)")
}

fun parsePort(value: Any?, source: String): Int {
    if (value is Boolean) throw ConfigError("invalid port from $source: '$value' (1-65535)")
    val port = try {
        when (value) {
            is Number -> value.toLong()
            else -> value.toString().trim().toLong(10)
        }
    } catch (e: NumberFormatException) {
        throw ConfigError("invalid port from $source: '$value' (1-65535)")
    }
    if (port < 1 || port > 65535) throw ConfigError("port from $source out of range: $port (1-65535)")
    return port.toInt()
}

fun resolveConfig(
    cli: Map<String, Any?>,
    stdinCfg: Map<String, String>,
    fileCfg: Map<String, String>
): ResolvedConfig {
    val envCfg = HashMap<String, String>()
    for (key in CONFIG_KEYS) {
        val v = System.getenv(CONFIG_ENV_PREFIX + key.uppercase())
        if (!v.isNullOrEmpty()) envCfg[key] = v
    }
    val raw = HashMap<String, Any?>()
    val sources = HashMap<String, String>()
    for (key in CONFIG_KEYS) {
        val layer = when {
            cli.containsKey(key) && cli[key] != null -> "cli" to cli
            envCfg.containsKey(key) -> "env" to envCfg
            stdinCfg.containsKey(key) -> "stdin" to stdinCfg
            fileCfg.containsKey(key) -> "file" to fileCfg
            else -> null
        }
        if (layer != null) {
            raw[key] = layer.second[key]
            sources[key] = layer.first
        } else {
            sources[key] = "default"
        }
    }
    val host = (raw["host"]?.toString() ?: "127.0.0.1").trim()
    if (host.isEmpty()) throw ConfigError("host from ${sources["host"]} must not be empty")
    if (!bindAllowed(host)) throw ConfigError("host from ${sources["host"]} must be a loopback address (127.0.0.1, ::1 or localhost), got '$host'")
    val port = if (raw.containsKey("port")) parsePort(raw["port"], sources["port"]!!) else 1423
    val noAuth = if (raw.containsKey("no_auth")) parseBool(raw["no_auth"], sources["no_auth"]!!) else false
    var token: String? = null
    if (noAuth) {
        sources["token"] = "disabled"
    } else if (raw.containsKey("token")) {
        token = raw["token"].toString()
        if (token.isEmpty()) throw ConfigError("token from ${sources["token"]} must not be empty")
    } else {
        val bytes = ByteArray(32)
        java.security.SecureRandom().nextBytes(bytes)
        token = java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
        sources["token"] = "generated"
    }
    return ResolvedConfig(host, port, token, noAuth, sources)
}
