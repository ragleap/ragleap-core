#!/usr/bin/env bash
# DIAGNOSTIC EXPERIMENT, not an assertion test: can the ragleap-ops chart's app and voice pods pull the
# ghcr.io/ragleap/ragleap-app image with the workflow token, start, and become Ready on a throwaway kind cluster?
# It prints what happened and a final VERDICT line. It exits 0 whatever the verdict (setup problems exit 1),
# so a green job does NOT mean the pods work: read the VERDICT.
# Meant for a GitHub runner. It refuses to run elsewhere unless FORCE=1.
# Env: GHCR_TOKEN (the workflow token, read:packages), GITHUB_ACTOR.
set -euo pipefail

CL=ops-app-pods
NS=ops-app-test
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OPS=$ROOT/packages/ragleap-ops/helm/ragleap-ops

stop() { echo "STOP: $*"; exit 1; }
if [ "${GITHUB_ACTIONS:-}" != "true" ] && [ "${FORCE:-}" != "1" ]; then
  stop "not on a GitHub runner. A kind cluster needs several GB of disk and memory. Set FORCE=1 to run anyway."
fi
[ -n "${GHCR_TOKEN:-}" ] || stop "GHCR_TOKEN is not set"
[ -n "${GITHUB_ACTOR:-}" ] || stop "GITHUB_ACTOR is not set"

cleanup() {
  kill $(jobs -p) 2>/dev/null || true
  kind delete cluster --name "$CL" >/dev/null 2>&1 || true
  echo "cleanup done (kind cluster $CL deleted)"
}
trap cleanup EXIT

echo "== preflight"
for c in kubectl helm docker python3; do command -v "$c" >/dev/null || stop "$c not found"; done
if ! command -v kind >/dev/null; then
  echo "kind not preinstalled; downloading v0.24.0"
  curl -sLo /tmp/kind https://github.com/kubernetes-sigs/kind/releases/download/v0.24.0/kind-linux-amd64
  chmod +x /tmp/kind
  export PATH=/tmp:$PATH
fi
CLUSTERS=$(kind get clusters 2>&1 || true)
if grep -q -x -e "$CL" <<< "$CLUSTERS"; then stop "kind cluster $CL already exists"; fi
IMG=$(helm template t "$OPS" | grep -m1 -o 'ghcr.io/[^" ]*ragleap-app:[^" ]*') || stop "could not read the app image from the chart"
echo "chart $(sed -n 's/^version: //p' "$OPS/Chart.yaml") uses image: $IMG"
IMG_ARGS=()
if [ -n "${APP_IMAGE:-}" ]; then
  case "$APP_IMAGE" in
    ghcr.io/ragleap/ragleap-app:*) ;;
    *) stop "APP_IMAGE must start with ghcr.io/ragleap/ragleap-app: (got '$APP_IMAGE')" ;;
  esac
  IMG="$APP_IMAGE"
  IMG_ARGS=(--set "app.image=$IMG" --set "voice.image=$IMG")
  echo "image overridden for this run: $IMG"
fi

echo "== can the workflow token read the image? (docker manifest inspect, no pull)"
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GITHUB_ACTOR" --password-stdin >/dev/null
if docker manifest inspect "$IMG" >/tmp/manifest.json 2>/tmp/manifest.err; then
  echo "PRECHECK: the token can read $IMG"
  python3 - <<'PY'
