import { createHash } from "node:crypto";

/** Deterministic MERGE key: SHA-256 (hex) of the parts joined with a NUL character. */
export function compositeKey(...parts: string[]): string {
  const joined = parts.join("\x00");
  return createHash("sha256").update(joined, "utf8").digest("hex");
}
