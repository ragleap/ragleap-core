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
