#!/usr/bin/env python3
"""nostr-a2a bridge: agent-to-agent messaging between machines over a Nostr relay.

One process per machine (`a2a run`) holds the agents' keys, keeps a websocket to the relay(s),
and routes messages: topic -> local agent + local session. Agents use the `a2a` CLI (see
skill/nostr-a2a/SKILL.md), which talks to the daemon over a unix socket.

Wire format (all events signed by the sending agent's key, content NIP-44 v2 encrypted):
  kind 4100  invite        tag p=<invitee agent pubkey>   content -> invitee (agent<->agent key)
  kind 4101  topic message tag p=<topic pubkey>           content -> topic key (shared secret)
"""
from __future__ import annotations

import argparse, asyncio, base64, contextlib, hashlib, hmac, json, logging, os, re, secrets
import shlex, sqlite3, ssl, sys, time, tomllib, uuid
from pathlib import Path

KIND_INVITE, KIND_TOPIC = 4100, 4101
PROTO = 1
MAX_INLINE_FILE = 40_000          # bytes per message (NIP-44 caps plaintext at 64 KiB)
DELIVERY_TIMEOUT = 3700
MAX_DELIVERIES_PER_HOUR = 30      # per topic, loop/runaway protection
SESSION_NS = uuid.UUID("6f1c4c1e-8a7b-4f0e-9d55-a2a0b51d9e01")
log = logging.getLogger("a2a")

# ───────────────────────────── crypto (lazy imports so the CLI stays light) ─────────────────────────────

def _cc():
    import coincurve
    return coincurve


def keypair(sk_hex: str | None = None) -> tuple[str, str]:
    cc = _cc()
    sk = cc.PrivateKey(bytes.fromhex(sk_hex)) if sk_hex else cc.PrivateKey()
    return sk.secret.hex(), sk.public_key_xonly.format().hex()


def event_id(ev: dict) -> str:
    ser = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                     separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(ser.encode()).hexdigest()


def sign_event(sk_hex: str, kind: int, content: str, tags: list) -> dict:
    cc = _cc()
    sk = cc.PrivateKey(bytes.fromhex(sk_hex))
    ev = {"pubkey": sk.public_key_xonly.format().hex(), "created_at": int(time.time()),
          "kind": kind, "tags": tags, "content": content}
    ev["id"] = event_id(ev)
    ev["sig"] = sk.sign_schnorr(bytes.fromhex(ev["id"])).hex()
    return ev


def verify_event(ev: dict) -> bool:
    try:
        if event_id(ev) != ev["id"]:
            return False
        return _cc().PublicKeyXOnly(bytes.fromhex(ev["pubkey"])).verify(
            bytes.fromhex(ev["sig"]), bytes.fromhex(ev["id"]))
    except Exception:
        return False


# NIP-44 v2 ------------------------------------------------------------------------------------------

def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def conversation_key(sk_hex: str, pk_hex: str) -> bytes:
    cc = _cc()
    point = cc.PublicKey(b"\x02" + bytes.fromhex(pk_hex)).multiply(bytes.fromhex(sk_hex))
    shared_x = point.format(compressed=True)[1:]
    return hmac.new(b"nip44-v2", shared_x, hashlib.sha256).digest()


def _padded_len(n: int) -> int:
    if n <= 32:
        return 32
    nxt = 1 << ((n - 1).bit_length())
    chunk = 32 if nxt <= 256 else nxt // 8
    return chunk * ((n - 1) // chunk + 1)


def _chacha(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    c = Cipher(algorithms.ChaCha20(key, b"\x00\x00\x00\x00" + nonce12), mode=None).encryptor()
    return c.update(data) + c.finalize()


def nip44_encrypt(plaintext: str, ckey: bytes) -> str:
    raw = plaintext.encode()
    if not 1 <= len(raw) <= 65535:
        raise ValueError(f"message too large for one event ({len(raw)} bytes, max 65535)")
    nonce = secrets.token_bytes(32)
    keys = _hkdf_expand(ckey, nonce, 76)
    padded = len(raw).to_bytes(2, "big") + raw + b"\x00" * (_padded_len(len(raw)) - len(raw))
    ct = _chacha(keys[:32], keys[32:44], padded)
    mac = hmac.new(keys[44:], nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ct + mac).decode()


def nip44_decrypt(payload: str, ckey: bytes) -> str:
    data = base64.b64decode(payload)
    if data[0] != 2:
        raise ValueError("unsupported NIP-44 version")
    nonce, ct, mac = data[1:33], data[33:-32], data[-32:]
    keys = _hkdf_expand(ckey, nonce, 76)
    if not hmac.compare_digest(mac, hmac.new(keys[44:], nonce + ct, hashlib.sha256).digest()):
        raise ValueError("bad MAC")
    padded = _chacha(keys[:32], keys[32:44], ct)
    n = int.from_bytes(padded[:2], "big")
    return padded[2:2 + n].decode()


# bech32 npub/nsec (BIP-173) ------------------------------------------------------------------------
_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(h):
    return [ord(x) >> 5 for x in h] + [0] + [ord(x) & 31 for x in h]


def _convertbits(data, frm, to, pad=True):
    acc = bits = 0
    out, maxv = [], (1 << to) - 1
    for v in data:
        acc = (acc << frm) | v
        bits += frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (to - bits)) & maxv)
    return out


