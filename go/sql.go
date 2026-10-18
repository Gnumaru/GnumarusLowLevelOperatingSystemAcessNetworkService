package main

import (
	"database/sql"
	"fmt"
	"strings"
)

// Single-statement scan, classification, literal binding.

type BadParams struct{ Msg string }

func (e *BadParams) Error() string { return e.Msg }

type SqlScan struct {
	Statements []string
	First      string
	Returning  bool
}

var rowFirstWords = map[string]bool{
	"SELECT": true, "VALUES": true, "WITH": true,
	"EXPLAIN": true, "PRAGMA": true, "TABLE": true,
}

var ddlFirstWords = map[string]bool{
	"CREATE": true, "DROP": true, "ALTER": true, "VACUUM": true,
	"ANALYZE": true, "REINDEX": true, "ATTACH": true, "DETACH": true,
	"BEGIN": true, "COMMIT": true, "ROLLBACK": true, "SAVEPOINT": true,
	"END": true,
}

func isWordChar(c byte) bool {
	return c == '_' || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
}

func isWordCharOrDigit(c byte) bool {
	return isWordChar(c) || (c >= '0' && c <= '9')
}

// ScanSql splits SQL into statements and finds the first keyword.
func ScanSql(sql string) SqlScan {
	var stmts []string
	var cur strings.Builder
	var first string
	var returning bool
	var word strings.Builder
	flush := func() {
		if word.Len() > 0 {
			w := strings.ToUpper(word.String())
			if first == "" {
				first = w
			}
			if w == "RETURNING" {
				returning = true
			}
			word.Reset()
		}
	}
	state := 0
	i := 0
	n := len(sql)
	for i < n {
		c := sql[i]
		switch state {
		case 0:
			switch {
			case c == '\'':
				state = 1
				cur.WriteByte(c)
				i++
			case c == '"':
				state = 2
				cur.WriteByte(c)
				i++
			case c == '`':
				state = 3
				cur.WriteByte(c)
				i++
			case c == '-' && i+1 < n && sql[i+1] == '-':
				state = 4
				i += 2
			case c == '/' && i+1 < n && sql[i+1] == '*':
				state = 5
				i += 2
			case c == ';':
				flush()
				if strings.TrimSpace(cur.String()) != "" {
					stmts = append(stmts, cur.String())
				}
				cur.Reset()
				i++
			case isWordChar(c):
				word.WriteByte(c)
				cur.WriteByte(c)
				i++
			default:
				if !(c >= '0' && c <= '9') && !isWordChar(c) && c != '$' {
					flush()
				}
				cur.WriteByte(c)
				i++
			}
		case 1:
			if c == '\'' && i+1 < n && sql[i+1] == '\'' {
				cur.WriteString("''")
				i += 2
			} else {
				cur.WriteByte(c)
				i++
				if c == '\'' {
					state = 0
				}
			}
		case 2:
			if c == '"' && i+1 < n && sql[i+1] == '"' {
				cur.WriteString(`""`)
				i += 2
			} else {
				cur.WriteByte(c)
				i++
				if c == '"' {
					state = 0
				}
			}
		case 3:
			cur.WriteByte(c)
			i++
			if c == '`' {
				state = 0
			}
		case 4:
			if c == '\n' {
				state = 0
			}
			i++
		default:
			if c == '*' && i+1 < n && sql[i+1] == '/' {
				state = 0
				i += 2
			} else {
				i++
			}
		}
	}
	flush()
	if strings.TrimSpace(cur.String()) != "" {
		stmts = append(stmts, cur.String())
	}
	return SqlScan{stmts, first, returning}
}

// bindNative prepares native driver args: positional passes through,
// :named placeholders are rewritten to `?` (tokens only, never values)
// with sql.Named args in order. database/sql takes `?` only.
func bindNative(sql string, params any) (string, []any, string) {
	var positional []any
	var named map[string]any
	switch p := params.(type) {
	case nil:
		return sql, nil, ""
	case []any:
		if len(p) == 0 {
			return sql, nil, ""
		}
		positional = p
	case map[string]any:
		if len(p) == 0 {
			return sql, nil, ""
		}
		named = p
	default:
		return "", nil, "Field 'params' must be an array or an object."
	}
	if named != nil {
		rewritten, order, errmsg := rewriteNamed(sql, named)
		if errmsg != "" {
			return "", nil, errmsg
		}
		args := make([]any, 0, len(order))
		for _, name := range order {
			args = append(args, sqlNamed(name, named[name]))
		}
		return rewritten, args, ""
	}
	if hasNamed(sql) {
		return "", nil, "Named placeholder with array params."
	}
	return sql, positional, ""
}

