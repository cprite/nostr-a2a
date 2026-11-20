# nostr-a2a

Agent-to-agent messaging between machines that are not on the same network, over
[Nostr](https://github.com/nostr-protocol/nips).

The problem it solves: you and a friend each run AI agents on your own computers, in different
cities, and today you copy messages between them by hand. Laptops sleep, change networks and sit
behind NAT, so a plain HTTP endpoint per agent doesn't work well. With Nostr, every machine only
connects **out** to a relay; the relay stores messages until the other side wakes up; every
message is signed by its sender and end-to-end encrypted, so the relay can't read or forge it.

## How it works

```
 your machine                    relay                     friend's machine
 agent ⇄ bridge  ──wss──►  [ stores signed,  ]  ◄──wss──  bridge ⇄ agent
                            [ encrypted events ]
```

- **Relay** — off-the-shelf [nostr-rs-relay](https://github.com/scsibug/nostr-rs-relay), private:
  only allowlisted keys may publish. Knows nothing about agents, topics or sessions.
- **Bridge** — one small process per machine; the only thing with keys and a relay connection.
  Agents never talk to Nostr themselves.
- **Topics** — like ROS 2 topics. One is opened with an invite; each gets its own key pair. Its
  public key is the address messages are tagged with; its secret is shared only with members.
- **Sessions stay local** — each bridge maps `topic → local agent + local session` in its own
  table. Session ids never go over the network.
- **Files** — encrypted with a one-off key and stored as blobs (Blossom); the message carries
  the link, hash and key.

Full dataflow and data model: [`docs/a2a-nostr-diagrams.pdf`](docs/a2a-nostr-diagrams.pdf).

## Status

| Part | State |
|---|---|
| Design, dataflow + ER diagrams | done (`docs/`) |
| Private relay in an isolated Proxmox container behind Tailscale Funnel | running, tested (`relay/`) |
| Relay smoke test over the public internet path | done (`tools/relay_test.py`) |
| Agent skill (`SKILL.md`) for Claude Code, OpenClaw and any CLI agent | draft interface (`skill/`) |
| Bridge (`a2a` daemon + CLI): invites, topics, request→auto-response, files ≤40 KB, offline catch-up, close | working, tested end to end through the live relay (`bridge/`) |
| Optional fallback relay (own, local or public), on-failure / always / off | working, tested live (primary rejected → round trip via relay.damus.io) |
| Encrypted file transfer (Blossom) for files > 40 KB | next |

## Layout

```
docs/     design diagrams (PDF + SVG) and the script that draws them
relay/    relay config, systemd unit, host + container firewall rules,
          Tailscale policy template, SETUP.md (step by step, with the gotchas)
tools/    relay_test.py — publish/read smoke test
skill/    nostr-a2a/SKILL.md — what agents read: the `a2a` CLI, message format, safety rules
bridge/   a2a.py (daemon + CLI), config example, systemd unit, install guide
```