def npub(pk_hex: str, hrp: str = "npub") -> str:
    data = _convertbits(bytes.fromhex(pk_hex), 8, 5)
    poly = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ 1
    return hrp + "1" + "".join(_B32[d] for d in data + [(poly >> 5 * (5 - i)) & 31 for i in range(6)])


def to_hex_pubkey(s: str) -> str:
    s = s.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", s):
        return s
    if s.startswith("npub1"):
        data = [_B32.index(c) for c in s[5:]]
        if _polymod(_hrp_expand("npub") + data) != 1:
            raise ValueError("bad npub checksum")
        return bytes(_convertbits(data[:-6], 5, 8, False)).hex()
    raise ValueError(f"not a pubkey: {s}")


# ───────────────────────────── config + state ─────────────────────────────

def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


DEFAULT_CONFIG = expand(os.environ.get("A2A_CONFIG", "~/.config/nostr-a2a/config.toml"))


class Config:
    def __init__(self, path: Path = DEFAULT_CONFIG):
        self.path = path
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
        self.relays: list[str] = raw.get("relays", [])
        # Fallback relays — any relay the user chooses (public, own, local). Always read;
        # written per fallback_mode.
        self.fallback_relays: list[str] = raw.get("fallback_relays", [])
        self.fallback_mode: str = raw.get("fallback_mode", "on-failure")
        if self.fallback_mode not in ("on-failure", "always", "off"):
            raise ValueError("fallback_mode must be on-failure | always | off")
        if self.fallback_mode == "off":
            self.fallback_relays = []
        self.state = expand(raw.get("state_dir", "~/.local/share/nostr-a2a"))
        self.agents: dict[str, dict] = raw.get("agents", {})
        self.peers: dict[str, dict] = {}
        for pid, p in raw.get("peers", {}).items():
            p = dict(p)
            p["pk"] = to_hex_pubkey(p["pubkey"])
            p.setdefault("allow_agents", list(self.agents))
            self.peers[pid] = p
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.state / "keys").mkdir(exist_ok=True, mode=0o700)

    @property
    def sock(self) -> Path:
        return self.state / "bridge.sock"

    def key(self, agent: str) -> str:
        return (self.state / "keys" / f"{agent}.key").read_text().strip()

    def agent_pk(self, agent: str) -> str:
        return keypair(self.key(agent))[1]

    def peer_by_pk(self, pk: str) -> dict | None:
        return next((p for p in self.peers.values() if p["pk"] == pk), None)

    def label(self, pk: str) -> str:
        """Agent@Owner for peers, display name for our own agents."""
        p = self.peer_by_pk(pk)
        if p:
            return f'{p.get("name", "?")}@{p.get("owner", "?")}'
        for a, ac in self.agents.items():
            with contextlib.suppress(Exception):
                if self.agent_pk(a) == pk:
                    return ac.get("display", a)
        return pk[:12] + "…"

    def resolve_peer(self, s: str) -> dict:
        for pid, p in self.peers.items():
            if s in (pid, f'{p.get("name")}@{p.get("owner")}', p.get("name")) or s.lower() == pid.lower():
                return p
        with contextlib.suppress(ValueError):
            p = self.peer_by_pk(to_hex_pubkey(s))
            if p:
                return p
        raise KeyError(f"unknown peer: {s} (known: {', '.join(self.peers) or 'none'})")


SCHEMA = """
CREATE TABLE IF NOT EXISTS topics(topic_id TEXT PRIMARY KEY, topic_pk TEXT UNIQUE, topic_sk TEXT,
  title TEXT, purpose TEXT, created_by TEXT, status TEXT, created_at INTEGER, closed_at INTEGER);
CREATE TABLE IF NOT EXISTS members(topic_id TEXT, pk TEXT, label TEXT, role TEXT, joined_at INTEGER,
  PRIMARY KEY(topic_id, pk));
CREATE TABLE IF NOT EXISTS routes(topic_id TEXT, agent TEXT, session_id TEXT, started INTEGER DEFAULT 0,
  created_at INTEGER, PRIMARY KEY(topic_id, agent));
CREATE TABLE IF NOT EXISTS messages(event_id TEXT PRIMARY KEY, topic_id TEXT, msg_id TEXT, task_id TEXT,
  reply_to TEXT, type TEXT, from_pk TEXT, direction TEXT, body TEXT, files TEXT, state TEXT,
  raw TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS cursor(relay TEXT PRIMARY KEY, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS seen(event_id TEXT PRIMARY KEY, at INTEGER);
CREATE TABLE IF NOT EXISTS deliveries(topic_id TEXT, at INTEGER);
"""


