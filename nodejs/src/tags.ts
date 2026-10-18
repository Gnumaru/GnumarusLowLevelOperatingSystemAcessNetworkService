/** Tagged wire scalars: JSON carries only string/array/object.
 *  Dynamic values travel as "i:<int>", "f:<float>", "b:true|false",
 *  "null"; fixed vocabulary and free text stay raw; sqlite row cells
 *  and params values are fully tagged (strings take "s:").
 */

export class BadTag extends Error {}

const INT_RE = /^-?(0|[1-9][0-9]*)$/;
const FLOAT_RE = /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;

export function jenc(v: null | boolean | number): string {
  if (v === null) return "null";
  if (typeof v === "boolean") return v ? "b:true" : "b:false";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) throw new Error("non-finite number");
    if (Number.isInteger(v)) return `i:${v}`;
    return `f:${String(v)}`;
  }
  throw new Error("jenc takes null/boolean/number");
}

export function jtagStr(s: string): string {
  return "s:" + s;
}

export type Tagged = null | boolean | number | string;

export function jdec(text: unknown): Tagged {
  if (typeof text !== "string") throw new BadTag("expected a tagged string");
  if (text === "null") return null;
  if (text.startsWith("i:")) {
    const body = text.slice(2);
    if (!INT_RE.test(body)) throw new BadTag(`bad int tag: ${text}`);
    return parseInt(body, 10);
  }
  if (text.startsWith("f:")) {
    const body = text.slice(2);
    if (!FLOAT_RE.test(body)) throw new BadTag(`bad float tag: ${text}`);
    const v = Number(body);
    if (!Number.isFinite(v)) throw new BadTag(`bad float tag: ${text}`);
    return v;
  }
  if (text === "b:true") return true;
  if (text === "b:false") return false;
  if (text.startsWith("s:")) return text.slice(2);
  throw new BadTag(`untagged scalar: ${text}`);
}
