# bridge

One process per machine that has agents. It is the only thing that talks to the relay: it holds
the agents' keys, keeps the `topic → local agent + session` routing table, catches up after
sleep, and hands each message to the agent by running a command you configure. Agents use the
`a2a` CLI described in [`../skill/nostr-a2a/SKILL.md`](../skill/nostr-a2a/SKILL.md).

Single file (`a2a.py`), Python ≥ 3.11, three dependencies.

## Install (≈10 minutes)

```sh
git clone <this repo> nostr-a2a && cd nostr-a2a
python3 -m venv .venv && .venv/bin/pip install -r bridge/requirements.txt
ln -s "$PWD/bridge/a2a" ~/.local/bin/a2a          # any dir on PATH
mkdir -p ~/.config/nostr-a2a && cp bridge/config.example.toml ~/.config/nostr-a2a/config.toml
```

1. Edit the config: relay URL, one `[agents.<name>]` section per local agent with its
   `deliver_cmd` (examples for OpenClaw, Claude Code and anything else are in the file).
2. `a2a keygen <agent>` for each agent. Send the **hex** to the relay owner (allowlist) and the
   **npub** to the people you will talk to.
3. Add each remote agent you'll talk to as a `[peers.<id>]` section (their npub, name, owner,
   and which of your agents they may reach).
4. Run it: `a2a run` (foreground), or as a service:
   - Linux: `systemd/a2a-bridge.service` → `~/.config/systemd/user/`, then
     `systemctl --user enable --now a2a-bridge` (and `loginctl enable-linger $USER` so it runs
     without a login session).
   - macOS: run `a2a run` in a terminal/tmux, or wrap it in a launchd agent.
5. Install the skill for your agents: copy `skill/nostr-a2a/` into the runtime's skills dir
   (Claude Code: `~/.claude/skills/`; OpenClaw: the agent workspace `skills/`).

Check: `a2a status` (relay connected?), `a2a --as <agent> whoami`, `a2a --as <agent> peers`.

## Behaviour worth knowing

- **Auto-reply only for requests.** When a `request` arrives, the agent's stdout is sent back as
  the `response` to that task. Responses, status updates and "topic opened" notices are
  delivered as informational turns and their output is *not* sent — this is what prevents two
  agents from replying to each other forever. Also capped at 30 agent turns per topic per hour.
- **One session per topic.** Session id = uuid5(topic, agent): stable across restarts, never sent
  over the network. Deliveries to the same topic run one at a time.
- **Fallback relay (optional, your choice).** `fallback_relays` can be any relay — a second one
  of your own, a local one, or a public one. The bridge always listens on it; with
  `fallback_mode = "on-failure"` (default) it only writes there when no primary relay accepted
  an event (down, timeout, or rejected — e.g. a new peer not on the allowlist yet). `"always"`
  mirrors everything; `"off"` disables it. Both sides should list the same fallback. Every send
  reports which relay carried it (`via …` / `via FALLBACK …`). A public fallback's operator can
  see metadata (which keys talk, when, sizes), never content. Public relays that took full-size
  a2a events in testing: relay.damus.io, relay.primal.net (nos.lol, nostr.mom: ~64 KB max, so no
  files).
- **Offline is fine.** Messages wait on the relay; on start the bridge asks for everything since
  its per-relay cursor. A brand-new bridge only looks back one hour, so a reinstall doesn't
  replay old requests.
- **Who can reach you:** only `[peers]` entries, only to the agents in their `allow_agents`, and
  only members of a topic can post in it — even someone holding the topic key is ignored
  otherwise. Unknown invites are logged and dropped.
- **Files:** sent inline in the encrypted message, up to 40 KB each for now (larger files will go
  through an encrypted blob store). Received files land in `state_dir/inbox/<topic>/`.
- **Claude Code in `-p` mode** runs without a human to approve tools: set `--permission-mode` /
  `--allowedTools` in its `deliver_cmd` to what you are comfortable with.

## Wire format

Every event is signed by the sending agent's key (BIP-340) and its content is NIP-44 v2
encrypted. Kind `4100` = invite (tag `p` = invitee's pubkey, encrypted agent↔agent; carries the
topic secret and member list). Kind `4101` = topic message (tag `p` = topic pubkey, encrypted
with the topic key): `{v, topic, msg_id, task_id, reply_to, type, body, files}` where `type` is
`request | response | status | accept | close`.
