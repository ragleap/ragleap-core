# Changelog

All notable changes to `ragleap-ansible` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.2] - 2026-10-28

### Fixed

- The firewall role failed on Ubuntu 24.04 when `/run/sshd` did not exist yet, which is the case on a host where SSH hardening had not run: `sshd -T` stopped with an error that begins 'Missing privilege separation' and `ufw` was never enabled (CI run 40466686432). The role now creates `/run/sshd` before it reads the effective sshd configuration. Verified on GitHub-hosted Ubuntu 22.04 and 24.04 runners (`ansible-vm-firewall-test`, run 40630611316): the role ran twice and the second run changed nothing, `ufw` was active with deny-incoming and allow-outgoing and ports 22 and 8082 open, a port that was not opened was blocked from a container on the Docker bridge, and the job survived. Not tested: reboot persistence, IPv6 rules, Docker published ports, SSH hardening together with the firewall, any host that is not a GitHub-hosted runner.

### Documentation

- README: the base role and the opt-in SSH hardening were run on real hosts, GitHub-hosted Ubuntu 22.04 and 24.04 runners, in CI (`ansible-vm-test`, run 40378770112): both ran twice with the second run changing nothing, the running sshd offered `publickey,password` before hardening and only `publickey` after, and a key login as the deploy user still worked. Two deliberate breaks made the check fail (runs 40465088911 and 40465114601). Not tested: reboot persistence, unattended upgrades actually running, a drop-in order other than the cloud image's, and any host that is not a GitHub-hosted runner.

## [0.1.1] - 2026-10-19

### Changed

- The project URLs on the package page (Source, Changelog, Issues) now point to github.com/ragleap/ragleap-core, where the repository moved from antonyrag/ragleap-core. No code or chart changes.

## [0.1.0] - 2026-10-14

### Added

- `ragleap_base` role: apt cache, base packages, a deploy user with key-only login, passwordless sudo validated with `visudo`, and unattended-upgrades.
- `playbooks/bootstrap.yml`, which targets only the group `ragleap_servers`.
- A check that rejects an SSH public key that is not a full OpenSSH key line, because a truncated key would lock the deploy user out once sshd hardening exists.
- Container test that runs the playbook twice in a throwaway Ubuntu 22.04 container and requires the second run to change nothing.
- Opt-in sshd hardening (`ragleap_base_harden_ssh`): a drop-in that turns off password and keyboard-interactive logins, checked with `sshd -t` and rolled back if the check fails. It refuses to run without a deploy key.
- Opt-in firewall (`ragleap_base_enable_firewall`): ufw with deny-incoming and allow-outgoing defaults, the ports sshd really listens on opened first, and extra TCP ports from `ragleap_base_firewall_allow_tcp`. `ufw enable` and real enforcement were not tested, because a container has no kernel firewall. The role only adds rules and never removes one.

### Not yet implemented

- Helm, k3s and chart installation. Nothing has been released.
