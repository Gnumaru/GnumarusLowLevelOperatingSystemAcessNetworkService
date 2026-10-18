package main

import (
	"fmt"
	"math"
	"regexp"
	"strconv"
)

// Tagged wire scalars: JSON carries only string/array/object.
// Dynamic values travel as "i:<int>", "f:<float>", "b:true|false",
// "null"; fixed vocabulary and free text stay raw; sqlite row cells
// and params values are fully tagged (strings take "s:").

type BadTag struct{ Msg string }

func (e *BadTag) Error() string { return e.Msg }

var intTagRe = regexp.MustCompile(`^-?(0|[1-9][0-9]*)$`)
var floatTagRe = regexp.MustCompile(`^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$`)

// JEnc encodes a value scalar as a tagged string.
func JEnc(v any) (string, error) {
	switch t := v.(type) {
	case nil:
		return "null", nil
	case bool:
		if t {
			return "b:true", nil
		}
		return "b:false", nil
	case int:
		return "i:" + strconv.Itoa(t), nil
	case int64:
		return "i:" + strconv.FormatInt(t, 10), nil
	case float64:
		if math.IsInf(t, 0) || math.IsNaN(t) {
			return "", fmt.Errorf("non-finite float")
		}
		return "f:" + strconv.FormatFloat(t, 'f', -1, 64), nil
	default:
		return "", fmt.Errorf("jenc takes nil/bool/int/float64")
	}
}

// JTagStr tags a string for fully-tagged positions (row cells, params).
func JTagStr(s string) string { return "s:" + s }

// JDec parses a tagged string back to a value.
func JDec(text any) (any, error) {
	s, ok := text.(string)
	if !ok {
		return nil, &BadTag{"expected a tagged string"}
	}
	if s == "null" {
		return nil, nil
	}
	if len(s) > 2 && s[0] == 'i' && s[1] == ':' {
		body := s[2:]
		if !intTagRe.MatchString(body) {
			return nil, &BadTag{"bad int tag: " + s}
		}
		n, err := strconv.ParseInt(body, 10, 64)
		if err != nil {
			return nil, &BadTag{"bad int tag: " + s}
		}
		return n, nil
	}
	if len(s) > 2 && s[0] == 'f' && s[1] == ':' {
		body := s[2:]
		if !floatTagRe.MatchString(body) {
			return nil, &BadTag{"bad float tag: " + s}
		}
		v, err := strconv.ParseFloat(body, 64)
		if err != nil || math.IsInf(v, 0) || math.IsNaN(v) {
			return nil, &BadTag{"bad float tag: " + s}
		}
		return v, nil
	}
	if s == "b:true" {
		return true, nil
	}
	if s == "b:false" {
		return false, nil
	}
	if len(s) > 2 && s[0] == 's' && s[1] == ':' {
		return s[2:], nil
	}
	return nil, &BadTag{"untagged scalar: " + s}
}