import json
d = json.load(open("/tmp/manifest.json"))
layers = d.get("layers") or []
total = sum(l.get("size", 0) for l in layers)
print("manifest lists %d layers, about %d MB compressed" % (len(layers), total // 1000000) if layers else "manifest is an index (multi-platform); layer sizes not summed")
PY
else
  echo "PRECHECK: DENIED or not found: $(head -c 300 /tmp/manifest.err)"
  echo "VERDICT: PULL_DENIED_AT_PRECHECK (the workflow token cannot read $IMG; the package's Actions access or visibility needs changing by an owner)"
  exit 0
fi

echo "== creating kind cluster"
kind create cluster --name "$CL" --wait 180s
kubectl create namespace "$NS"
# The pull secret is built from the Docker config that 'docker login' wrote above, so the token is never a command argument.
DOCKER_CFG="$HOME/.docker/config.json"
[ -f "$DOCKER_CFG" ] || stop "docker login did not write $DOCKER_CFG"
grep -q '"auth"' "$DOCKER_CFG" || stop "the Docker config has no inline auth (a credential helper is in use), so a pull secret cannot be built from it"
kubectl -n "$NS" create secret generic ghcr-pull-secret --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$DOCKER_CFG"
# The chart's app and voice pods read envFrom secret ragleap-app-env, which the chart does NOT create.
# A placeholder with no real keys is supplied so the pods can be created at all.
kubectl -n "$NS" create secret generic ragleap-app-env --from-literal=EXPERIMENT_PLACEHOLDER=1

echo "== installing the chart with app and voice at one replica (no --wait: we want to watch)"
helm install ops "$OPS" -n "$NS" ${IMG_ARGS[@]+"${IMG_ARGS[@]}"}

snapshot() { # prints one line per pod kind and the verdict token as the last line
  kubectl -n "$NS" get pods -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
res = {}
for p in d['items']:
    n = p['metadata']['name']
    key = 'app' if n.startswith('ragleap-app-') else 'voice' if n.startswith('ragleap-voice-') else None
    if not key:
        continue
    st = p['status']
    conds = {c['type']: c['status'] for c in st.get('conditions', [])}
    reasons = []
    for cs in (st.get('initContainerStatuses') or []) + (st.get('containerStatuses') or []):
        w = (cs.get('state') or {}).get('waiting')
        if w:
            reasons.append(w.get('reason', ''))
        t = (cs.get('lastState') or {}).get('terminated')
        if t:
            reasons.append('last:' + t.get('reason', '') + '/exit' + str(t.get('exitCode', '')))
    restarts = sum(cs.get('restartCount', 0) for cs in (st.get('containerStatuses') or []))
    res[key] = dict(pod=n, phase=st.get('phase'), ready=conds.get('Ready') == 'True', restarts=restarts, reasons=sorted(set(reasons)))
for key in ('app', 'voice'):
    r = res.get(key)
    print('%-5s %s' % (key, ('pod=%s phase=%s ready=%s restarts=%s reasons=%s' % (r['pod'], r['phase'], r['ready'], r['restarts'], ','.join(r['reasons']) or '-')) if r else 'no pod yet'))
allr = [x for r in res.values() for x in r['reasons']]
if len(res) == 2 and all(r['ready'] for r in res.values()):
    v = 'READY'
elif any(x in ('ImagePullBackOff', 'ErrImagePull') for x in allr):
    v = 'PULL_FAILED'
elif any(x == 'CreateContainerConfigError' for x in allr):
    v = 'CONFIG_ERROR'
elif any(r['restarts'] > 0 or 'CrashLoopBackOff' in r['reasons'] for r in res.values()):
    v = 'CRASHLOOP'
else:
    v = 'NOT_READY_YET'
print('TOKEN ' + v)
"
}

echo "== watching for up to 6 minutes"
VERDICT="NOT_READY_YET"; START=$SECONDS; BAD=0
while [ $((SECONDS - START)) -le 360 ]; do
  OUT=$(snapshot)
  VERDICT=$(tail -1 <<< "$OUT" | sed 's/^TOKEN //')
  echo "[$((SECONDS - START))s] $(head -2 <<< "$OUT" | tr '\n' ' ') -> $VERDICT"
  [ "$VERDICT" = "READY" ] && break
  case "$VERDICT" in PULL_FAILED|CONFIG_ERROR) BAD=$((BAD + 1)) ;; *) BAD=0 ;; esac
  [ "$BAD" -ge 6 ] && break
  sleep 15
done

echo "== library versions inside the running app container"
if [ "$VERDICT" = "READY" ]; then
  kubectl -n "$NS" exec deploy/ragleap-app -c app -- python -c "import importlib.metadata as m; print('IMAGE_LIBS: pillow=' + m.version('pillow') + ' pypdf=' + m.version('pypdf'))" 2>&1 | tail -3 || true
else
  echo "IMAGE_LIBS: not read (verdict is $VERDICT)"
fi
echo "== final state"
kubectl -n "$NS" get pods -o wide
echo "== events (last 25)"
kubectl -n "$NS" get events --sort-by=.lastTimestamp 2>&1 | tail -25
for d in ragleap-app ragleap-voice; do
  echo "== logs: $d (current, last 60 lines)"
  kubectl -n "$NS" logs "deploy/$d" -c "${d#ragleap-}" --tail=60 2>&1 | head -80 || true
  echo "== logs: $d (previous container, last 40 lines)"
  kubectl -n "$NS" logs "deploy/$d" -c "${d#ragleap-}" --previous --tail=40 2>&1 | head -60 || true
done
echo "== describe app pod (conditions and state)"
kubectl -n "$NS" describe pod -l app=ragleap-app 2>&1 | grep -E "State:|Reason:|Exit Code|Message:|Ready:|Warning|Error|runAsNonRoot|non-numeric" | head -20 || true

echo
echo "VERDICT: $VERDICT"
case "$VERDICT" in
  READY) echo "Both pods became Ready with placeholder configuration. That says the image pulls and the processes listen; it says nothing about application behaviour." ;;
  PULL_FAILED) echo "The cluster could not pull the image although the runner could read the manifest; check the pull secret and the package access." ;;
  CONFIG_ERROR) echo "A container could not be created. Read the events above (a missing Secret, or 'image has non-numeric user' with runAsNonRoot, are the usual causes)." ;;
  CRASHLOOP) echo "The container starts and exits. Read the previous-container logs above for the cause, usually missing configuration." ;;
  *) echo "Not ready within 6 minutes. Read the logs above." ;;
esac
exit 0
