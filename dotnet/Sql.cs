using System.Runtime.InteropServices;

/// <summary>Single-statement scan, classification, literal binding.</summary>
static class Sql
{
    public sealed record Scan(List<string> Statements, string First, bool Returning);

    public static readonly HashSet<string> RowFirst = new(
        ["SELECT", "VALUES", "WITH", "EXPLAIN", "PRAGMA", "TABLE"],
        StringComparer.Ordinal);

    public static readonly HashSet<string> DdlFirst = new(
        ["CREATE", "DROP", "ALTER", "VACUUM", "ANALYZE", "REINDEX",
            "ATTACH", "DETACH", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "END"],
        StringComparer.Ordinal);

    private static bool IsWord(char c) => char.IsLetter(c) || c == '_';

    public static Scan ScanSql(string sql)
    {
        var stmts = new List<string>();
        var cur = new System.Text.StringBuilder();
        string? first = null;
        var returning = false;
        var word = new System.Text.StringBuilder();
        void FlushWord()
        {
            if (word.Length > 0)
            {
                var w = word.ToString().ToUpperInvariant();
                first ??= w;
                if (w == "RETURNING") returning = true;
                word.Clear();
            }
        }
        var state = 0;
        var i = 0;
        while (i < sql.Length)
        {
            var c = sql[i];
            if (state == 0)
            {
                if (c == '\'') { state = 1; cur.Append(c); i++; }
                else if (c == '"') { state = 2; cur.Append(c); i++; }
                else if (c == '`') { state = 3; cur.Append(c); i++; }
                else if (c == '-' && i + 1 < sql.Length && sql[i + 1] == '-') { state = 4; i += 2; }
                else if (c == '/' && i + 1 < sql.Length && sql[i + 1] == '*') { state = 5; i += 2; }
                else if (c == ';')
                {
                    FlushWord();
                    if (cur.ToString().Trim() != "") stmts.Add(cur.ToString());
                    cur.Clear(); i++;
                }
                else if (IsWord(c)) { word.Append(c); cur.Append(c); i++; }
                else
                {
                    if (!char.IsLetterOrDigit(c) && c != '_' && c != '$') FlushWord();
                    cur.Append(c); i++;
                }
            }
            else if (state == 1)
            {
                if (c == '\'' && i + 1 < sql.Length && sql[i + 1] == '\'')
                { cur.Append("''"); i += 2; }
                else { cur.Append(c); i++; if (c == '\'') state = 0; }
            }
            else if (state == 2)
            {
                if (c == '"' && i + 1 < sql.Length && sql[i + 1] == '"')
                { cur.Append("\"\""); i += 2; }
                else { cur.Append(c); i++; if (c == '"') state = 0; }
            }
            else if (state == 3)
            {
                cur.Append(c); i++;
                if (c == '`') state = 0;
            }
            else if (state == 4)
            {
                if (c == '\n') state = 0;
                i++;
            }
            else
            {
                if (c == '*' && i + 1 < sql.Length && sql[i + 1] == '/') { state = 0; i += 2; }
                else i++;
            }
        }
        FlushWord();
        if (cur.ToString().Trim() != "") stmts.Add(cur.ToString());
        return new Scan(stmts, first ?? "", returning);
    }

    /// <summary>
    /// Rewrite placeholders for Microsoft.Data.Sqlite, which takes named
    /// parameters only. Positional `?` become `$p1..$pN` (tokens only,
    /// never values); `:named` pass through untouched. Values go through
    /// Parameters.AddWithValue. Returns (sql, positionalCount).
    /// </summary>
    public static (string Sql, int Placeholders) RewritePlaceholders(
        string sql, bool named)
    {
        var out_ = new System.Text.StringBuilder();
        var count = 0;
        var state = 0;
        var i = 0;
        while (i < sql.Length)
        {
            var c = sql[i];
            if (state == 0)
            {
                if (c == '\'') { state = 1; out_.Append(c); i++; }
                else if (c == '"') { state = 2; out_.Append(c); i++; }
                else if (c == '`') { state = 3; out_.Append(c); i++; }
                else if (c == '-' && i + 1 < sql.Length && sql[i + 1] == '-') { state = 4; i += 2; }
                else if (c == '/' && i + 1 < sql.Length && sql[i + 1] == '*') { state = 5; i += 2; }
                else if (c == '?')
                {
                    if (named) throw new BadParams("Unnamed placeholder with object params.");
                    count++;
                    out_.Append("$p" + count);
                    i++;
                }
                else if ((c == ':' || c == '@' || c == '$') && i + 1 < sql.Length &&
                    (char.IsLetter(sql[i + 1]) || sql[i + 1] == '_'))
                {
                    var j = i + 1;
                    while (j < sql.Length && (char.IsLetterOrDigit(sql[j]) || sql[j] == '_')) j++;
                    if (!named) throw new BadParams("Named placeholder with array params.");
                    out_.Append(sql[i..j]);
                    i = j;
                }
                else { out_.Append(c); i++; }
            }
            else if (state == 1)
            {
                if (c == '\'' && i + 1 < sql.Length && sql[i + 1] == '\'')
                { out_.Append("''"); i += 2; }
                else { out_.Append(c); i++; if (c == '\'') state = 0; }
            }
            else if (state == 2)
            {
                if (c == '"' && i + 1 < sql.Length && sql[i + 1] == '"')
                { out_.Append("\"\""); i += 2; }
                else { out_.Append(c); i++; if (c == '"') state = 0; }
            }
            else if (state == 3)
            {
                out_.Append(c); i++;
                if (c == '`') state = 0;
            }
            else if (state == 4)
            {
                if (c == '\n') state = 0;
                i++;
            }
            else
            {
                if (c == '*' && i + 1 < sql.Length && sql[i + 1] == '/') { state = 0; i += 2; }
                else i++;
            }
        }
        return (out_.ToString(), count);
    }

