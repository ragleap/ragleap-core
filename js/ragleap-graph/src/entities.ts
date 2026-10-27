import { cpLength, cpTruncate, pyCapitalize, pyIsUpper, pyLower, pyStripChars, PY_WS } from "./pyText.ts";

export const SENTENCE_INITIAL_STOPWORDS: ReadonlySet<string> = new Set([
  "What", "Who", "When", "Where", "Why", "How", "Which",
  "The", "This", "That", "These", "Those",
  "Is", "Are", "Was", "Were", "Do", "Does", "Did",
  "Can", "Could", "Would", "Should", "Will", "May", "Might",
]);

const STRIP_CHARS = " \t\r\n.,;:!?()[]{}'\"";
const WS_RUN = new RegExp(`${PY_WS}+`, "gu");
// Python's \b is Unicode-aware, JavaScript's is ASCII only: use explicit lookarounds.
const NOT_WORD_BEFORE = "(?<![\\p{L}\\p{N}_])";
const NOT_WORD_AFTER = "(?![\\p{L}\\p{N}_])";
const ACRONYM = new RegExp(`${NOT_WORD_BEFORE}[A-Z]{2,}(?:-[A-Z0-9]+)?${NOT_WORD_AFTER}`, "gu");
const PHRASE = new RegExp(
  `${NOT_WORD_BEFORE}(?:[A-Z][a-z]{2,})(?:${PY_WS}+[A-Z][a-z]{2,}){0,2}${NOT_WORD_AFTER}`,
  "gu",
);

/** Port of GraphIndex._normalize_entity_name. */
export function normalizeEntityName(rawName: string): string {
  if (!rawName) return "";
  const name = pyStripChars(rawName.replace(WS_RUN, " "), STRIP_CHARS);
  if (cpLength(name) < 3) return "";
  let normalized: string;
  if (pyIsUpper(name)) {
    normalized = name;
  } else {
    normalized = name.split(" ").map(pyCapitalize).join(" ");
  }
  if (cpLength(normalized) > 120) normalized = cpTruncate(normalized, 120);
  return normalized;
}

/** Port of GraphIndex._extract_entity_candidates_from_text. */
export function extractEntityCandidates(
  text: string,
  maxEntities = 12,
  domainTerms: readonly string[] | null = null,
): string[] {
  if (!text) return [];
  const candidates: string[] = [];
  for (const m of text.matchAll(ACRONYM)) candidates.push(m[0]);
  for (const m of text.matchAll(PHRASE)) {
    if (!SENTENCE_INITIAL_STOPWORDS.has(m[0])) candidates.push(m[0]);
  }
  if (domainTerms && domainTerms.length > 0) {
    const textLower = pyLower(text);
    for (const term of domainTerms) {
      if (textLower.includes(pyLower(term))) candidates.push(term);
    }
  }
  const seen = new Set<string>();
  const final: string[] = [];
  for (const cand of candidates) {
    const normalized = normalizeEntityName(cand);
    if (!normalized) continue;
    const key = pyLower(normalized);
    if (seen.has(key)) continue;
    seen.add(key);
    final.push(normalized);
    if (final.length >= maxEntities) break;
  }
  return final;
}

/** Port of GraphIndex.extract_query_entities. */
export function extractQueryEntities(
  query: string,
  maxEntities = 10,
  domainTerms: readonly string[] | null = null,
): string[] {
  return extractEntityCandidates(query, maxEntities, domainTerms);
}
