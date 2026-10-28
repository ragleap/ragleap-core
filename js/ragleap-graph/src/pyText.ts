// Helpers that reproduce Python str behaviour where it differs from JavaScript.

/** Characters Python treats as whitespace for str.isspace() and re \s (str patterns). */
export const PY_WS =
  "[\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";

/** len() in code points, like Python. */
export function cpLength(s: string): number {
  let n = 0;
  for (const _ch of s) n++;
  return n;
}

/** s[:max] in code points, like Python. */
export function cpTruncate(s: string, max: number): string {
  const cps = Array.from(s);
  return cps.length <= max ? s : cps.slice(0, max).join("");
}

/** Python str.lower() */
export function pyLower(s: string): string {
  return s.toLowerCase();
}

const LOWER_OR_TITLE = /[\p{Lowercase}\p{Lt}]/u;
const UPPER = /\p{Uppercase}/u;

/** Python str.isupper(): at least one uppercase letter and no lowercase or titlecase letters. */
export function pyIsUpper(s: string): boolean {
  let cased = false;
  for (const ch of s) {
    if (LOWER_OR_TITLE.test(ch)) return false;
    if (UPPER.test(ch)) cased = true;
  }
  return cased;
}

const TITLE_DIGRAPH: Record<string, string> = {
  "\u01c4": "\u01c5", "\u01c5": "\u01c5", "\u01c6": "\u01c5",
  "\u01c7": "\u01c8", "\u01c8": "\u01c8", "\u01c9": "\u01c8",
  "\u01ca": "\u01cb", "\u01cb": "\u01cb", "\u01cc": "\u01cb",
  "\u01f1": "\u01f2", "\u01f2": "\u01f2", "\u01f3": "\u01f2",
};

function titleFirst(ch: string): string {
  const digraph = TITLE_DIGRAPH[ch];
  if (digraph !== undefined) return digraph;
  const up = ch.toUpperCase();
  const cp = ch.codePointAt(0) as number;
  const ligature = cp === 0xdf || (cp >= 0xfb00 && cp <= 0xfb06) || (cp >= 0xfb13 && cp <= 0xfb17);
  if (ligature) {
    const parts = Array.from(up);
    return parts[0] + parts.slice(1).join("").toLowerCase();
  }
  return up;
}

/** Python str.capitalize(): first character titlecased, the rest lowercased. */
export function pyCapitalize(s: string): string {
  if (s === "") return "";
  const first = String.fromCodePoint(s.codePointAt(0) as number);
  const lowerAll = s.toLowerCase();
  const lowerFirst = first.toLowerCase();
  const rest = lowerAll.startsWith(lowerFirst)
    ? lowerAll.slice(lowerFirst.length)
    : s.slice(first.length).toLowerCase();
  return titleFirst(first) + rest;
}

/** Python s.strip(chars) for a set of BMP characters. */
export function pyStripChars(s: string, chars: string): string {
  let start = 0;
  let end = s.length;
  while (start < end && chars.includes(s[start])) start++;
  while (end > start && chars.includes(s[end - 1])) end--;
  return s.slice(start, end);
}

const WS_EDGE = new RegExp(`^${PY_WS}+|${PY_WS}+$`, "gu");

/** Python str.strip() with no arguments: removes Unicode whitespace at both ends. */
export function pyStrip(s: string): string {
  return s.replace(WS_EDGE, "");
}

/** Compares strings by code point like Python does (JavaScript's < compares UTF-16 units). Returns -1, 0 or 1. */
export function cpCompare(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  const n = Math.min(x.length, y.length);
  for (let i = 0; i < n; i++) {
    const d = (x[i].codePointAt(0) as number) - (y[i].codePointAt(0) as number);
    if (d !== 0) return d < 0 ? -1 : 1;
  }
  return x.length === y.length ? 0 : x.length < y.length ? -1 : 1;
}

/** Python repr() of a str, for the common cases (quote choice, backslash, \n \r \t, other control characters). */
export function pyRepr(s: string): string {
  const hasSingle = s.includes("'");
  const hasDouble = s.includes('"');
  const quote = hasSingle && !hasDouble ? '"' : "'";
  let out = "";
  for (const ch of s) {
    const cp = ch.codePointAt(0) as number;
    if (ch === "\\") out += "\\\\";
    else if (ch === quote) out += "\\" + ch;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (cp < 0x20 || cp === 0x7f) out += "\\x" + cp.toString(16).padStart(2, "0");
    else out += ch;
  }
  return quote + out + quote;
}