def db(cfg: Config) -> sqlite3.Connection:
    con = sqlite3.connect(cfg.state / "bridge.db", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    os.chmod(cfg.state / "bridge.db", 0o600)
    return con


def slug(s: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:24] or "topic") + "-" + secrets.token_hex(2)


# ───────────────────────────── the daemon ─────────────────────────────

class Bridge:
    def __init__(self, cfg: Config):
        self.cfg, self.db = cfg, db(cfg)
        self.conns: dict[str, object] = {}
        self.pending_ok: dict[str, asyncio.Future] = {}
        self.locks: dict[tuple, asyncio.Lock] = {}
        self.agent_pks = {a: cfg.agent_pk(a) for a in cfg.agents}
        self.pk_agent = {v: k for k, v in self.agent_pks.items()}

    # relay I/O ------------------------------------------------------------------------------------
    def filters(self) -> list[dict]:
        # First start (no cursor yet): look back only an hour, so a reinstalled bridge doesn't
        # replay and re-answer old requests that are still stored on the relay.
        since_inv = min((r["last_seen"] for r in self.db.execute("SELECT last_seen FROM cursor")),
                        default=int(time.time()) - 3000) - 600
        fs = [{"kinds": [KIND_INVITE], "#p": list(self.agent_pks.values()), "since": since_inv}]
        rows = self.db.execute("SELECT topic_pk, created_at FROM topics WHERE status IN ('invited','active')").fetchall()
        if rows:
            fs.append({"kinds": [KIND_TOPIC], "#p": [r["topic_pk"] for r in rows],
                       "since": min(r["created_at"] for r in rows) - 600})
        return fs

    async def resubscribe(self):
        for ws in list(self.conns.values()):
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(["REQ", "a2a", *self.filters()]))

    async def relay_loop(self, url: str):
        import websockets
        backoff = 2
        while True:
            try:
                async with websockets.connect(url, open_timeout=20, ping_interval=60,
                                              max_size=1 << 20, ssl=ssl.create_default_context()
                                              if url.startswith("wss") else None) as ws:
                    self.conns[url] = ws
                    backoff = 2
                    log.info("connected %s", url)
                    await ws.send(json.dumps(["REQ", "a2a", *self.filters()]))
                    await self.flush_outbox()
                    async for raw in ws:
                        await self.on_relay_message(url, json.loads(raw))
            except Exception as e:
                log.warning("relay %s: %s — reconnect in %ss", url, e, backoff)
            finally:
                self.conns.pop(url, None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def on_relay_message(self, url: str, msg: list):
        if msg[0] == "EVENT" and len(msg) >= 3:
            # Hand off to the worker: handling may publish and wait for an OK, which this
            # read loop has to be free to receive.
            await self.events.put((url, msg[2]))
        elif msg[0] == "OK":
            fut = self.pending_ok.get((url, msg[1]))
            if fut and not fut.done():
                fut.set_result((bool(msg[2]), msg[3] if len(msg) > 3 else ""))
        elif msg[0] == "NOTICE":
            log.info("notice from %s: %s", url, msg[1])

    async def _send_to(self, urls: list[str], ev: dict, timeout: float = 15) -> tuple[str | None, str]:
        """Send to the given connected relays; return (url that accepted, reasons) — url None if none did."""
        loop = asyncio.get_running_loop()
        futs = {}
        for u in urls:
            ws = self.conns.get(u)
            if not ws:
                continue
            futs[u] = self.pending_ok[(u, ev["id"])] = loop.create_future()
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(["EVENT", ev]))
        reasons = []
        try:
            pending = set(futs.values())
            deadline = loop.time() + timeout
            while pending:
                done, pending = await asyncio.wait(pending, timeout=max(0, deadline - loop.time()),
                                                   return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                for u, f in futs.items():
                    if f in done:
                        ok, why = f.result()
                        if ok:
                            return u, ""
                        reasons.append(f"{u}: {why or 'rejected'}")
            reasons += [f"{u}: no confirmation" for u, f in futs.items() if not f.done()]
            if not futs:
                reasons.append("not connected")
            return None, "; ".join(reasons)
        finally:
            for u in futs:
                self.pending_ok.pop((u, ev["id"]), None)

    async def publish(self, ev: dict) -> tuple[bool, str]:
        """Primary relays first; fallback relays per `fallback_mode`.
        Returns (accepted anywhere, where/why)."""
        prim, fb, mode = self.cfg.relays, self.cfg.fallback_relays, self.cfg.fallback_mode
        if mode == "always" and fb:
            (u1, why1), (u2, why2) = await asyncio.gather(self._send_to(prim, ev), self._send_to(fb, ev))
            if u1 or u2:
                return True, "via " + " + ".join(x for x in (u1, u2) if x)
            return False, f"primary: {why1}; fallback: {why2} (queued, will retry)"
        u, why = await self._send_to(prim, ev)
        if u:
            return True, f"via {u}"
        if mode == "on-failure" and fb:
            u2, why2 = await self._send_to(fb, ev)
            if u2:
                log.warning("primary relay failed (%s) — sent via fallback %s", why, u2)
                return True, f"via FALLBACK {u2} (primary: {why})"
            why = f"primary: {why}; fallback: {why2}"
        return False, f"{why} (queued, will retry)"

    async def flush_outbox(self):
        for r in self.db.execute("SELECT event_id, raw FROM messages WHERE direction='out' AND state='pending'").fetchall():
            ok, why = await self.publish(json.loads(r["raw"]))
            if ok:
                self.db.execute("UPDATE messages SET state='sent' WHERE event_id=?", (r["event_id"],))
                log.info("outbox: sent %s %s", r["event_id"][:12], why)

    async def event_worker(self):
        """Process incoming events one at a time, in arrival order."""
        while True:
            url, ev = await self.events.get()
            try:
                await self.on_event(ev)
            except Exception:
                log.exception("handling event %s", str(ev.get("id", "?"))[:12])
            self.db.execute("INSERT INTO cursor VALUES(?,?) ON CONFLICT(relay) DO UPDATE SET "
                            "last_seen=MAX(last_seen, excluded.last_seen)", (url, ev.get("created_at", 0)))

    async def outbox_loop(self):
        while True:
            await asyncio.sleep(30)
            with contextlib.suppress(Exception):
                await self.flush_outbox()

    # incoming -------------------------------------------------------------------------------------
    async def on_event(self, ev: dict):
        if self.db.execute("SELECT 1 FROM seen WHERE event_id=?", (ev.get("id"),)).fetchone():
            return
        if not verify_event(ev):
            log.warning("dropping event with bad id/signature")
            return
        self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (ev["id"], int(time.time())))
        if ev["pubkey"] in self.pk_agent and ev["kind"] == KIND_TOPIC:
            return  # our own topic message echoed back
        ptag = next((t[1] for t in ev["tags"] if len(t) > 1 and t[0] == "p"), None)
        if ev["kind"] == KIND_INVITE and ptag in self.pk_agent:
            await self.on_invite(ev, self.pk_agent[ptag])
        elif ev["kind"] == KIND_TOPIC:
            t = self.db.execute("SELECT * FROM topics WHERE topic_pk=?", (ptag,)).fetchone()
            if t:
                await self.on_topic_message(ev, t)

    async def on_invite(self, ev: dict, agent: str):
        peer = self.cfg.peer_by_pk(ev["pubkey"])
        if not peer or agent not in peer.get("allow_agents", []):
            log.warning("ignoring invite to %s from unknown/unallowed key %s", agent, ev["pubkey"][:12])
            return
        inv = json.loads(nip44_decrypt(ev["content"], conversation_key(self.cfg.key(agent), ev["pubkey"])))
        if inv.get("type") != "invite" or self.db.execute("SELECT 1 FROM topics WHERE topic_id=?", (inv["topic_id"],)).fetchone():
            return
        tsk, tpk = keypair(inv["topic_sk"])
        now = int(time.time())
        self.db.execute("INSERT INTO topics VALUES(?,?,?,?,?,?,?,?,NULL)",
                        (inv["topic_id"], tpk, tsk, inv.get("title", ""), inv.get("purpose", ""), ev["pubkey"], "active", ev["created_at"]))
        for m in inv.get("members", []):
            self.db.execute("INSERT OR IGNORE INTO members VALUES(?,?,?,?,?)",
                            (inv["topic_id"], m["pk"], self.cfg.label(m["pk"]), m.get("role", "member"), now))
        self.add_route(inv["topic_id"], agent)
        log.info("joined topic %s (%s) for %s, invited by %s", inv["topic_id"], inv.get("title"), agent, self.cfg.label(ev["pubkey"]))
        await self.resubscribe()
        await self.send_topic(agent, inv["topic_id"], "accept", f"{self.cfg.agents[agent].get('display', agent)} joined")
        await self.deliver(agent, inv["topic_id"], {
            "type": "opened", "from_pk": ev["pubkey"], "task_id": None,
            "body": f'{self.cfg.label(ev["pubkey"])} opened this topic with you.\n'
                    f'Title: {inv.get("title", "")}\nPurpose: {inv.get("purpose") or "-"}\n'
                    f'Members: {", ".join(self.cfg.label(m["pk"]) for m in inv.get("members", []))}\n'
                    f'Nothing to answer yet; wait for a request, or send one with `a2a send --topic {inv["topic_id"]} ...`.'},
            expect_reply=False)

    async def on_topic_message(self, ev: dict, t: sqlite3.Row):
        members = {r["pk"] for r in self.db.execute("SELECT pk FROM members WHERE topic_id=?", (t["topic_id"],))}
        if ev["pubkey"] not in members:
            log.warning("dropping message in %s from non-member %s", t["topic_id"], ev["pubkey"][:12])
            return
        m = json.loads(nip44_decrypt(ev["content"], conversation_key(t["topic_sk"], t["topic_pk"])))
        files = self.save_files(t["topic_id"], m.get("files", []))
        self.db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ev["id"], t["topic_id"], m.get("msg_id"), m.get("task_id"), m.get("reply_to"), m.get("type"),
                         ev["pubkey"], "in", m.get("body", ""), json.dumps(files), "received", None, ev["created_at"]))
        typ = m.get("type")
        if typ == "accept":
            self.db.execute("UPDATE topics SET status='active' WHERE topic_id=? AND status='invited'", (t["topic_id"],))
        if typ == "close":
            self.close_local(t["topic_id"])
        if typ == "accept":
            return  # visible in `a2a topics`; not worth an agent turn
        for r in self.db.execute("SELECT agent FROM routes WHERE topic_id=?", (t["topic_id"],)).fetchall():
            await self.deliver(r["agent"], t["topic_id"], {**m, "from_pk": ev["pubkey"], "files": files,
                                                           "event_id": ev["id"]},
                               expect_reply=(typ == "request"))

    def save_files(self, topic_id: str, files: list) -> list:
        out = []
        d = self.cfg.state / "inbox" / topic_id
        for f in files:
            data = base64.b64decode(f["data"])
            ok = hashlib.sha256(data).hexdigest() == f.get("sha256")
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(f.get("name", "file")).name) or "file"
            p = d / name
            p.write_bytes(data)
            out.append({"path": str(p), "sha256_ok": ok, "size": len(data)})
        return out

    # delivery to local agents ---------------------------------------------------------------------
    def add_route(self, topic_id: str, agent: str):
        sid = str(uuid.uuid5(SESSION_NS, f"{topic_id}/{agent}"))
        self.db.execute("INSERT OR IGNORE INTO routes VALUES(?,?,?,0,?)", (topic_id, agent, sid, int(time.time())))

    def render(self, topic_id: str, m: dict) -> str:
        t = self.db.execute("SELECT title FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        head = (f'[A2A] topic: {topic_id} "{t["title"] if t else ""}" · from: {self.cfg.label(m["from_pk"])}'
                f' · type: {m.get("type")}' + (f' · task: {m["task_id"]}' if m.get("task_id") else ""))
        files = "".join(f'\nattachment: {f["path"]} ({f["size"]} B, sha256 {"verified" if f["sha256_ok"] else "MISMATCH"})'
                        for f in m.get("files") or [])
        tail = ("\n\n(Your reply to this turn is sent back automatically as the response to this task.)"
                if m.get("type") == "request" else
                "\n\n(Informational: your reply is NOT sent anywhere. Use `a2a send` if you need to follow up.)")
        return f"{head}\n---\n{m.get('body', '')}{files}{tail}"

    async def deliver(self, agent: str, topic_id: str, m: dict, expect_reply: bool):
        hour = int(time.time()) - 3600
        n = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE topic_id=? AND at>?", (topic_id, hour)).fetchone()[0]
        if n >= MAX_DELIVERIES_PER_HOUR:
            log.warning("rate limit: %s deliveries in the last hour for %s, not waking %s", n, topic_id, agent)
            return
        self.db.execute("INSERT INTO deliveries VALUES(?,?)", (topic_id, int(time.time())))
        asyncio.create_task(self._deliver(agent, topic_id, m, expect_reply))

    async def _deliver(self, agent: str, topic_id: str, m: dict, expect_reply: bool):
        lock = self.locks.setdefault((topic_id, agent), asyncio.Lock())
        async with lock:   # one turn at a time per topic session
            ac = self.cfg.agents[agent]
            route = self.db.execute("SELECT * FROM routes WHERE topic_id=? AND agent=?", (topic_id, agent)).fetchone()
            text = self.render(topic_id, m)
            tmpl = ac.get("resume_cmd") if route["started"] and ac.get("resume_cmd") else ac["deliver_cmd"]
            subs = {"{session}": route["session_id"], "{message}": text, "{agent}": agent, "{topic}": topic_id}
            argv = [subs.get(a, a) if a in subs else _sub(a, subs) for a in tmpl]
            stdin_msg = "{message}" not in " ".join(tmpl)
            env = {**os.environ, "A2A_AGENT": agent, "A2A_TOPIC": topic_id, "A2A_SESSION": route["session_id"]}
            log.info("delivering %s in %s to %s", m.get("type"), topic_id, agent)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=expand(ac.get("workdir", "~")), env=env,
                    stdin=asyncio.subprocess.PIPE if stdin_msg else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(proc.communicate(text.encode() if stdin_msg else None), DELIVERY_TIMEOUT)
            except Exception as e:
                log.error("delivery to %s failed: %s", agent, e)
                if expect_reply:
                    await self.send_topic(agent, topic_id, "status", f"delivery failed on our side: {e}",
                                          task_id=m.get("task_id"), reply_to=m.get("msg_id"))
                return
            self.db.execute("UPDATE routes SET started=1 WHERE topic_id=? AND agent=?", (topic_id, agent))
            if proc.returncode != 0:
                log.error("agent %s exited %s: %s", agent, proc.returncode, err.decode(errors="replace")[-500:])
            reply = parse_reply(out.decode(errors="replace"), ac.get("reply_format", "text")).strip()
            if expect_reply and reply and reply not in ("NO_REPLY", "HEARTBEAT_OK"):
                await self.send_topic(agent, topic_id, "response", reply, task_id=m.get("task_id"), reply_to=m.get("msg_id"))

    # outgoing -------------------------------------------------------------------------------------
    async def send_topic(self, agent: str, topic_id: str, typ: str, body: str, task_id=None, reply_to=None,
                         files: list | None = None) -> dict:
        t = self.db.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        if not t or t["status"] == "closed":
            raise ValueError(f"no open topic {topic_id}")
        if not self.db.execute("SELECT 1 FROM members WHERE topic_id=? AND pk=?", (topic_id, self.agent_pks[agent])).fetchone():
            raise PermissionError(f"{agent} is not a member of {topic_id}")
        msg_id = "m-" + secrets.token_hex(4)
        task_id = task_id or ("t-" + secrets.token_hex(3) if typ == "request" else None)
        m = {"v": PROTO, "topic": topic_id, "msg_id": msg_id, "task_id": task_id, "reply_to": reply_to,
             "type": typ, "body": body, "files": files or []}
        content = nip44_encrypt(json.dumps(m, ensure_ascii=False), conversation_key(t["topic_sk"], t["topic_pk"]))
        ev = sign_event(self.cfg.key(agent), KIND_TOPIC, content, [["p", t["topic_pk"]]])
        self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ev["id"], topic_id, msg_id, task_id, reply_to, typ, ev["pubkey"], "out", body,
                         json.dumps([{"name": f["name"], "size": len(base64.b64decode(f["data"]))} for f in files or []]),
                         "pending", json.dumps(ev), ev["created_at"]))
        self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (ev["id"], int(time.time())))
        ok, why = await self.publish(ev)
        self.db.execute("UPDATE messages SET state=? WHERE event_id=?", ("sent" if ok else "pending", ev["id"]))
        return {"ok": True, "topic": topic_id, "msg_id": msg_id, "task_id": task_id,
                "delivered_to_relay": ok, ("relay" if ok else "note"): why}

    async def open_topic(self, agent: str, peers: list[str], title: str, purpose: str) -> dict:
        ps = [self.cfg.resolve_peer(p) for p in peers]
        for p in ps:
            if agent not in p.get("allow_agents", []):
                raise PermissionError(f"{agent} is not allowed to talk to {p.get('name')}@{p.get('owner')} (see allow_agents)")
        tsk, tpk = keypair()
        tid, now, me = slug(title), int(time.time()), self.agent_pks[agent]
        members = [{"pk": me, "role": "owner"}] + [{"pk": p["pk"], "role": "member"} for p in ps]
        self.db.execute("INSERT INTO topics VALUES(?,?,?,?,?,?,?,?,NULL)", (tid, tpk, tsk, title, purpose, me, "invited", now))
        for mm in members:
            self.db.execute("INSERT INTO members VALUES(?,?,?,?,?)", (tid, mm["pk"], self.cfg.label(mm["pk"]), mm["role"], now))
        self.add_route(tid, agent)
        await self.resubscribe()
        results = []
        for p in ps:
            inv = {"v": PROTO, "type": "invite", "topic_id": tid, "topic_sk": tsk, "title": title,
                   "purpose": purpose, "members": members}
            ev = sign_event(self.cfg.key(agent), KIND_INVITE,
                            nip44_encrypt(json.dumps(inv), conversation_key(self.cfg.key(agent), p["pk"])), [["p", p["pk"]]])
            ok, why = await self.publish(ev)
            if not ok:
                self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (ev["id"], tid, None, None, None, "invite", me, "out", title, "[]", "pending", json.dumps(ev), now))
            results.append({"peer": f'{p.get("name")}@{p.get("owner")}', "invite_sent": ok, ("relay" if ok else "note"): why})
        return {"ok": True, "topic": tid, "title": title, "invites": results}

    def close_local(self, topic_id: str):
        self.db.execute("UPDATE topics SET status='closed', closed_at=?, topic_sk=NULL WHERE topic_id=?",
                        (int(time.time()), topic_id))

    # local API (unix socket, one JSON request per connection) -------------------------------------
    async def api(self, reader, writer):
        try:
            req = json.loads(await reader.readline())
            res = await self.handle(req)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        writer.write((json.dumps(res, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()

    def pick_agent(self, req) -> str:
        a = req.get("as") or os.environ.get("A2A_AGENT")
        if not a:
            if len(self.cfg.agents) != 1:
                raise ValueError(f"several local agents ({', '.join(self.cfg.agents)}): pass --as <agent>")
            a = next(iter(self.cfg.agents))
        if a not in self.cfg.agents:
            raise KeyError(f"unknown local agent {a}")
        return a

    async def handle(self, req: dict) -> dict:
        op = req["op"]
        if op == "status":
            return {"ok": True, "relays": {u: (u in self.conns) for u in self.cfg.relays},
                    "fallback_relays": {u: (u in self.conns) for u in self.cfg.fallback_relays},
                    "fallback_mode": self.cfg.fallback_mode,
                    "agents": list(self.cfg.agents)}
        agent = self.pick_agent(req)
        me = self.agent_pks[agent]
        if op == "whoami":
            return {"ok": True, "agent": agent, "display": self.cfg.agents[agent].get("display", agent),
                    "npub": npub(me), "hex": me, "relays_connected": list(self.conns)}
        if op == "peers":
            return {"ok": True, "peers": [f'{p.get("name")}@{p.get("owner")}' for p in self.cfg.peers.values()
                                          if agent in p.get("allow_agents", [])]}
        if op == "topics":
            rows = self.db.execute("SELECT t.* FROM topics t JOIN routes r USING(topic_id) WHERE r.agent=? "
                                   "ORDER BY t.created_at DESC", (agent,)).fetchall()
            return {"ok": True, "topics": [{
                "id": r["topic_id"], "title": r["title"], "status": r["status"],
                "members": [m["label"] for m in self.db.execute("SELECT label FROM members WHERE topic_id=?", (r["topic_id"],))]}
                for r in rows]}
        if op == "open":
            return await self.open_topic(agent, req["with"], req["title"], req.get("purpose", ""))
        if op == "send":
            files = []
            for fp in req.get("files", []):
                data = expand(fp).read_bytes()
                if len(data) > MAX_INLINE_FILE:
                    raise ValueError(f"{fp} is {len(data)} B; inline limit is {MAX_INLINE_FILE} B (large files: not yet)")
                files.append({"name": Path(fp).name, "sha256": hashlib.sha256(data).hexdigest(),
                              "data": base64.b64encode(data).decode()})
            return await self.send_topic(agent, req["topic"], req.get("type", "request"), req["body"],
                                         task_id=req.get("task"), files=files)
        if op == "inbox":
            since = int(time.time()) - int(req.get("since", 86400))
            rows = self.db.execute("SELECT * FROM messages WHERE topic_id=? AND created_at>=? ORDER BY created_at",
                                   (req["topic"], since)).fetchall()
            return {"ok": True, "messages": [{
                "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])), "from": self.cfg.label(r["from_pk"]),
                "type": r["type"], "task": r["task_id"], "body": r["body"], "files": json.loads(r["files"] or "[]"),
                "state": r["state"]} for r in rows]}
        if op == "close":
            res = await self.send_topic(agent, req["topic"], "close", req.get("body") or "topic closed")
            self.close_local(req["topic"])
            await self.resubscribe()
            return res
        raise ValueError(f"unknown op {op}")

    async def run(self):
        if self.cfg.sock.exists():
            self.cfg.sock.unlink()
        server = await asyncio.start_unix_server(self.api, path=str(self.cfg.sock))
        os.chmod(self.cfg.sock, 0o600)
        log.info("bridge up: agents=%s relays=%s fallback=%s (%s)", list(self.cfg.agents), self.cfg.relays,
                 self.cfg.fallback_relays, self.cfg.fallback_mode)
        self.events = asyncio.Queue()
        await asyncio.gather(server.serve_forever(), self.outbox_loop(), self.event_worker(),
                             *(self.relay_loop(u) for u in self.cfg.relays + self.cfg.fallback_relays))


def _sub(arg: str, subs: dict) -> str:
    for k, v in subs.items():
        arg = arg.replace(k, v)
    return arg


def parse_reply(out: str, fmt: str) -> str:
    if fmt == "openclaw-json":
        with contextlib.suppress(Exception):
            start = out.index("{")
            j = json.loads(out[start:])
            res = j.get("result", j)
            return "\n\n".join(p.get("text", "") for p in res.get("payloads", []) if p.get("text"))
    if fmt == "claude-json":
        with contextlib.suppress(Exception):
            return json.loads(out).get("result", "")
    return out


# ───────────────────────────── CLI ─────────────────────────────

async def call(cfg: Config, req: dict) -> dict:
    try:
        r, w = await asyncio.open_unix_connection(str(cfg.sock))
    except (FileNotFoundError, ConnectionRefusedError):
        return {"ok": False, "error": "bridge is not running (start it with `a2a run`)"}
    w.write((json.dumps(req) + "\n").encode())
    await w.drain()
    return json.loads(await r.readline())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="a2a", description="Agent-to-agent messaging over Nostr")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--as", dest="as_", help="local agent to act as (default: $A2A_AGENT or the only one)")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the bridge daemon")
    k = sub.add_parser("keygen", help="create a key for a local agent (prints the pubkey to allowlist)")
    k.add_argument("agent")
    for c in ("whoami", "peers", "topics", "status"):
        sub.add_parser(c)
    o = sub.add_parser("open")
    o.add_argument("--with", dest="with_", action="append", required=True)
    o.add_argument("--title", required=True)
    o.add_argument("--purpose", default="")
    s = sub.add_parser("send")
    s.add_argument("--topic", required=True)
    s.add_argument("--task")
    s.add_argument("--type", default="request", choices=["request", "response", "status"])
    s.add_argument("--file", action="append", default=[])
    s.add_argument("body")
    i = sub.add_parser("inbox")
    i.add_argument("--topic", required=True)
    i.add_argument("--since", default="24h")
    c = sub.add_parser("close")
    c.add_argument("--topic", required=True)
    c.add_argument("body", nargs="?")
    a = ap.parse_args(argv)
    cfg = Config(expand(a.config))

    if a.cmd == "run":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        asyncio.run(Bridge(cfg).run())
        return 0
    if a.cmd == "keygen":
        kp = cfg.state / "keys" / f"{a.agent}.key"
        if kp.exists():
            sk = kp.read_text().strip()
        else:
            sk, _ = keypair()
            kp.write_text(sk + "\n")
            os.chmod(kp, 0o600)
        pk = keypair(sk)[1]
        print(json.dumps({"agent": a.agent, "npub": npub(pk), "hex": pk,
                          "note": "add the hex to the relay's pubkey_whitelist; give the npub to peers"}, indent=1))
        return 0

    req = {"op": a.cmd, "as": a.as_}
    if a.cmd == "open":
        req.update({"with": a.with_, "title": a.title, "purpose": a.purpose})
    elif a.cmd == "send":
        req.update({"topic": a.topic, "task": a.task, "type": a.type, "body": a.body,
                    "files": [str(Path(f).resolve()) for f in a.file]})
    elif a.cmd == "inbox":
        m = re.fullmatch(r"(\d+)([smhd]?)", a.since)
        req.update({"topic": a.topic, "since": int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 3600}[m[2]]})
    elif a.cmd == "close":
        req.update({"topic": a.topic, "body": a.body})
    res = asyncio.run(call(cfg, req))
    if a.json or not res.get("ok"):
        print(json.dumps(res, ensure_ascii=False, indent=1), file=sys.stdout if res.get("ok") else sys.stderr)
        return 0 if res.get("ok") else 1
    if a.cmd == "inbox":
        for m in res["messages"]:
            print(f'[{m["at"]}] {m["from"]} · {m["type"]}' + (f' · {m["task"]}' if m["task"] else "") + f'\n  {m["body"]}')
            for f in m["files"]:
                print(f'  attachment: {f.get("path") or f.get("name")}')
    elif a.cmd == "topics":
        for t in res["topics"]:
            print(f'{t["id"]}  [{t["status"]}]  "{t["title"]}"  — {", ".join(t["members"])}')
    elif a.cmd == "peers":
        print("\n".join(res["peers"]) or "(no peers configured)")
    else:
        print(json.dumps({k: v for k, v in res.items() if k != "ok"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
