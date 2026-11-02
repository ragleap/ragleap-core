#!/usr/bin/env bash
# RagLeap Core installer: checks Docker, downloads the code, creates .env with fresh secrets,
# starts everything and prints a one-click link to the dashboard.
#
#   curl -fsSL https://raw.githubusercontent.com/ragleap/ragleap-core/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/ragleap/ragleap-core/main/install.sh | bash -s -- --ollama
#
# Unattended: RAGLEAP_PROVIDER=gemini|ollama|skip  GEMINI_API_KEY=...  RAGLEAP_DIR=...  RAGLEAP_NONINTERACTIVE=1
# An existing .env is never modified.
# Offline (no internet): install.sh --offline /path/ragleap-offline-....tar, a bundle made by scripts/make_offline_bundle.sh.
# Safety checks (free disk, another setup under the same project name) can be skipped with RAGLEAP_SKIP_CHECKS=1.
set -euo pipefail

REPO_URL="${RAGLEAP_REPO:-https://github.com/ragleap/ragleap-core.git}"
DIR="${RAGLEAP_DIR:-ragleap-core}"
PROVIDER="${RAGLEAP_PROVIDER:-}"
CHAT_MODEL="${RAGLEAP_OLLAMA_MODEL:-qwen2.5:3b}"
EMBED_MODEL="nomic-embed-text"
WAIT_TRIES="${RAGLEAP_WAIT_TRIES:-90}"
OFFLINE="${RAGLEAP_OFFLINE_BUNDLE:-}"
BASE_URL="http://localhost:8000"
MIN_FREE_GB="${RAGLEAP_MIN_FREE_GB:-4}"
MIN_FREE_GB_OLLAMA="${RAGLEAP_MIN_FREE_GB_OLLAMA:-14}"
case "$(uname -s 2>/dev/null || echo unknown)" in Darwin|MINGW*|MSYS*|CYGWIN*) DESKTOP_OS=1 ;; *) DESKTOP_OS=0 ;; esac

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

