#!/usr/bin/env bash
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" != "true" && "${FORCE:-0}" != "1" ]]; then
  echo "ERROR: parity script is restricted to GitHub Actions; set FORCE=1 only for an intentional local test." >&2
  exit 2
fi

for command in mvn java python3 unzip diff find sort; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $command" >&2
    exit 2
  }
done

ROOT="$(git rev-parse --show-toplevel)"
PACKAGE="$ROOT/packages/ragleap-app-chart"
JAVA_PROJECT="$ROOT/java/ragleap-app-chart"
CHART="$PACKAGE/helm/ragleap-app-chart/Chart.yaml"

[[ -f "$PACKAGE/pyproject.toml" ]] || { echo "ERROR: missing pyproject.toml" >&2; exit 2; }
[[ -f "$JAVA_PROJECT/pom.xml" ]] || { echo "ERROR: missing Maven POM" >&2; exit 2; }
[[ -f "$CHART" ]] || { echo "ERROR: missing Helm Chart.yaml" >&2; exit 2; }

PY_VERSION="$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$PACKAGE/pyproject.toml" | head -n 1)"
[[ -n "$PY_VERSION" ]] || { echo "ERROR: could not read Python package version" >&2; exit 2; }
POM_VERSION="$(python3 -c 'import sys,xml.etree.ElementTree as ET; r=ET.parse(sys.argv[1]).getroot(); n={"m":"http://maven.apache.org/POM/4.0.0"}; print(r.findtext("m:version", namespaces=n))' "$JAVA_PROJECT/pom.xml")"
CHART_VERSION="$(sed -n 's/^version:[[:space:]]*//p' "$CHART" | head -n 1 | tr -d "\"'[:space:]")"
APP_VERSION="$(sed -n 's/^appVersion:[[:space:]]*//p' "$CHART" | head -n 1 | tr -d "\"'[:space:]")"

echo "Python package version: $PY_VERSION"
echo "Maven artifact version:  $POM_VERSION"
echo "Helm chart version:      $CHART_VERSION"
echo "Helm appVersion:         $APP_VERSION"

[[ "$PY_VERSION" == "$POM_VERSION" ]] || { echo "ERROR: Python/Maven version mismatch" >&2; exit 1; }
[[ "$PY_VERSION" == "$CHART_VERSION" ]] || { echo "ERROR: Python/Helm chart version mismatch" >&2; exit 1; }
[[ "$PY_VERSION" == "$APP_VERSION" ]] || { echo "ERROR: Python/Helm appVersion mismatch" >&2; exit 1; }

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

echo
echo "=== Maven verify (tests, main JAR, sources and Javadocs) ==="
mvn -B -f "$JAVA_PROJECT/pom.xml" verify

JAR="$JAVA_PROJECT/target/ragleap-app-chart-$PY_VERSION.jar"
SOURCES_JAR="$JAVA_PROJECT/target/ragleap-app-chart-$PY_VERSION-sources.jar"
JAVADOC_JAR="$JAVA_PROJECT/target/ragleap-app-chart-$PY_VERSION-javadoc.jar"
for artifact in "$JAR" "$SOURCES_JAR" "$JAVADOC_JAR"; do
  [[ -s "$artifact" ]] || { echo "ERROR: expected artifact missing: $artifact" >&2; exit 1; }
done

echo
echo "=== Build Python wheel (no publishing) ==="
mkdir -p "$TMP/wheels" "$TMP/wheel" "$TMP/jar"
python3 -m pip wheel "$PACKAGE" --no-deps --wheel-dir "$TMP/wheels"
WHEEL="$(find "$TMP/wheels" -maxdepth 1 -type f -name '*.whl' -print -quit)"
[[ -n "$WHEEL" ]] || { echo "ERROR: Python wheel was not produced" >&2; exit 1; }

unzip -q "$WHEEL" -d "$TMP/wheel"
unzip -q "$JAR" -d "$TMP/jar"

WHEEL_PAYLOAD="$TMP/wheel/ragleap_app_chart/helm"
JAR_PAYLOAD="$TMP/jar/ragleap_app_chart/helm"
[[ -d "$WHEEL_PAYLOAD" ]] || { echo "ERROR: wheel chart payload not found at expected path" >&2; exit 1; }
[[ -d "$JAR_PAYLOAD" ]] || { echo "ERROR: JAR chart payload not found at expected path" >&2; exit 1; }

compare_payloads() {
  local left="$1"
  local right="$2"
  local left_list="$TMP/left-files.txt"
  local right_list="$TMP/right-files.txt"

  [[ -d "$left" && -d "$right" ]] || {
    echo "ERROR: a parity payload directory is missing" >&2
    return 2
  }

  (cd "$left" && find . -type f -print | LC_ALL=C sort) > "$left_list"
  (cd "$right" && find . -type f -print | LC_ALL=C sort) > "$right_list"

  diff -u "$left_list" "$right_list"
  diff -ru "$left" "$right"
}

echo
echo "=== Positive parity: file lists and contents ==="
compare_payloads "$WHEEL_PAYLOAD" "$JAR_PAYLOAD"
echo "PASS: JAR Helm payload matches the Python wheel."

echo
echo "=== Break proof 1: removing a chart file must fail parity ==="
mkdir -p "$TMP/break-missing"
cp -a "$WHEEL_PAYLOAD" "$TMP/break-missing/helm"
MISSING_TREE="$TMP/break-missing/helm"
rm -f "$MISSING_TREE/ragleap-app-chart/Chart.yaml"
if compare_payloads "$MISSING_TREE" "$JAR_PAYLOAD" >"$TMP/missing-proof.log" 2>&1; then
  echo "ERROR: missing-file break proof unexpectedly passed" >&2
  exit 1
else
  echo "PASS: parity rejected a missing Chart.yaml."
fi

echo
echo "=== Break proof 2: modifying a chart file must fail parity ==="
mkdir -p "$TMP/break-content"
cp -a "$WHEEL_PAYLOAD" "$TMP/break-content/helm"
CONTENT_TREE="$TMP/break-content/helm"
printf '\n# deliberate parity break-proof mutation\n' >> "$CONTENT_TREE/ragleap-app-chart/Chart.yaml"
if compare_payloads "$CONTENT_TREE" "$JAR_PAYLOAD" >"$TMP/content-proof.log" 2>&1; then
  echo "ERROR: modified-content break proof unexpectedly passed" >&2
  exit 1
else
  echo "PASS: parity rejected altered Chart.yaml content."
fi

echo
echo "PASS: Java app-chart parity and both break-proof checks completed."
echo "No package was published and no Helm release or cluster was touched."
