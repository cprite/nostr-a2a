# bridge (next)

One small process per machine that has agents. It is the only thing that talks to the relay:
holds the agents' keys, keeps the topic → local agent/session routing table, catches up from a
per-relay cursor after sleep, and hands each message to the agent via a configurable command
(`openclaw agent …`, `claude -p …`, anything). See `docs/` for the dataflow and the data model.