# --- preflight helpers ---
free_gb() {
  # whole GB free on the disk that holds $1 (or its nearest existing parent folder)
  local p="$1" kb
  while [ ! -d "$p" ] && [ "$p" != "/" ] && [ "$p" != "." ]; do p="$(dirname "$p")"; done
  kb="$(df -Pk "$p" 2>/dev/null | awk 'NR==2 {print $4}')"
  case "$kb" in ''|*[!0-9]*) echo ""; return 0 ;; esac
  echo $((kb / 1024 / 1024))
}
docker_free_gb() {
  # on Linux the images live under the Docker root folder, which can be on another disk
  local root="" r=""
  if [ "$(uname -s)" = "Linux" ]; then root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"; fi
  if [ -n "$root" ] && [ -d "$root" ]; then r="$(free_gb "$root")"; fi
  if [ -z "$r" ]; then r="$(free_gb "$1")"; fi
  echo "$r"
}
check_disk() {
  # $1 = GB needed, $2 = what for
  [ "${RAGLEAP_SKIP_CHECKS:-}" = "1" ] && return 0
  local have
  have="$(docker_free_gb "$DIR")"
  if [ -z "$have" ]; then say "Could not measure free disk space, continuing."; return 0; fi
  if [ "$have" -lt "$1" ]; then
    die "Not enough free disk space: $have GB free, about $1 GB needed $2. Free some space and run this again (skip this check with RAGLEAP_SKIP_CHECKS=1)."
  fi
  say "Disk space OK: $have GB free."
}
host_ollama_up() { curl -sf --max-time 3 "http://localhost:11434/api/tags" >/dev/null 2>&1; }
project_name() {
  local n="${COMPOSE_PROJECT_NAME:-}"
  if [ -z "$n" ] && [ -f .env ]; then n="$(grep '^COMPOSE_PROJECT_NAME=' .env | head -1 | cut -d= -f2- | tr -d '\r\n"' || true)"; fi
  if [ -z "$n" ] && [ -f docker-compose.yml ]; then n="$(sed -n 's/^name:[[:space:]]*//p' docker-compose.yml | head -1 | tr -d '\r\n"' || true)"; fi
  if [ -z "$n" ]; then n="$(basename "$PWD" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')"; fi
  echo "$n"
}
norm_path() { printf '%s' "$1" | tr '\\' '/' | tr 'A-Z' 'a-z'; }
check_project_conflict() {
  [ "${RAGLEAP_SKIP_CHECKS:-}" = "1" ] && return 0
  local pn mine list d other=""
  pn="$(project_name)"
  [ -n "$pn" ] || return 0
  mine="$PWD"
  if pwd -W >/dev/null 2>&1; then mine="$(pwd -W)"; fi
  list="$(docker ps -a --filter "label=com.docker.compose.project=$pn" --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | sort -u || true)"
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    if [ "$(norm_path "$d")" != "$(norm_path "$mine")" ]; then other="$d"; fi
  done <<< "$list"
  if [ -n "$other" ]; then
    die "Another RagLeap setup already exists under the project name '$pn' (folder: $other). Starting a second one would mix up the two. Stop or remove it first, or install into a new folder with a different COMPOSE_PROJECT_NAME (skip this check with RAGLEAP_SKIP_CHECKS=1)."
  fi
}
port_busy() {
  # true when something on this computer already listens on this port
  # (RAGLEAP_PORT_CHECK=off makes every port count as free; the tests use it so results do not depend on the machine)
  [ "${RAGLEAP_PORT_CHECK:-}" = "off" ] && return 1
  ( exec 3<>"/dev/tcp/127.0.0.1/$1" ) 2>/dev/null
}
pick_port() {
  # first free port at or above $1 (the six default ports are far apart, so picks never collide)
  local p="$1" i=0
  while port_busy "$p" && [ "$i" -lt 50 ]; do p=$((p + 1)); i=$((i + 1)); done
  echo "$p"
}
# --- end preflight helpers ---
# --- browser helper ---
open_browser() {
  # opens a URL in the default browser; returns 1 when that is not possible (servers, CI)
  [ "${RAGLEAP_NO_BROWSER:-}" = "1" ] && return 1
  [ -n "${CI:-}" ] && return 1
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*)
      command -v powershell.exe >/dev/null 2>&1 || return 1
      MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' powershell.exe -NoProfile -Command "Start-Process '$1'" >/dev/null 2>&1 ;;
    Darwin)
      command -v open >/dev/null 2>&1 || return 1
      open "$1" >/dev/null 2>&1 ;;
    *)
      [ -n "${SSH_CONNECTION:-}" ] && return 1
      { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } || return 1
      command -v xdg-open >/dev/null 2>&1 || return 1
      xdg-open "$1" >/dev/null 2>&1 ;;
  esac
}
# --- end browser helper ---

while [ $# -gt 0 ]; do
  case "$1" in
    --ollama) PROVIDER=ollama ;;
    --gemini) PROVIDER=gemini ;;
    --skip-ai) PROVIDER=skip ;;
    --offline) OFFLINE="${2:-}"; [ -n "$OFFLINE" ] || { printf '%s\n' "--offline needs the path of the bundle (.tar)" >&2; exit 2; }; shift ;;
    -h|--help)
      say "Usage: install.sh [--gemini | --ollama | --skip-ai] [--offline BUNDLE.tar]"
      say "Unattended: RAGLEAP_PROVIDER=gemini|ollama|skip GEMINI_API_KEY=... RAGLEAP_DIR=... RAGLEAP_NONINTERACTIVE=1"
      exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

