// Runs after tsc. tsc rewrites relative import extensions in the emitted JavaScript but not in the declaration files, and it
// drops `/// <reference lib>` lines. Declaration files must name ".js" files (the emitted ones) and must carry the lib they need.
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const dist = fileURLToPath(new URL("../dist/", import.meta.url));
const LIB = '/// <reference lib="esnext.disposable" />\n';
let rewritten = 0;
let referenced = 0;

function walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) walk(path);
    else if (entry.name.endsWith(".d.ts")) fix(path);
  }
}

function fix(path) {
  let text = readFileSync(path, "utf8");
  const next = text.replace(/(["'])(\.{1,2}\/[^"']*?)\.ts\1/g, (_m, q, spec) => {
    rewritten++;
    return `${q}${spec}.js${q}`;
  });
  text = next;
  if (text.includes("Symbol.asyncDispose") || text.includes("[Symbol.asyncDispose]") || /asyncDispose/.test(text)) {
    if (!text.startsWith(LIB)) {
      text = LIB + text;
      referenced++;
    }
  }
  writeFileSync(path, text);
}

walk(dist);
let left = 0;
(function check(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) check(path);
    else if (/\.(d\.ts|js)$/.test(entry.name)) left += (readFileSync(path, "utf8").match(/from\s+["']\.{1,2}\/[^"']*\.ts["']/g) ?? []).length;
  }
})(dist);
console.log(`postbuild: rewrote ${rewritten} specifiers, added ${referenced} lib reference(s), ${left} .ts specifiers left`);
if (left > 0) process.exit(1);