    /// <summary>Collect named placeholder names in order (tokens only).</summary>
    public static List<string> NamedOrder(string sql)
    {
        var order = new List<string>();
        var state = 0;
        var i = 0;
        while (i < sql.Length)
        {
            var c = sql[i];
            if (state == 0)
            {
                if (c == '\'') { state = 1; i++; }
                else if (c == '"') { state = 2; i++; }
                else if (c == '`') { state = 3; i++; }
                else if (c == '-' && i + 1 < sql.Length && sql[i + 1] == '-') { state = 4; i += 2; }
                else if (c == '/' && i + 1 < sql.Length && sql[i + 1] == '*') { state = 5; i += 2; }
                else if ((c == ':' || c == '@' || c == '$') && i + 1 < sql.Length &&
                    (char.IsLetter(sql[i + 1]) || sql[i + 1] == '_'))
                {
                    var j = i + 1;
                    while (j < sql.Length && (char.IsLetterOrDigit(sql[j]) || sql[j] == '_')) j++;
                    order.Add(sql[(i + 1)..j]);
                    i = j;
                }
                else i++;
            }
            else if (state == 1)
            {
                if (c == '\'' && i + 1 < sql.Length && sql[i + 1] == '\'') i += 2;
                else
                {
                    if (c == '\'') state = 0;
                    i++;
                }
            }
            else if (state == 2)
            {
                if (c == '"' && i + 1 < sql.Length && sql[i + 1] == '"') i += 2;
                else
                {
                    if (c == '"') state = 0;
                    i++;
                }
            }
            else if (state == 3)
            {
                if (c == '`') state = 0;
                i++;
            }
            else if (state == 4)
            {
                if (c == '\n') state = 0;
                i++;
            }
            else
            {
                if (c == '*' && i + 1 < sql.Length && sql[i + 1] == '/') { state = 0; i += 2; }
                else i++;
            }
        }
        return order;
    }

    /// <summary>Map a decoded tag value to a driver value (bool -> 1/0).</summary>
    public static object? ToDb(object? v) => v switch
    {
        null => DBNull.Value,
        bool b => b ? 1L : 0L,
        long l => l,
        int i => (long)i,
        double d => d,
        float f => (double)f,
        string s => s,
        _ => throw new BadParams("Params must be tagged scalars (i:/f:/s:/b:/null)."),
    };

    public sealed class BadParams(string message) : Exception(message);
}

/// <summary>Minimal libc interop: statx (Linux birth/ctime) + kill (pgid kill).</summary>
static class Native
{
    [StructLayout(LayoutKind.Sequential)]
    private struct StatxTimestamp
    {
        public long tv_sec;
        public uint tv_nsec;
        public int __reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Statx
    {
        public uint stx_mask;
        public uint stx_blksize;
        public ulong stx_attributes;
        public uint stx_nlink;
        public uint stx_uid;
        public uint stx_gid;
        public ushort stx_mode;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 1)]
        public ushort[] spare0;
        public ulong stx_ino;
        public ulong stx_size;
        public ulong stx_blocks;
        public ulong stx_attributes_mask;
        public StatxTimestamp stx_atime;
        public StatxTimestamp stx_btime;
        public StatxTimestamp stx_ctime;
        public StatxTimestamp stx_mtime;
        public uint stx_rdev_major;
        public uint stx_rdev_minor;
        public uint stx_dev_major;
        public uint stx_dev_minor;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 14)]
        public ulong[] spare2;
    }

    private const uint STATX_BTIME = 0x00000800;
    private const uint STATX_CTIME = 0x00000080;
    private const int AT_FDCWD = -100;
    private const int AT_SYMLINK_NOFOLLOW = 0x100;

    [DllImport("libc", SetLastError = true)]
    private static extern int statx(int dirfd,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string path,
        int flags, uint mask, ref Statx buf);

    [DllImport("libc", SetLastError = true)]
    private static extern int kill(int pid, int sig);

    private static readonly bool HasStatx =
        OperatingSystem.IsLinux() && Marshal.SizeOf<Statx>() == 256;

    /// <summary>(ctime, btime, size) via statx; each null when
    /// unavailable. Never throws.</summary>
    public static (double? Ctime, double? Btime, long? Size) StatxAll(string path)
    {
        if (!HasStatx) return (null, null, null);
        try
        {
            var buf = new Statx();
            if (statx(AT_FDCWD, path, AT_SYMLINK_NOFOLLOW,
                    STATX_BTIME | STATX_CTIME, ref buf) != 0)
                return (null, null, null);
            double ctime = buf.stx_ctime.tv_sec + buf.stx_ctime.tv_nsec / 1e9;
            double? btime = (buf.stx_mask & STATX_BTIME) != 0
                ? buf.stx_btime.tv_sec + buf.stx_btime.tv_nsec / 1e9
                : null;
            return (ctime, btime, (long)buf.stx_size);
        }
        catch
        {
            return (null, null, null);
        }
    }

    /// <summary>(ctime, btime) epoch seconds; each null when unavailable.</summary>
    public static (double? Ctime, double? Btime) StatxTimes(string path)
    {
        var (ctime, btime, _) = StatxAll(path);
        return (ctime, btime);
    }

    /// <summary>Kill a whole process group (negative pid). Returns false on error.</summary>
    public static bool KillGroup(int pgid)
    {
        try
        {
            return kill(-pgid, 9) == 0;
        }
        catch
        {
            return false;
        }
    }
}
