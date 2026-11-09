# Time synchronisation (host clock)

RagLeap does not set or synchronise clocks. Containers and Kubernetes pods use the clock of the machine
they run on, so the time in the audit log lines (`ts`, always UTC), in the database (`created_at`) and in
agent run records is only as good as the host clock.

This page is a guide for the **operator**. It is not legal advice and it does not make an installation
compliant with any law or standard. If a rule applies to you (for example the CERT-In Directions of
28 April 2022), what it requires of you is a question for your own counsel.

## Who does what

| Topic | Operator (you) | RagLeap |
|---|---|---|
| NTP service on the host or node | Install, configure, keep running | Not provided |
| Which time servers to use | Choose and document | Gives examples below |
| Drift monitoring | Run it and alert on it | Gives an example rule below; not in the Helm chart |
| Timestamps in logs and records | Keep the host clock right | Writes UTC |
| Ansible task for time sync | n/a | Not built yet |

## Check the host

```bash
timedatectl status          # want: "System clock synchronized: yes" and "NTP service: active"
chronyc tracking            # "System time" is the current offset from the reference
chronyc sources -v          # which servers are used and whether one is selected (marked with ^*)
docker exec ragleap-core-app-1 date -u    # a container shows the host time
```

## Set up chrony on Ubuntu or Debian

```bash
sudo apt-get install -y chrony
sudoedit /etc/chrony/chrony.conf     # replace the default pool or server lines with your servers
sudo systemctl restart chrony
chronyc sources -v
```

If you must use the servers published by the Indian government, the CERT-In FAQ of May 2022 names
`samay1.nic.in` and `samay2.nic.in` (National Informatics Centre) and `time.nplindia.org` (National Physical
Laboratory). We read this in secondary summaries of that FAQ, so check the current official text before
you rely on the names. The same FAQ says clocks do not have to be set to IST (NTP carries UTC), and that
cloud providers' own time services can be used if they do not deviate from those sources.

```text
server samay1.nic.in iburst
server samay2.nic.in iburst
server time.nplindia.org iburst
```

Offline or air-gapped sites cannot reach public servers. Run an NTP server inside your network (fed by GPS
or another source you trust) and point the hosts at it with the same `server ... iburst` lines.

Do not run an NTP service inside RagLeap containers. Fix the host (or the Kubernetes node) instead.

## Watch for drift

If you run Prometheus with node_exporter (the `ragleap-observability` chart does not install it today), these
rules are a starting point. Change the thresholds to your needs.

```yaml
groups:
  - name: host-clock
    rules:
      - alert: HostClockNotSynchronised
        expr: node_timex_sync_status == 0
        for: 10m
      - alert: HostClockDrift
        expr: abs(node_timex_offset_seconds) > 0.05
        for: 5m
```

## Not built yet

- An opt-in time-sync task in `ragleap-ansible` (the base role does not touch NTP).
- A node_exporter scrape and the clock rules above in the `ragleap-observability` chart.

Until then, use this page and your own tools. See also the
[security incident runbook](https://github.com/ragleap/ragleap-core/blob/main/docs/security-incident-runbook.md).