# --- offline mode (install.sh --offline BUNDLE.tar) ---
sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }
OB=""
offline_prepare() {
  [ -f "$OFFLINE" ] || die "Bundle not found: $OFFLINE"
  [ -f "$OFFLINE.sha256" ] || die "The checksum file $OFFLINE.sha256 must be next to the bundle."
  local want got names bad barch march
  want="$(awk '{print $1}' "$OFFLINE.sha256" | head -1)"; got="$(sha256 "$OFFLINE" | awk '{print $1}')"
  [ -n "$want" ] && [ "$want" = "$got" ] || die "The bundle does not match its checksum. Copy it again."
  names="$(tar -tf "$OFFLINE")" || die "The bundle cannot be read as a tar archive."
  bad="$(printf '%s\n' "$names" | grep -Ec '(^/|(^|/)\.\.(/|$))' || true)"
  [ "$bad" = "0" ] || die "The bundle has unsafe file names. It was not unpacked."
  OFFLINE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/ragleap-offline.XXXXXX")"
  trap 'rm -rf "$OFFLINE_TMP"' EXIT
  tar -xf "$OFFLINE" -C "$OFFLINE_TMP"
  OB="$(find "$OFFLINE_TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
  { [ -n "$OB" ] && [ -f "$OB/MANIFEST.txt" ] && [ -f "$OB/SHA256SUMS" ]; } || die "This does not look like a RagLeap offline bundle."
  ( cd "$OB" && sha256 -c SHA256SUMS >/dev/null 2>&1 ) || die "A file inside the bundle does not match its checksum."
  barch="$(sed -n 's/^architecture: //p' "$OB/MANIFEST.txt" | head -1)"
  case "$(uname -m)" in x86_64|amd64) march=amd64 ;; aarch64|arm64) march=arm64 ;; *) march="$(uname -m)" ;; esac
  [ "$barch" = "$march" ] || die "This bundle is for $barch computers; this computer is $march."
  say "Bundle checked: version $(sed -n 's/^version: //p' "$OB/MANIFEST.txt" | head -1), $barch."
}
offline_load_images() {
  say "Loading the Docker images from the bundle (this can take several minutes)..."
  gunzip -c "$OB/images.tar.gz" | docker load >/dev/null || die "Loading the images failed."
}
offline_has_models() { [ -f "$OB/models.tar.gz" ]; }
offline_load_models() {
  offline_has_models || die "This bundle has no models. Build it with --with-models \"$CHAT_MODEL,$EMBED_MODEL\", or install with --skip-ai."
  local line vol want
  line="$(sed -n 's/^models: //p' "$OB/MANIFEST.txt" | head -1 | tr -d ' ')"
  for want in "$CHAT_MODEL" "$EMBED_MODEL"; do
    case ",$line," in
      *",$want,"*) ;;
      *) die "The bundle does not contain the model $want (it has: $line). Set RAGLEAP_OLLAMA_MODEL for the chat model, or build the bundle with the models you need." ;;
    esac
  done
  vol="$(project_name)_ragleap_core_ollama"
  docker volume create --label "com.docker.compose.project=$(project_name)" --label com.docker.compose.volume=ragleap_core_ollama "$vol" >/dev/null
  docker run --rm -v "$vol:/root/.ollama" -v "$OB:/in:ro" --entrypoint tar ollama/ollama:latest -xzf /in/models.tar.gz -C /root/.ollama || die "Loading the models failed."
  say "Local models loaded from the bundle."
}
offline_check_models() {
  local out want
  out="$($COMPOSE $PROFILE exec -T ollama ollama list 2>/dev/null || true)"
  for want in "$@"; do
    case "$out" in *"$want"*) ;; *) die "The model $want is not in the Ollama volume, so the models were not loaded correctly." ;; esac
  done
  say "Local models are ready (from the bundle)."
}
# --- end offline mode ---

say "RagLeap Core installer"

if ! command -v docker >/dev/null 2>&1; then
  if [ "$(uname -s 2>/dev/null)" = "Linux" ]; then
    die "Docker is not installed. Install Docker Engine first: https://docs.docker.com/engine/install/ then run this again."
  else
    die "Docker is not installed. Install Docker Desktop first: https://www.docker.com/products/docker-desktop . On Windows it needs WSL 2 and CPU virtualization turned on."
  fi
fi
if ! docker info >/dev/null 2>&1; then
  if [ "$(uname -s 2>/dev/null)" = "Linux" ]; then
    die "Docker is installed but not running, or this user cannot reach it. Start it (for example: sudo systemctl start docker) and make sure your user is in the docker group, then run this again."
  else
    die "Docker is installed but not running. Start Docker Desktop, wait until it says it is running, then run this again. If it never starts, check that virtualization is enabled and that WSL 2 is installed."
  fi
fi
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "Docker Compose was not found. Install Docker Desktop, or the docker compose plugin."
fi

command -v curl >/dev/null 2>&1 || die "curl is not installed (the installer uses it to wait for RagLeap to start). Install curl and run this again."
check_disk "$MIN_FREE_GB" "to download and build RagLeap"
if [ -n "$OFFLINE" ]; then offline_prepare; offline_load_images; fi

