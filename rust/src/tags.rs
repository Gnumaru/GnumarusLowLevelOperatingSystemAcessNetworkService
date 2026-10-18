//! Tagged wire scalars: JSON carries only string/array/object.
//! Dynamic values travel as "i:<int>", "f:<float>", "b:true|false",
//! "null"; fixed vocabulary and free text stay raw; sqlite row cells
//! and params values are fully tagged (strings take "s:").

#[derive(Debug)]
pub struct BadTag(pub String);

impl std::fmt::Display for BadTag {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for BadTag {}

/// Encode a value scalar as a tagged string.
pub fn jenc_int(n: i64) -> String {
    format!("i:{n}")
}

/// Encode an f64 (finite) as a tagged float, shortest round-trip.
pub fn jenc_float(v: f64) -> Result<String, String> {
    if !v.is_finite() {
        return Err("non-finite float".to_string());
    }
    Ok(format!("f:{}", shortest_f64(v)))
}

fn shortest_f64(v: f64) -> String {
    // {:} prints shortest round-trip; switch exponent form to plain
    // mantissa handling is unnecessary: parsers accept both.
    format!("{v:?}")
}

/// Parse a tagged string back to a value.
/// Returns Bool(v), Int(v), Float(v), Str(v) or Null.
#[derive(Debug, PartialEq)]
pub enum Tagged {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
}

pub fn jdec(text: &str) -> Result<Tagged, BadTag> {
    if text == "null" {
        return Ok(Tagged::Null);
    }
    if let Some(body) = text.strip_prefix("i:") {
        if !valid_int(body) {
            return Err(BadTag(format!("bad int tag: {text}")));
        }
        return body
            .parse::<i64>()
            .map(Tagged::Int)
            .map_err(|_| BadTag(format!("bad int tag: {text}")));
    }
    if let Some(body) = text.strip_prefix("f:") {
        if !valid_float(body) {
            return Err(BadTag(format!("bad float tag: {text}")));
        }
        return body
            .parse::<f64>()
            .map_err(|_| BadTag(format!("bad float tag: {text}")))
            .and_then(|v| {
                if v.is_finite() {
                    Ok(Tagged::Float(v))
                } else {
                    Err(BadTag(format!("bad float tag: {text}")))
                }
            });
    }
    if text == "b:true" {
        return Ok(Tagged::Bool(true));
    }
    if text == "b:false" {
        return Ok(Tagged::Bool(false));
    }
    if let Some(body) = text.strip_prefix("s:") {
        return Ok(Tagged::Str(body.to_string()));
    }
    Err(BadTag(format!("untagged scalar: {text}")))
}

fn valid_int(body: &str) -> bool {
    let b = body.strip_prefix('-').unwrap_or(body);
    if b.is_empty() {
        return false;
    }
    if b.len() > 1 && b.starts_with('0') {
        return false;
    }
    b.bytes().all(|c| c.is_ascii_digit())
}

fn valid_float(body: &str) -> bool {
    let b = body.strip_prefix('-').unwrap_or(body);
    if b.is_empty() {
        return false;
    }
    // mantissa int[.frac] | .frac, plus optional exponent
    let (mant, exp) = match b.find(['e', 'E']) {
        Some(i) => (&b[..i], Some(&b[i + 1..])),
        None => (b, None),
    };
    if let Some(e) = exp {
        let digits = e.strip_prefix(['+', '-']).unwrap_or(e);
        if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
            return false;
        }
    }
    if mant.is_empty() {
        return false;
    }
    match mant.find('.') {
        None => mant.bytes().all(|c| c.is_ascii_digit()) && !mant.is_empty(),
        Some(i) => {
            let (a, b) = (&mant[..i], &mant[i + 1..]);
            (!a.is_empty() || !b.is_empty())
                && a.bytes().all(|c| c.is_ascii_digit())
                && b.bytes().all(|c| c.is_ascii_digit())
        }
    }
}
