#!/usr/bin/env bash
# Runs the ragleap-ansible roles on a REAL host: the base role twice (privilege escalation through sudo), then the opt-in
# SSH hardening twice (with the real reload handler), then a real key login against the hardened sshd.
# It CHANGES the machine it runs on (creates a user, writes sudoers and sshd configuration), so it refuses to run
# anywhere except a GitHub-hosted runner, which is a disposable VM. There is deliberately no override.
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
  echo "Not exercised: the firewall on a real host (ufw enforcement), reboot persistence, unattended upgrades actually running,"
  echo "a drop-in order different from the cloud image default, and any host that is not a GitHub-hosted runner."
  if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; exit 0; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi
}

if [ "${GITHUB_ACTIONS:-}" != "true" ] || [ "${RUNNER_ENVIRONMENT:-}" != "github-hosted" ]; then
  stop "this test changes sshd and users on the machine it runs on, so it only runs on a GitHub-hosted runner (there is no override)"
fi
sudo -n true 2>/dev/null || stop "passwordless sudo is required"
[ -f "$PKG/playbooks/bootstrap.yml" ] || stop "bootstrap.yml not found at $PKG"

echo "== ansible-core in a virtualenv (the pin the container test uses)"
python3 -m venv /tmp/av || stop "python3 -m venv failed"
/tmp/av/bin/pip install -q "ansible-core>=2.16,<2.17"
export PATH=/tmp/av/bin:$PATH
record "OBSERVED  $(ansible --version | head -1); system python $(python3 --version 2>&1)"
record "OBSERVED  os: $(. /etc/os-release; echo "$PRETTY_NAME"); systemd state: $(systemctl is-system-running 2>&1 || true)"

rm -f /tmp/av-key /tmp/av-key.pub
ssh-keygen -q -t ed25519 -N '' -C ci-test -f /tmp/av-key
PUB=$(cat /tmp/av-key.pub)
printf '{"ragleap_base_deploy_ssh_public_key": "%s"}\n' "$PUB" > /tmp/av-base.json
printf '{"ragleap_base_deploy_ssh_public_key": "%s", "ragleap_base_harden_ssh": true, "ragleap_base_manage_services": false}\n' "$PUB" > /tmp/av-ssh.json
printf '[ragleap_servers]\nlocalhost ansible_connection=local ansible_python_interpreter=/usr/bin/python3\n' > /tmp/av-inv.ini

play() { ( cd "$PKG" && ansible-playbook -i /tmp/av-inv.ini playbooks/bootstrap.yml -e "@$1" ) > "$2" 2>&1; }
recap() { grep -E '^localhost +:' "$1" | tail -1 | tr -s ' ' || true; }
twice() { # label varsfile
  if play "$2" /tmp/run1.out; then record "PASS  $1: run 1 succeeded ($(recap /tmp/run1.out))"; else tail -40 /tmp/run1.out; fail "$1: run 1 failed"; finish; fi
  if play "$2" /tmp/run2.out && grep -q 'changed=0' /tmp/run2.out && grep -q 'failed=0' /tmp/run2.out; then
    record "PASS  $1: run 2 changed nothing ($(recap /tmp/run2.out))"
  else
    tail -40 /tmp/run2.out; fail "$1: run 2 failed or changed something"
  fi
}

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
methods() { # which authentication methods the RUNNING sshd offers (no credentials are sent)
  local out
  out=$(ssh "${SSH_OPTS[@]}" -o PreferredAuthentications=none ragleap@127.0.0.1 true 2>&1 || true)
  grep -o 'Permission denied ([a-z,-]*)' <<< "$out" | head -1 || echo "no answer: $(head -c 120 <<< "$out")"
}
key_login() { ssh "${SSH_OPTS[@]}" -i /tmp/av-key -o IdentitiesOnly=yes ragleap@127.0.0.1 'id -un' 2>&1 | tail -1 || true; }

echo "== phase 1: the base role on a real host (privilege escalation through sudo), run twice"
twice "base role" /tmp/av-base.json
if id ragleap >/dev/null 2>&1; then record "PASS  the deploy user ragleap exists"; else fail "the deploy user was not created"; fi
if sudo grep -q -F "$PUB" /home/ragleap/.ssh/authorized_keys 2>/dev/null; then record "PASS  the deploy key is in authorized_keys"; else fail "the deploy key is not in authorized_keys"; fi
if sudo visudo -cf /etc/sudoers.d/90-ragleap >/dev/null 2>&1; then record "PASS  the sudoers file is valid"; else fail "the sudoers file is missing or invalid"; fi
if sudo -u ragleap sudo -n true 2>/dev/null; then record "PASS  the deploy user has passwordless sudo"; else fail "the deploy user cannot sudo without a password"; fi

echo "== baseline before hardening"
record "OBSERVED  ssh.service: $(systemctl is-active ssh 2>&1 || true); ssh.socket: $(systemctl is-active ssh.socket 2>&1 || true); /run/sshd: $(ls -ld /run/sshd 2>&1 | cut -c1-70)"
BEFORE=$(methods)
record "OBSERVED  the running sshd offers before hardening: $BEFORE"
LOGIN=$(key_login)
if [ "$LOGIN" = "ragleap" ]; then record "PASS  key login as ragleap works before hardening"; else fail "key login before hardening gave: $LOGIN"; fi

echo "== phase 2: opt-in SSH hardening on a real host, run twice"
twice "ssh hardening" /tmp/av-ssh.json
EFFECTIVE=$(sudo sshd -T 2>&1 || true)
record "OBSERVED  sshd -T after hardening: $(grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication) ' <<< "$EFFECTIVE" | tr '\n' ';')"
if grep -q '^passwordauthentication no' <<< "$EFFECTIVE"; then record "PASS  sshd -T says password authentication is off"; else fail "sshd -T still has password authentication on"; fi
if grep -q '^kbdinteractiveauthentication no' <<< "$EFFECTIVE"; then record "PASS  sshd -T says keyboard-interactive is off"; else fail "sshd -T still has keyboard-interactive on"; fi
AFTER=$(methods)
record "OBSERVED  the running sshd offers after hardening: $AFTER"
if [ "$AFTER" = "Permission denied (publickey)" ]; then
  record "PASS  the running sshd now offers only publickey (before: $BEFORE)"
else
  fail "the running sshd offers '$AFTER' after hardening (want exactly 'Permission denied (publickey)')"
fi
if [ "$BEFORE" = "$AFTER" ]; then record "NOTE  the baseline already offered the same methods, so this run cannot show that the role changed the live daemon"; fi
LOGIN=$(key_login)
if [ "$LOGIN" = "ragleap" ]; then record "PASS  key login as ragleap still works against the hardened sshd"; else fail "key login against the hardened sshd gave: $LOGIN"; fi
record "OBSERVED  after hardening: ssh.service $(systemctl is-active ssh 2>&1 || true), ssh.socket $(systemctl is-active ssh.socket 2>&1 || true)"
finish