if [ -f "$DIR/docker-compose.yml" ]; then
  say "Using the existing folder: $DIR"
elif [ -n "$OFFLINE" ]; then
  mkdir -p "$DIR"
  tar -xzf "$OB/repo.tar.gz" -C "$DIR" || die "Unpacking the code from the bundle failed."
  say "Unpacked the code from the bundle into $DIR"
else
  command -v git >/dev/null 2>&1 || die "Git is not installed (it is needed to download RagLeap)."
  git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"
if [ -n "$OFFLINE" ]; then export COMPOSE_PROJECT_NAME=ragleap-core; fi   # the bundled images are named for this project
check_project_conflict

interactive() {
  [ "${RAGLEAP_NONINTERACTIVE:-}" != "1" ] && [ -r /dev/tty ] && [ -w /dev/tty ] && ( : </dev/tty ) 2>/dev/null
}
ask() {
  local a
  printf '%s' "$1" >/dev/tty
  IFS= read -r a </dev/tty || a=""
  printf '%s' "${a:-$2}"
}
ask_secret() {
  local a
  printf '%s' "$1" >/dev/tty
  IFS= read -rs a </dev/tty || a=""
  printf '\n' >/dev/tty
  printf '%s' "$a"
}
rand_hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }
fernet_key() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; }
set_env() {
  K="$1" V="$2" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"] }
    index($0, k "=") == 1 { print k "=" v; d = 1; next }
    { print }
    END { if (!d) print k "=" v }' .env > .env.tmp && mv .env.tmp .env
}
env_value() { grep "^$1=" .env | head -1 | cut -d= -f2- | tr -d '\r\n' || true; }

USE_OLLAMA=0
HOST_OLLAMA=0
DASH_LOGIN=""
if [ -f .env ]; then
  say "Keeping your existing .env (it is not changed). Change the AI provider any time in the dashboard: Settings tab."
  if [ -n "$PROVIDER" ]; then say "(The AI choice you passed is ignored because .env already exists.)"; fi
  DASH_LOGIN="$(env_value RAGLEAP_API_KEY)"
  if [ -z "$DASH_LOGIN" ]; then say "WARNING: your .env has no RAGLEAP_API_KEY, so the API has no password. Set one."; fi
  if grep -q '^OLLAMA_BASE_URL=http://ollama:' .env; then USE_OLLAMA=1; fi
