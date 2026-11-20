# Relay setup: isolated Proxmox container + Tailscale Funnel

What you get: a private Nostr relay that only allowlisted keys can publish to, reachable from the
internet as `wss://<relay>.<tailnet>.ts.net/`, and unable to reach anything on your LAN, your
Proxmox host, other containers, or other devices on your tailnet.

Placeholders: `<CTID>` container id, `<node>` Proxmox node name, `<relay>` Tailscale hostname.
The isolated subnet below (`10.99.9.0/24` on `vmbr9`) is an example; pick one unused on your LAN.

## Isolation layers

| # | Layer | Blocks |
|---|---|---|
| 1 | Own bridge `vmbr9`, no physical port, NAT out via `vmbr0` | being on the same L2 network as anything |
| 2 | Host nftables table `inet relayiso` (`host/relay-isolation.nft`) | relay → LAN / host / other guests / tailnet IPs; anything → relay |
| 3 | Unprivileged CT, no nesting, no mounts, only `/dev/net/tun`, no SSH | container escape surface |
| 4 | Tailscale policy (`tailscale-policy.example.hujson`) | relay → your tailnet devices |
| 5 | CT nftables table `inet ct106` (`container/nftables.conf`) | new connections over `tailscale0`, both ways |
| 6 | Relay listens on `127.0.0.1` only; Funnel proxies 443 → it | anything reaching the relay except via Funnel |

Each layer was tested on its own (e.g. with layer 5 lifted, layer 4 alone still blocks the relay).

## 1. Host: bridge + firewall

```sh
pvesh create /nodes/<node>/network --iface vmbr9 --type bridge --cidr 10.99.9.1/24 --autostart 1
pvesh set /nodes/<node>/network                      # applies; vmbr0 untouched

install -m 644 host/relay-isolation.nft     /etc/relay-isolation.nft
install -m 644 host/relay-isolation.service /etc/systemd/system/
install -m 644 host/90-relay-nat.conf       /etc/sysctl.d/
nft -c -f /etc/relay-isolation.nft && systemctl daemon-reload && systemctl enable --now relay-isolation
sysctl -p /etc/sysctl.d/90-relay-nat.conf
```
The table only matches `vmbr9`; existing guests on `vmbr0` are not affected. Back up
`/etc/network/interfaces` first anyway.

## 2. Container

```sh
pct create <CTID> local:vztmpl/debian-13-standard_*_amd64.tar.zst --hostname nostr-relay \
  --unprivileged 1 --cores 1 --memory 512 --swap 256 --rootfs local-zfs:8 \
  --net0 name=eth0,bridge=vmbr9,ip=10.99.9.2/24,gw=10.99.9.1,firewall=0 \
  --nameserver "9.9.9.9 1.1.1.1" --onboot 1
cat >> /etc/pve/lxc/<CTID>.conf <<EOF
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF
pct start <CTID>
```
Inside (`pct exec <CTID> -- …`):
- Without nesting, systemd 257 fails three mounts: `systemctl mask dev-mqueue.mount run-lock.mount tmp.mount`.
- IPv6 is dropped on the segment: `echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4`.
- `apt install curl ca-certificates nftables`, then install `container/nftables.conf` as
  `/etc/nftables.conf` and `container/nftables-only-own-table.conf` into
  `/etc/systemd/system/nftables.service.d/`. **Gotcha:** Debian's `nftables.service` runs
  `nft flush ruleset` on stop, which wipes tailscaled's rules; the drop-in replaces that.

Verify from inside: `https://example.com` works; LAN gateway, Proxmox host, other guests and
`100.x` tailnet IPs all time out.

## 3. Relay

Build (see `UPSTREAM` for the pinned version and toolchain), e.g. cross-compiled on an arm64 box:
```sh
rustup override set 1.95.0 && rustup target add x86_64-unknown-linux-gnu
CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-gnu-gcc \
CC_x86_64_unknown_linux_gnu=x86_64-linux-gnu-gcc \
  cargo build --release --target x86_64-unknown-linux-gnu
```
Install: user `nostr` (system, nologin), binary `/opt/nostr-relay/nostr-rs-relay`, `config.toml` →
`/etc/nostr-relay/`, data `/var/lib/nostr-relay` (owned by nostr), `nostr-relay.service` →
`/etc/systemd/system/`. The hardening in the unit works without nesting.

`pubkey_whitelist = []` means **nobody** may publish. Omitting the key would mean everybody.
Add agent pubkeys (hex, not npub) and `systemctl restart nostr-relay`.

## 4. Tailscale + Funnel

1. Merge `tailscale-policy.example.hujson` into your tailnet policy **first**.
2. In the CT: install tailscale from pkgs.tailscale.com, then
   `tailscale up --advertise-tags=tag:relay --hostname=<relay> --accept-dns=false --accept-routes=false`
   and approve the login link.
3. `tailscale funnel --bg --https=443 http://127.0.0.1:7777`
4. Set `relay_url` in `config.toml` to the Funnel URL, restart the relay.

**Gotcha:** `--shields-up` refuses Funnel ("Unable to turn on Funnel while shields-up is enabled").
Inbound is covered by layers 5 and 6 instead. Port 443 on the relay's tailnet IP stays reachable
from your own devices: that is tailscaled's Funnel proxy, i.e. the same public endpoint.

## 5. Test

```sh
python3 -m venv .venv && .venv/bin/pip install coincurve websockets
.venv/bin/python tools/relay_test.py wss://<relay>.<tailnet>.ts.net/ --resolve-public
```
`--resolve-public` resolves the Funnel name over public DNS-over-HTTPS and connects over the
internet, bypassing your tailnet. A fresh key must get `blocked: pubkey is not allowed…`;
an allowlisted key (`NOSTR_SK=<hex>`) must get `OK true` and read its event back.
