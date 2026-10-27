#!/usr/bin/env bash
# Runs the ragleap-ansible FIREWALL role on a REAL host and tests enforcement from a container on the Docker bridge
# (traffic from a host to itself goes over loopback, which ufw always allows, so it would prove nothing).
# It CHANGES the machine it runs on (ufw), so it only runs on a GitHub-hosted runner, a disposable VM. No override.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
PKG=$ROOT/packages/ragleap-ansible
FAILS=0
RESULTS=()

stop() { echo "STOP: $*"; exit 1; }
record() { RESULTS+=("$1"); echo "$1"; }
fail() { FAILS=$((FAILS + 1)); record "FAIL  $1"; }
finish() {
  echo
  echo "== SUMMARY ($(date +%T))"
  for r in "${RESULTS[@]}"; do echo "$r"; done
  echo "Not exercised: reboot persistence, ports other than the two tested, IPv6 rules, Docker published ports, SSH hardening together with the firewall,"
  echo "and any host that is not a GitHub-hosted runner."
  if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; exit 0; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi
}
cleanup() { kill $(cat /tmp/l1.pid /tmp/l2.pid 2>/dev/null) 2>/dev/null || true; }
trap cleanup EXIT

if [ "${GITHUB_ACTIONS:-}" != "true" ] || [ "${RUNNER_ENVIRONMENT:-}" != "github-hosted" ]; then
  stop "this test turns on a firewall on the machine it runs on, so it only runs on a GitHub-hosted runner (there is no override)"
fi
sudo -n true 2>/dev/null || stop "passwordless sudo is required"
command -v docker >/dev/null || stop "docker is required for the enforcement check"
[ -f "$PKG/playbooks/bootstrap.yml" ] || stop "bootstrap.yml not found at $PKG"

python3 -m venv /tmp/av || stop "python3 -m venv failed"
/tmp/av/bin/pip install -q "ansible-core>=2.16,<2.17"
export PATH=/tmp/av/bin:$PATH
record "OBSERVED  $(ansible --version | head -1); os: $(. /etc/os-release; echo "$PRETTY_NAME")"
record "OBSERVED  before: ufw $(sudo ufw status 2>&1 | head -1); ssh.service $(systemctl is-active ssh 2>&1 || true), ssh.socket $(systemctl is-active ssh.socket 2>&1 || true); /run/sshd: $(ls -d /run/sshd 2>&1 | cut -c1-60)"

rm -f /tmp/av-key /tmp/av-key.pub
ssh-keygen -q -t ed25519 -N '' -C ci-test -f /tmp/av-key
PUB=$(cat /tmp/av-key.pub)
printf '{"ragleap_base_deploy_ssh_public_key": "%s", "ragleap_base_enable_firewall": true, "ragleap_base_firewall_allow_tcp": [8082]}\n' "$PUB" > /tmp/av-fw.json
printf '[ragleap_servers]\nlocalhost ansible_connection=local ansible_python_interpreter=/usr/bin/python3\n' > /tmp/av-inv.ini
play() { ( cd "$PKG" && ansible-playbook -i /tmp/av-inv.ini playbooks/bootstrap.yml -e "@$1" ) > "$2" 2>&1; }
recap() { grep -E '^localhost +:' "$1" | tail -1 | tr -s ' ' || true; }

GW=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)
[ -n "$GW" ] || stop "could not read the Docker bridge gateway address"
record "OBSERVED  docker bridge gateway: $GW"
docker pull -q alpine:3.20 >/dev/null || stop "could not pull alpine:3.20"
mkdir -p /tmp/www
( cd /tmp/www && nohup python3 -m http.server 8081 --bind 0.0.0.0 >/tmp/l1.log 2>&1 & echo $! > /tmp/l1.pid )
( cd /tmp/www && nohup python3 -m http.server 8082 --bind 0.0.0.0 >/tmp/l2.log 2>&1 & echo $! > /tmp/l2.pid )
sleep 2
reach() { docker run --rm alpine:3.20 wget -q -T 4 -O /dev/null "http://$GW:$1/" >/dev/null 2>&1 && echo yes || echo no; }

echo "== baseline: both ports must be reachable from a container before the firewall exists"
B1=$(reach 8081); B2=$(reach 8082)
record "OBSERVED  baseline from a container: 8081 reachable=$B1, 8082 reachable=$B2"
if [ "$B1" = yes ] && [ "$B2" = yes ]; then record "PASS  the baseline reaches both ports, so a later block means something"; else fail "the baseline does not reach both ports from a container, so the enforcement check would prove nothing"; finish; fi

echo "== the firewall role on a real host, run twice"
if play /tmp/av-fw.json /tmp/run1.out; then record "PASS  firewall: run 1 succeeded ($(recap /tmp/run1.out))"; else tail -40 /tmp/run1.out; fail "firewall: run 1 failed"; finish; fi
if play /tmp/av-fw.json /tmp/run2.out && grep -q 'changed=0' /tmp/run2.out && grep -q 'failed=0' /tmp/run2.out; then
  record "PASS  firewall: run 2 changed nothing ($(recap /tmp/run2.out))"
else
  tail -40 /tmp/run2.out; fail "firewall: run 2 failed or changed something"
fi

UFW=$(sudo ufw status verbose 2>&1 || true)
record "OBSERVED  ufw status verbose: $(tr '\n' ';' <<< "$UFW" | tr -s ' ' | cut -c1-300)"
if grep -q '^Status: active' <<< "$UFW"; then record "PASS  ufw is active"; else fail "ufw is not active"; fi
if grep -q 'deny (incoming), allow (outgoing)' <<< "$UFW"; then record "PASS  defaults are deny incoming, allow outgoing"; else fail "ufw defaults are not deny incoming / allow outgoing"; fi
for p in 22 8082; do
  if grep -E "^$p/tcp +ALLOW" <<< "$UFW" >/dev/null; then record "PASS  ufw allows $p/tcp"; else fail "ufw has no allow rule for $p/tcp"; fi
done

echo "== enforcement, from a container on the Docker bridge"
A2=$(reach 8082); A1=$(reach 8081)
record "OBSERVED  after: 8082 (allowed) reachable=$A2, 8081 (not allowed) reachable=$A1"
if [ "$A2" = yes ]; then record "PASS  the allowed port 8082 is still reachable"; else fail "the allowed port 8082 is not reachable after the firewall was enabled"; fi
if [ "$A1" = no ]; then record "PASS  the port that was not opened (8081) is blocked"; else fail "port 8081 is still reachable although the firewall should block it"; fi
LO=$(curl -s -m 4 -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/ 2>/dev/null || true)
record "OBSERVED  from the host itself over loopback, port 8081 answers: ${LO:-none} (loopback is always allowed by ufw, so this is not a test of the rules)"
finish
