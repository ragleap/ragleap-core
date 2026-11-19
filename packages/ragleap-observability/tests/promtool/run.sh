#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
chart="$here/../../helm/ragleap-observability"
img=prom/prometheus:v3.16.0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
helm template t "$chart" --set slo.enabled=true -s templates/prometheus-rules-configmap.yaml \
  | awk '/^  alerts.yml: \|/{f=1;next} f&&/^    /{sub(/^    /,"");print;next} f{exit}' > "$tmp/alerts.yml"
cp "$here/slo_test.yml" "$tmp/"
chmod -R a+rX "$tmp"
docker run --rm -v "$tmp":/t --entrypoint promtool "$img" check rules /t/alerts.yml
docker run --rm -v "$tmp":/t --entrypoint promtool "$img" test rules /t/slo_test.yml
