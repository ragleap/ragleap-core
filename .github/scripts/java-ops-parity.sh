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
PACKAGE="$ROOT/packages/ragleap-ops"
JAVA_PROJECT="$ROOT/java/ragleap-ops"
CHART="$PACKAGE/helm/ragleap-ops/Chart.yaml"
INIT="$PACKAGE/src/ragleap_ops/__init__.py"

[[ -f "$PACKAGE/pyproject.toml" ]] || { echo "ERROR: missing pyproject.toml" >&2; exit 2; }
[[ -f "$JAVA_PROJECT/pom.xml" ]] || { echo "ERROR: missing Maven POM" >&2; exit 2; }
[[ -f "$CHART" ]] || { echo "ERROR: missing Helm Chart.yaml" >&2; exit 2; }
[[ -f "$INIT" ]] || { echo "ERROR: missing Python package init" >&2; exit 2; }
[[ -d "$PACKAGE/k8s" ]] || { echo "ERROR: missing Kubernetes resource directory" >&2; exit 2; }

PY_VERSION="$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$PACKAGE/pyproject.toml" | head -n 1)"
INIT_VERSION="$(sed -n 's/^[[:space:]]*__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$INIT" | head -n 1)"
POM_VERSION="$(python3 -c 'import sys,xml.etree.ElementTree as ET; r=ET.parse(sys.argv[1]).getroot(); n={"m":"http://maven.apache.org/POM/4.0.0"}; print(r.findtext("m:version", namespaces=n))' "$JAVA_PROJECT/pom.xml")"
CHART_VERSION="$(sed -n 's/^version:[[:space:]]*//p' "$CHART" | head -n 1 | tr -d "\"'[:space:]")"
APP_VERSION="$(sed -n 's/^appVersion:[[:space:]]*//p' "$CHART" | head -n 1 | tr -d "\"'[:space:]")"

echo "Python package version: $PY_VERSION"
echo "Python __version__:     $INIT_VERSION"
echo "Maven artifact version: $POM_VERSION"
echo "Helm chart version:     $CHART_VERSION"
echo "Helm appVersion:        $APP_VERSION"

for version in "$INIT_VERSION" "$POM_VERSION" "$CHART_VERSION" "$APP_VERSION"; do
  [[ "$PY_VERSION" == "$version" ]] || {
    echo "ERROR: Python, __version__, Maven and Helm versions must match" >&2
    exit 1
  }
done

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

echo
echo "=== Maven verify (tests, main JAR, sources and Javadocs) ==="
mvn -B -f "$JAVA_PROJECT/pom.xml" verify

JAR="$JAVA_PROJECT/target/ragleap-ops-$PY_VERSION.jar"
SOURCES_JAR="$JAVA_PROJECT/target/ragleap-ops-$PY_VERSION-sources.jar"
JAVADOC_JAR="$JAVA_PROJECT/target/ragleap-ops-$PY_VERSION-javadoc.jar"
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

WHEEL_PAYLOAD="$TMP/wheel/ragleap_ops"
JAR_PAYLOAD="$TMP/jar/ragleap_ops"
for tree in helm k8s; do
  [[ -d "$WHEEL_PAYLOAD/$tree" ]] || {
    echo "ERROR: Python wheel is missing $tree payload" >&2
    exit 1
  }
  [[ -d "$JAR_PAYLOAD/$tree" ]] || {
    echo "ERROR: Java JAR is missing $tree payload" >&2
    exit 1
  }
done

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
echo "=== Positive parity: Helm and Kubernetes file lists and contents ==="
compare_payloads "$WHEEL_PAYLOAD/helm" "$JAR_PAYLOAD/helm"
compare_payloads "$WHEEL_PAYLOAD/k8s" "$JAR_PAYLOAD/k8s"
echo "PASS: JAR Helm and Kubernetes payloads match the Python wheel."

echo
echo "=== Break proof 1: removing Helm Chart.yaml must fail parity ==="
mkdir -p "$TMP/break-missing"
cp -a "$WHEEL_PAYLOAD" "$TMP/break-missing/ragleap_ops"
rm -f "$TMP/break-missing/ragleap_ops/helm/ragleap-ops/Chart.yaml"
if compare_payloads "$TMP/break-missing/ragleap_ops/helm" "$JAR_PAYLOAD/helm" >"$TMP/missing-proof.log" 2>&1; then
  echo "ERROR: missing-file break proof unexpectedly passed" >&2
  exit 1
else
  echo "PASS: parity rejected a missing Helm Chart.yaml."
fi

echo
echo "=== Break proof 2: modifying a Kubernetes manifest must fail parity ==="
mkdir -p "$TMP/break-content"
cp -a "$WHEEL_PAYLOAD" "$TMP/break-content/ragleap_ops"
printf '\n# deliberate parity break-proof mutation\n' \
  >> "$TMP/break-content/ragleap_ops/k8s/namespace.yaml"
if compare_payloads "$TMP/break-content/ragleap_ops/k8s" "$JAR_PAYLOAD/k8s" >"$TMP/content-proof.log" 2>&1; then
  echo "ERROR: modified-content break proof unexpectedly passed" >&2
  exit 1
else
  echo "PASS: parity rejected altered namespace.yaml content."
fi

echo
echo "PASS: Java ops parity and both break-proof checks completed."
echo "No package was published; no Helm release or Kubernetes cluster was touched."