else
  [ -f .env.example ] || die ".env.example is missing in $DIR."
  if [ -n "$OFFLINE" ]; then
    [ "$PROVIDER" != gemini ] || die "Gemini needs the internet. For an offline install use --ollama (local models) or --skip-ai."
    if [ -z "$PROVIDER" ]; then
      if offline_has_models; then PROVIDER=ollama; else PROVIDER=skip; fi
      say "Offline install: AI provider set to $PROVIDER."
    fi
  fi
  if [ -z "$PROVIDER" ]; then
    if interactive; then
      {
        echo
        echo "Which AI do you want to use?"
        echo "  1) Google Gemini  (free key, best quality, needs internet)"
        echo "  2) Ollama         (runs on this computer, no key, slower and less accurate on a CPU)"
        echo "  3) Skip           (choose later in the dashboard: Settings tab)"
      } >/dev/tty
      case "$(ask 'Choose 1, 2 or 3 [1]: ' 1)" in
        2) PROVIDER=ollama ;;
        3) PROVIDER=skip ;;
        *) PROVIDER=gemini ;;
      esac
    else
      PROVIDER=skip
      say "No AI provider chosen (non-interactive). Pick one later in the dashboard: Settings tab."
    fi
  fi
  case "$PROVIDER" in gemini|ollama|skip) ;; *) die "RAGLEAP_PROVIDER must be gemini, ollama or skip." ;; esac
  if [ "$PROVIDER" = ollama ]; then
    if [ "$DESKTOP_OS" = 1 ] && host_ollama_up; then
      HOST_OLLAMA=1
      say "Found the Ollama app on this computer: RagLeap will use it (no 9 GB Ollama container needed)."
    else
      say "Local AI will run in a bundled Ollama container. It needs about 12 GB of disk."
      check_disk "$MIN_FREE_GB_OLLAMA" "for the bundled local AI (about 12 GB)"
    fi
  fi

  GEMINI_KEY="${GEMINI_API_KEY:-}"
  if [ "$PROVIDER" = gemini ] && [ -z "$GEMINI_KEY" ] && interactive; then
    printf '%s\n' "Get a free Gemini key at https://aistudio.google.com/apikey" >/dev/tty
    GEMINI_KEY="$(ask_secret 'Paste your Gemini API key (hidden; press Enter to skip): ')"
  fi
  if [ "$PROVIDER" = gemini ] && [ -z "$GEMINI_KEY" ]; then
    say "No Gemini key given: add it later in the dashboard (Settings tab)."
  fi

  OLD_UMASK="$(umask)"
  umask 077
  cp .env.example .env
  DASH_LOGIN="$(rand_hex 24)"
  set_env RAGLEAP_API_KEY "$DASH_LOGIN"
  set_env ADDON_ENCRYPTION_KEY "$(fernet_key)"
  set_env SANDBOX_TOKEN "$(rand_hex 24)"
  set_env VOICE_WS_TOKEN "$(rand_hex 24)"
  if [ -n "$OFFLINE" ]; then set_env COMPOSE_PROJECT_NAME ragleap-core; fi
  for pv in APP_PORT:8000 DB_PORT:5433 VOICE_PORT:8766 NEO4J_HTTP_PORT:7475 NEO4J_BOLT_PORT:7688 REDIS_PORT:6380; do
    pn="${pv%%:*}"; pd="${pv##*:}"; pp="$(pick_port "$pd")"
    if [ "$pp" != "$pd" ]; then
      say "Port $pd is already in use on this computer, so RagLeap will use $pp instead ($pn)."
      set_env "$pn" "$pp"
    fi
  done
  if docker volume ls -q 2>/dev/null | grep -qx "$(project_name)_ragleap_core_data"; then
    say "An earlier database for this setup exists, so its database password is left as it was."
  else
    set_env POSTGRES_PASSWORD "$(rand_hex 12)"
  fi
  case "$PROVIDER" in
    gemini)
      set_env GEMINI_API_KEY "$GEMINI_KEY" ;;
    ollama)
      if [ "$HOST_OLLAMA" = 1 ]; then OLLAMA_URL_ENV="http://host.docker.internal:11434/v1"; else USE_OLLAMA=1; OLLAMA_URL_ENV="http://ollama:11434/v1"; fi
      set_env GEMINI_API_KEY ""
      set_env LLM_PROVIDER ollama
      set_env OLLAMA_MODEL "$CHAT_MODEL"
      set_env OLLAMA_BASE_URL "$OLLAMA_URL_ENV"
      set_env EMBEDDING_PROVIDER ollama
      set_env OLLAMA_EMBEDDING_MODEL "$EMBED_MODEL"
      set_env EMBEDDING_DIMENSIONS 768 ;;
    skip)
      set_env GEMINI_API_KEY "" ;;
  esac
  chmod 600 .env
  umask "$OLD_UMASK"
  say "Created .env with a fresh API key and secrets."
fi

AP="$(env_value APP_PORT)"
case "$AP" in ''|*[!0-9]*) ;; *) BASE_URL="http://localhost:$AP" ;; esac

PROFILE=""
if [ "$USE_OLLAMA" = 1 ]; then PROFILE="--profile ollama"; fi
UPOFF=""; UPFLAGS="--build -d"
if [ -n "$OFFLINE" ]; then UPOFF="--pull never"; UPFLAGS="-d --no-build --pull never"; fi

