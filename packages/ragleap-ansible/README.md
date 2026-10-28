# ragleap-ansible

Ansible roles that prepare a fresh Ubuntu 22.04 server for the RagLeap Helm charts. **Version 0.1.0 covers the server base only. It does not install k3s, Helm or the charts yet.**

## What works today

| Part | Status |
|---|---|
| `ragleap_base`: packages, deploy user with key-only login, passwordless sudo, unattended-upgrades | Tested in a throwaway Ubuntu 22.04 container, run twice, second run changes nothing. Also run twice on GitHub-hosted Ubuntu 22.04 and 24.04 runners (real systemd, privilege escalation through sudo): second run changed nothing (CI run 40378770112) |
| sshd hardening (opt-in `ragleap_base_harden_ssh`) | Effective settings checked with `sshd -T`, idempotent, bad config rolled back, all in a container. On GitHub-hosted Ubuntu 22.04 and 24.04 runners the running sshd offered `publickey,password` before hardening and only `publickey` after, and a key login as the deploy user still worked (CI run 40378770112). Weakening the role made that check fail: a drop-in that sorts after the cloud image's `50-cloud-init.conf` left password login on (run 40465088911), and skipping the sshd reload left the running daemon unchanged (run 40465114601) |
| firewall (opt-in `ragleap_base_enable_firewall`) | Rules and defaults checked in a container, idempotent, sshd port always opened. On GitHub-hosted Ubuntu 22.04 and 24.04 runners the role ran twice (the second run changed nothing), `ufw` came up active with deny-incoming and allow-outgoing and ports 22 and 8082 open, a port that was not opened was blocked from a container on the Docker bridge, and the job survived (CI run 40630611316). Before 0.1.2 the role failed on Ubuntu 24.04 when SSH hardening had not run (run 40466686432). Not tested: reboot persistence, IPv6, Docker published ports, SSH hardening together with the firewall |
| Helm, k3s, installing the charts | Not implemented yet |

## Usage

```bash
cp inventory.example.ini inventory.ini   # then list your server under [ragleap_servers]
echo 'ragleap_base_deploy_ssh_public_key: "ssh-ed25519 AAAA... you@host"' > vars.yml
ansible-playbook -i inventory.ini playbooks/bootstrap.yml -e @vars.yml
```

Pass the key through a vars file or JSON. A plain `-e name=value` splits at spaces and would cut the key short, which the role now rejects.

The playbook targets only the group `ragleap_servers`, never `localhost` by default.

## Hardening SSH (opt-in)

`ragleap_base_harden_ssh: true` turns off password logins and, by default, root logins without a key. It is off by default because a mistake can lock you out of the server.

- It needs `ragleap_base_deploy_ssh_public_key`. The role refuses to run without one.
- Keep one session open while you run it, and log in as the deploy user from a second terminal before closing the first.
- `ragleap_base_ssh_permit_root_login` defaults to `prohibit-password`. Set it to `no` only after switching `ansible_user` to the deploy user, or the next run cannot connect.
- The change is a drop-in file, `/etc/ssh/sshd_config.d/10-ragleap.conf`. The whole configuration is checked with `sshd -t` before sshd is reloaded, and if the check fails the file is removed again.

## Firewall (opt-in)

`ragleap_base_enable_firewall: true` installs ufw, denies incoming traffic by default, allows outgoing traffic, and opens the ports in `ragleap_base_firewall_allow_tcp` (for example `[80, 443]`).

- The ports sshd really listens on are always opened first, read from `sshd -T`, so the firewall cannot block your own login. Without sshd the role opens 22.
- Keep one session open while you run it, and test a new login before closing the first.
- k3s needs extra rules for its pod and service networks, which this version does not add.
- The role only adds rules. It never removes one, so a port opened earlier (for example an old ssh port) stays open until you remove it with ufw.

## Tests

```bash
bash tests/container_test.sh   # needs Docker; changes nothing on the host
```

## Known limitations

- Ubuntu 22.04 is the target; the real-host runs also covered 24.04. Both were GitHub-hosted runners, which are disposable VMs; the CPU architecture was not recorded. No other host has been tried.
- A container cannot run systemd, sshd or a firewall, so those parts cannot be proven that way.
