export const DEFAULT_MAX_CHUNK_LENGTH = 50_000;

export const INJECTION_PATTERNS: readonly string[] = [
  "ignore (all )?(previous|prior|above) instructions",
  "disregard (all )?(previous|prior|above) instructions",
  "you are now",
  "new instructions:",
  "system prompt:",
  "\\bsystem:\\s",
  "forget (everything|all) (you|that)",
  "reveal your (system prompt|instructions)",
  "act as if",
  "do not (follow|obey) (your|the) (previous|original) instructions",
];

const compiledPatterns = INJECTION_PATTERNS.map(
  (pattern) => new RegExp(pattern, "i"),
);

export function sanitize_text(text: string): string {
  if (!text) return text;

  // Unicode Cc/Cf categories, preserving newline and tab.
  return Array.from(text)
    .filter((char) => {
      if (char === "\n" || char === "\t") return true;
      return !/[\p{Cc}\p{Cf}]/u.test(char);
    })
    .join("");
}

export function detect_injection_risk(text: string): string[] {
  if (!text) return [];

  const matches: string[] = [];

  for (const pattern of compiledPatterns) {
    const found = pattern.exec(text);
    if (found) matches.push(found[0]);
  }

  return matches;
}

export function check_length(
  text: string,
  max_length = DEFAULT_MAX_CHUNK_LENGTH,
): boolean {
  // Python len(str) counts Unicode code points, not UTF-16 code units.
  return Array.from(text).length <= max_length;
}
