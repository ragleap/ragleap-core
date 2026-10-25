#!/usr/bin/env bash
# Builds and tests the Java jar of ragleap-terraform, then checks that it holds exactly the same Terraform
# files as the PyPI wheel built from the same source (same paths, same content). Never deploys anything.
# Meant for a GitHub runner. It refuses to run elsewhere unless FORCE=1 (the shared VPS has too little disk).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
J=$ROOT/java/ragleap-terraform
P=$ROOT/packages/ragleap-terraform
T=/tmp/tf-parity

stop() { echo "STOP: $*"; exit 1; }
if [ "${GITHUB_ACTIONS:-}" != "true" ] && [ "${FORCE:-}" != "1" ]; then
  stop "not on a GitHub runner. Set FORCE=1 to run anyway."
fi
for c in mvn java python3 unzip; do command -v "$c" >/dev/null || stop "$c not found"; done

PYV=$(sed -n 's/^version = "\(.*\)"/\1/p' "$P/pyproject.toml" | head -1)
JV=$(sed -n 's|^    <version>\(.*\)</version>|\1|p' "$J/pom.xml" | head -1)
echo "python package version: $PYV; java pom version: $JV"
[ -n "$PYV" ] && [ "$PYV" = "$JV" ] || stop "the Python and Java versions differ or could not be read"

echo "== mvn verify (compile, unit tests, jar, sources jar, javadoc jar)"
( cd "$J" && mvn -B verify )
JAR=$J/target/ragleap-terraform-$JV.jar
for f in "$JAR" "$J/target/ragleap-terraform-$JV-sources.jar" "$J/target/ragleap-terraform-$JV-javadoc.jar"; do
  [ -f "$f" ] || stop "missing build output: $f"
done
echo "PASS  jar, sources jar and javadoc jar were built"

echo "== building the wheel from the same source"
rm -rf "$T" && mkdir -p "$T/w" "$T/j"
python3 -m pip wheel "$P" --no-deps -w "$T" -q
WHL=$(ls "$T"/*.whl)
echo "wheel: $(basename "$WHL")"
unzip -q "$WHL" -d "$T/w"
unzip -q "$JAR" 'ragleap_terraform/*' -d "$T/j"

( cd "$T/w" && find ragleap_terraform -type f ! -name '__init__.py' ! -path '*/__pycache__/*' | sort ) > "$T/wheel.txt"
( cd "$T/j" && find ragleap_terraform -type f | sort ) > "$T/jar.txt"
echo "files in the wheel: $(wc -l < "$T/wheel.txt"); files in the jar: $(wc -l < "$T/jar.txt")"
[ -s "$T/wheel.txt" ] || stop "the wheel list is empty, so the comparison would prove nothing"

if diff "$T/wheel.txt" "$T/jar.txt"; then
  echo "PASS  the jar and the wheel hold the same file list"
else
  echo "FAIL  the file lists differ (see the diff above)"; exit 1
fi
if diff -r -x __init__.py -x __pycache__ "$T/w/ragleap_terraform" "$T/j/ragleap_terraform"; then
  echo "PASS  every file has identical content"
else
  echo "FAIL  file contents differ (see the diff above)"; exit 1
fi
echo "PARITY OK. Not exercised: publishing to Maven Central (needs the owner's GPG key and Central token), running Terraform."