// sqlNamed builds a driver named arg (bare "a" matches ":a").
func sqlNamed(name string, v any) any {
	return sql.Named(name, v)
}

// rewriteNamed rewrites :/@/$ named placeholders to `?` (tokens only)
// and returns the placeholder order. Mixed `?` with object params and
// unknown names are errors.
func rewriteNamed(sql string, named map[string]any) (string, []string, string) {
	var out strings.Builder
	var order []string
	state := 0
	i := 0
	n := len(sql)
	for i < n {
		c := sql[i]
		switch state {
		case 0:
			switch {
			case c == '\'':
				state = 1
				out.WriteByte(c)
				i++
			case c == '"':
				state = 2
				out.WriteByte(c)
				i++
			case c == '`':
				state = 3
				out.WriteByte(c)
				i++
			case c == '-' && i+1 < n && sql[i+1] == '-':
				state = 4
				i += 2
			case c == '/' && i+1 < n && sql[i+1] == '*':
				state = 5
				i += 2
			case c == '?':
				return "", nil, "Unnamed placeholder with object params."
			case (c == ':' || c == '@' || c == '$') && i+1 < n &&
				isWordChar(sql[i+1]):
				j := i + 1
				for j < n && isWordCharOrDigit(sql[j]) {
					j++
				}
				name := sql[i+1 : j]
				if _, ok := named[name]; !ok {
					return "", nil, fmt.Sprintf("Missing named param: %s.", name)
				}
				out.WriteByte('?')
				order = append(order, name)
				i = j
			default:
				out.WriteByte(c)
				i++
			}
		case 1:
			if c == '\'' && i+1 < n && sql[i+1] == '\'' {
				out.WriteString("''")
				i += 2
			} else {
				out.WriteByte(c)
				i++
				if c == '\'' {
					state = 0
				}
			}
		case 2:
			if c == '"' && i+1 < n && sql[i+1] == '"' {
				out.WriteString(`""`)
				i += 2
			} else {
				out.WriteByte(c)
				i++
				if c == '"' {
					state = 0
				}
			}
		case 3:
			out.WriteByte(c)
			i++
			if c == '`' {
				state = 0
			}
		case 4:
			if c == '\n' {
				state = 0
			}
			i++
		default:
			if c == '*' && i+1 < n && sql[i+1] == '/' {
				state = 0
				i += 2
			} else {
				i++
			}
		}
	}
	return out.String(), order, ""
}

// hasNamed reports whether any named placeholder appears outside literals.
func hasNamed(sql string) bool {
	state := 0
	i := 0
	n := len(sql)
	for i < n {
		c := sql[i]
		switch state {
		case 0:
			switch {
			case c == '\'':
				state = 1
				i++
			case c == '"':
				state = 2
				i++
			case c == '`':
				state = 3
				i++
			case c == '-' && i+1 < n && sql[i+1] == '-':
				state = 4
				i += 2
			case c == '/' && i+1 < n && sql[i+1] == '*':
				state = 5
				i += 2
			case (c == ':' || c == '@' || c == '$') && i+1 < n &&
				isWordChar(sql[i+1]):
				return true
			default:
				i++
			}
		case 1:
			if c == '\'' && i+1 < n && sql[i+1] == '\'' {
				i += 2
			} else {
				if c == '\'' {
					state = 0
				}
				i++
			}
		case 2:
			if c == '"' && i+1 < n && sql[i+1] == '"' {
				i += 2
			} else {
				if c == '"' {
					state = 0
				}
				i++
			}
		case 3:
			if c == '`' {
				state = 0
			}
			i++
		case 4:
			if c == '\n' {
				state = 0
			}
			i++
		default:
			if c == '*' && i+1 < n && sql[i+1] == '/' {
				state = 0
				i += 2
			} else {
				i++
			}
		}
	}
	return false
}
