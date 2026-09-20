# Optional Docker egress lockdown (Ubuntu 24.04)

Containers have internet access by default. On a dedicated task host, opt in:

```bash
sudo ./install.sh --egress-lockdown
```

The installer installs nftables if necessary and enables the owned
`fleet-gateway-egress.service`. Its table is applied atomically and idempotently;
reapplying flushes only `inet fleet_gateway`, never the host's other rules.
It covers Docker's default `docker0` and `br-*` bridges (all containers on them).
Custom bridge names, host networking, macvlan, and arbitrary operator-created
networks require separate policy. Gateway containers use Docker's default bridge.

Exact rules (also shipped as `systemd/fleet-gateway-egress.nft`):

```nft
add table inet fleet_gateway
flush table inet fleet_gateway
add chain inet fleet_gateway forward { type filter hook forward priority -10; policy accept; }
add rule inet fleet_gateway forward iifname "docker0" ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16, 127.0.0.0/8 } drop
add rule inet fleet_gateway forward iifname "br-*" ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16, 127.0.0.0/8 } drop
add rule inet fleet_gateway forward iifname "docker0" ip6 daddr { fc00::/7, fe80::/10, ::1/128 } drop
add rule inet fleet_gateway forward iifname "br-*" ip6 daddr { fc00::/7, fe80::/10, ::1/128 } drop
add chain inet fleet_gateway input { type filter hook input priority -10; policy accept; }
add rule inet fleet_gateway input iifname "docker0" drop
add rule inet fleet_gateway input iifname "br-*" drop
```

The forward hook blocks RFC1918, CGNAT (including Tailscale IPv4), link-local
(including metadata endpoints), and IPv6 private/link-local destinations.
The input hook blocks access to services on the Docker host itself. Public
internet forwarding remains allowed subject to other host rules. A private DNS
resolver will also be blocked; configure public DNS if your deployment needs it.
These network rules do not make Docker a hostile-code security boundary.

Manual check/apply/inspect (requires `sudo apt-get install nftables`):

```bash
sudo nft --check -f systemd/fleet-gateway-egress.nft
sudo nft -f systemd/fleet-gateway-egress.nft
sudo nft list table inet fleet_gateway
```

Remove persistence and the rules:

```bash
sudo systemctl disable --now fleet-gateway-egress.service
sudo nft delete table inet fleet_gateway # only if the table still exists
```

Verify on the actual Ubuntu host: an internet request should succeed, while a
request from a default-bridge container to a known reachable private/tailnet
endpoint should time out. Do not apply these Linux rules to macOS; Docker runs
inside a VM there. The repository's macOS tests exercise containers and HTTP,
not Ubuntu systemd/nftables installation.