if [ "$USE_OLLAMA" = 1 ]; then
  say "Starting the local Ollama service first (the models must be ready before RagLeap starts)..."
  if [ -n "$OFFLINE" ]; then offline_load_models; fi
  $COMPOSE $PROFILE up -d $UPOFF ollama
  say "Waiting for the local Ollama service..."
  n=0
  until $COMPOSE $PROFILE exec -T ollama ollama list >/dev/null 2>&1; do
    n=$((n + 1))
    if [ "$n" -ge 60 ]; then die "The Ollama service did not start. Check: $COMPOSE $PROFILE logs ollama"; fi
    sleep 2
  done
  m="$(env_value OLLAMA_MODEL)"; m="${m:-$CHAT_MODEL}"
  e="$(env_value OLLAMA_EMBEDDING_MODEL)"; e="${e:-$EMBED_MODEL}"
  if [ -n "$OFFLINE" ]; then offline_check_models "$m" "$e"; else
  say "Downloading local models (a few GB, once). Small models on a CPU answer slowly and less accurately than Gemini."; fi
  [ -n "$OFFLINE" ] || $COMPOSE $PROFILE exec -T ollama ollama pull "$m" || say "Could not download $m. Retry: $COMPOSE $PROFILE exec ollama ollama pull $m"
  [ -n "$OFFLINE" ] || $COMPOSE $PROFILE exec -T ollama ollama pull "$e" || say "Could not download $e. Retry: $COMPOSE $PROFILE exec ollama ollama pull $e"
fi

if [ "$HOST_OLLAMA" = 1 ]; then
  m="$(env_value OLLAMA_MODEL)"; m="${m:-$CHAT_MODEL}"
  e="$(env_value OLLAMA_EMBEDDING_MODEL)"; e="${e:-$EMBED_MODEL}"
  say "Downloading local models into your Ollama app (a few GB, once)..."
  for mm in "$m" "$e"; do
    if command -v ollama >/dev/null 2>&1; then
      ollama pull "$mm" || say "Could not download $mm. Retry in a terminal: ollama pull $mm"
    else
      curl -sf -X POST "http://localhost:11434/api/pull" -d "{\"model\":\"$mm\",\"stream\":false}" >/dev/null || say "Could not download $mm. Retry in a terminal: ollama pull $mm"
    fi
  done
  say "Tip: in the Ollama app settings, set the context length to 8192 so long document prompts are not cut off."
fi

say "Building and starting RagLeap (the first run takes a few minutes)..."
if ! $COMPOSE $PROFILE up $UPFLAGS; then
  say ""
  say "RagLeap could not start. If the message above says a port is already in use, set another one in .env (for example DB_PORT=5434 or APP_PORT=8001) and run this again."
  exit 1
fi

n=0
until curl -sf "$BASE_URL/health" >/dev/null 2>&1; do
  n=$((n + 1))
  if [ "$n" -ge "$WAIT_TRIES" ]; then
    say "RagLeap did not become healthy in time. Check: $COMPOSE logs app"
    exit 1
  fi
  sleep 2
done

say ""
say "RagLeap is running."
if [ -n "$DASH_LOGIN" ]; then
  if open_browser "$BASE_URL/office#key=$DASH_LOGIN"; then
    say "Opened your dashboard in your browser. Your login key is saved in $PWD/.env"
    say "To show the login link again, run: ragleap key"
  else
    say "Open your dashboard (one click; the key stays in your browser tab):"
    say "  $BASE_URL/office#key=$DASH_LOGIN"
    say "Keep that link private: it contains your API key. The key is also in $PWD/.env"
  fi
else
  say "Open your dashboard: $BASE_URL/office"
fi
say "API reference: $BASE_URL/docs (it needs your key in the x-api-key header, so it shows an error page until you send one)"


SHIM_DIR="${HOME:-.}/.local/bin"
if mkdir -p "$SHIM_DIR" 2>/dev/null && printf '#!/bin/sh\nRAGLEAP_HOME=%s exec bash %s/scripts/ragleap "$@"\n' "$(printf '%q' "$PWD")" "$(printf '%q' "$PWD")" > "$SHIM_DIR/ragleap" && chmod 755 "$SHIM_DIR/ragleap"; then
  say "Installed the ragleap command: $SHIM_DIR/ragleap (ragleap launch | stop | status | logs | update | key)"
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*)
      say "On Windows, run RagLeap commands in Git Bash (Start menu), from this folder: bash scripts/ragleap key   (also: launch | stop | status | logs | update)" ;;
    *)
      case ":$PATH:" in *":$SHIM_DIR:"*) ;; *) say "Add it to your PATH to use it from anywhere: export PATH=\"$SHIM_DIR:\$PATH\"" ;; esac ;;
  esac
else
  say "Could not install the ragleap command (optional). You can run: bash $PWD/scripts/ragleap"
fi
