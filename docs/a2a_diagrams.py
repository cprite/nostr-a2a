#!/usr/bin/env python3
"""Generate dataflow + ER diagrams (SVG) for the Nostr A2A bridge/relay design."""
import sys
from xml.sax.saxutils import escape

OUT = sys.argv[1]
FONT = "DejaVu Sans"
C_AGENT, C_BRIDGE, C_RELAY = "#3b6fb6", "#2e8b57", "#d2691e"
C_NOTE_BG, C_NOTE_BD = "#fff8dc", "#c9b26b"


def T(x, y, s, size=12, weight="normal", anchor="start", fill="#222", style=""):
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}" fill="{fill}" {style}>{escape(s)}</text>')


def R(x, y, w, h, fill="#fff", stroke="#333", sw=1.2, rx=6, extra=""):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {extra}/>'


def tw(s, size=12):
    return len(s) * size * 0.58


DEFS = '''<defs>
<marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
<path d="M0,0 L10,5 L0,10 z" fill="#333"/></marker>
<marker id="ahr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
<path d="M0,0 L10,5 L0,10 z" fill="#b03030"/></marker>
</defs>'''


def cylinder(cx, y, label, color):
    w, h = 96, 34
    x = cx - w / 2
    return (f'<path d="M{x},{y+6} a{w/2},6 0 0,0 {w},0 a{w/2},6 0 0,0 {-w},0 v{h-12} '
            f'a{w/2},6 0 0,0 {w},0 v{-(h-12)}" fill="#f4f4f4" stroke="{color}" stroke-width="1.2"/>'
            + T(cx, y + h - 6, label, 11, anchor="middle", fill=color))


# ───────────────────────────── 1. DATAFLOW ─────────────────────────────
def dataflow():
    W = 1180
    lanes = {  # name: (x, title, subtitle, color, db)
        "scott": (110, "Scott", "agent (hack-lead)", C_AGENT, None),
        "ba": (345, "Bridge A", "background service", C_BRIDGE, "bridge-A.sqlite"),
        "relay": (590, "Relay", "nostr-rs-relay", C_RELAY, "relay.sqlite"),
        "bb": (835, "Bridge B", "runs while laptop is on", C_BRIDGE, "bridge-B.sqlite"),
        "coder": (1070, "Coder", "friend's agent", C_AGENT, None),
    }
    steps = [
        ("phase", "PHASE 0 · SETUP (once)"),
        ("notes", [("ba", ["A1  generate Scott's key pair", "→ IDENTITY row (npub + nsec)"]),
                   ("bb", ["B1  generate Coder's key pair", "→ IDENTITY row (npub + nsec)"])]),
        ("span", ["A2  owners swap npubs by hand (e.g. Telegram)  →  each bridge adds a PEER row for the other agent"]),
        ("notes", [("relay", ["A3  whitelist += Scott, Coder", "→ ALLOWED_PUBKEY rows"])]),
        ("phase", "PHASE 1 · OPEN TOPIC"),
        ("msg", "scott", "ba", "1  open topic \"zalu-api\" with Coder"),
        ("notes", [("ba", ["2  create TOPIC: topic_id + topic key pair", "MEMBER rows (Scott, Coder)",
                           "ROUTE: zalu-api → Scott / session S1"])]),
        ("msg", "ba", "relay", "3  INVITE event (sealed to Coder's npub)", "net"),
        ("notes", [("relay", ["4  store EVENT + TAG p = Coder's npub", "(laptop may be asleep — it waits)"])]),
        ("msg", "bb", "relay", "5  REQ: events with p = Coder's npub", "net"),
        ("msg", "relay", "bb", "invite", "net"),
        ("notes", [("bb", ["6  verify signature; sender in PEER?", "save TOPIC + topic key, MEMBER rows",
                           "ROUTE: zalu-api → Coder / session C1"])]),
        ("msg", "bb", "relay", "7  ACCEPT event (into topic box)", "net"),
        ("msg", "relay", "ba", "accept", "net"),
        ("notes", [("ba", ["TOPIC.status = active"])]),
        ("msg", "ba", "scott", "8  \"Coder joined zalu-api\""),
        ("phase", "PHASE 2 · REQUEST → RESPONSE"),
        ("msg", "scott", "ba", "9  \"What's the login endpoint?\""),
        ("notes", [("ba", ["10  MESSAGE row (out, pending)", "encrypt with topic key, sign as Scott"])]),
        ("msg", "ba", "relay", "11  EVENT, tag p = topic pubkey", "net"),
        ("notes", [("relay", ["store EVENT + TAG", "(cannot read content)"])]),
        ("msg", "bb", "relay", "12  REQ: p ∈ my topics, since CURSOR", "net"),
        ("msg", "relay", "bb", "event", "net"),
        ("notes", [("bb", ["13  verify sig · decrypt · dedup", "MESSAGE row (in) · CURSOR := now",
                           "ROUTE lookup → Coder / C1"])]),
        ("msg", "bb", "coder", "14  run deliver_cmd (session C1)"),
        ("notes", [("coder", ["15  works on it"])]),
        ("msg", "coder", "bb", "answer text"),
        ("notes", [("bb", ["MESSAGE row (out)", "reply_to = msg 10, same task_id"])]),
        ("msg", "bb", "relay", "16  response EVENT (same topic)", "net"),
        ("msg", "relay", "ba", "event (live subscription)", "net"),
        ("notes", [("ba", ["17  verify · decrypt", "ROUTE lookup → Scott / S1", "MESSAGE 10 state = answered"])]),
        ("msg", "ba", "scott", "18  deliver answer into session S1"),
        ("phase", "PHASE 3 · CLOSE"),
        ("msg", "scott", "ba", "19  close topic"),
        ("msg", "ba", "relay", "20  CLOSE event", "net"),
        ("msg", "relay", "bb", "close", "net"),
        ("notes", [("ba", ["21  TOPIC.status = closed", "delete ROUTE · wipe topic key"]),
                   ("relay", ["22  delete topic events", "(NIP-09 / retention)"]),
                   ("bb", ["21  TOPIC.status = closed", "delete ROUTE · wipe topic key"])]),
    ]

    body = []
    y = 200
    for st in steps:
        kind = st[0]
        if kind == "phase":
            y += 14
            body.append(R(20, y, W - 40, 26, fill="#e9edf3", stroke="#9aa7b8", rx=4))
            body.append(T(34, y + 18, st[1], 13, "bold", fill="#2b3a55"))
            y += 44
        elif kind == "msg":
            a, b, label = lanes[st[1]][0], lanes[st[2]][0], st[3]
            net = len(st) > 4
            color = "#b03030" if net else "#333"
            mk = "ahr" if net else "ah"
            dx = 6 if b > a else -6
            body.append(T((a + b) / 2, y - 6, label, 12, anchor="middle", fill=color))
            body.append(f'<line x1="{a+dx}" y1="{y}" x2="{b-dx}" y2="{y}" stroke="{color}" '
                        f'stroke-width="1.6" marker-end="url(#{mk})" {"stroke-dasharray=\"0\"" if not net else ""}/>')
            y += 38
        elif kind == "notes":
            hmax = 0
            for lane, lines in st[1]:
                cx = lanes[lane][0]
                w = max(tw(s, 11.5) for s in lines) + 18
                h = 16 * len(lines) + 10
                hmax = max(hmax, h)
                body.append(R(cx - w / 2, y - 12, w, h, fill=C_NOTE_BG, stroke=C_NOTE_BD, rx=4))
                for i, s in enumerate(lines):
                    body.append(T(cx - w / 2 + 9, y + 4 + 16 * i, s, 11.5))
            y += hmax + 18
        elif kind == "span":
            body.append(R(lanes["ba"][0] - 100, y - 12, lanes["bb"][0] - lanes["ba"][0] + 200, 26,
                          fill=C_NOTE_BG, stroke=C_NOTE_BD, rx=4))
            body.append(T(lanes["relay"][0], y + 5, st[1][0], 11.5, anchor="middle"))
            y += 34
    H = y + 150

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">', DEFS,
           R(0, 0, W, H, fill="#ffffff", stroke="none", rx=0)]
    # host zones
    zones = [(10, 467, "NIKOPI5 (your Pi)", "#eef3fb"), (467, 712, "PROXMOX CONTAINER", "#fdf1e6"),
             (712, W - 10, "FRIEND'S LAPTOP (other city)", "#eef7f0")]
    for x1, x2, name, col in zones:
        out.append(R(x1, 56, x2 - x1, H - 56 - 100, fill=col, stroke="none", rx=0))
        out.append(T((x1 + x2) / 2, 76, name, 12, "bold", anchor="middle", fill="#555"))
    for xb in (467, 712):
        out.append(f'<line x1="{xb}" y1="56" x2="{xb}" y2="{H-100}" stroke="#b03030" stroke-width="1" stroke-dasharray="6,5"/>')
    out.append(T(W / 2, 30, "Nostr A2A — dataflow (example: Scott ⇄ Coder, topic \"zalu-api\")", 18, "bold", anchor="middle"))
    out.append(T(W / 2, 48, "red = crosses the internet (wss://, signed + encrypted) · black = local on one machine · yellow = what the bridge/relay does + which table it writes",
                 11, anchor="middle", fill="#555"))
    # lane headers + lifelines
    for key, (x, title, sub, col, db) in lanes.items():
        out.append(f'<line x1="{x}" y1="140" x2="{x}" y2="{H-110}" stroke="{col}" stroke-width="1.2" stroke-dasharray="3,4"/>')
        out.append(R(x - 85, 88, 170, 44, fill=col, stroke=col))
        out.append(T(x, 107, title, 14, "bold", anchor="middle", fill="#fff"))
        out.append(T(x, 124, sub, 10.5, anchor="middle", fill="#fff"))
        if db:
            out.append(cylinder(x, 140, db, col))
    out += body
    # footer
    fy = H - 88
    out.append(R(20, fy, W - 40, 72, fill="#f7f7f7", stroke="#ccc", rx=4))
    out.append(T(34, fy + 22, "Crosses the network: only signed + encrypted EVENTs. The relay sees: author npub, topic pubkey (tag p), time, size — never the text.", 12))
    out.append(T(34, fy + 42, "Stays local on each machine: keys, ROUTE (topic → agent + session), MESSAGE history, CURSOR. Sessions never leave the host.", 12))
    out.append(T(34, fy + 62, "Agents never talk to the relay. Only the bridge does — and it hands a message only to the agent/session named in ROUTE for that topic.", 12))
    out.append("</svg>")
    return "\n".join(out)


# ───────────────────────────── 2. ER DIAGRAM ─────────────────────────────
ROW, HEAD = 19, 28


def entity(x, y, w, name, fields, color, sub=None):
    """fields: list of (key, name, note). returns svg, dict of row-y by field name, height."""
    h = HEAD + ROW * len(fields) + 6
    s = [R(x, y, w, h, fill="#fff", stroke=color, sw=1.6, rx=5),
         f'<path d="M{x},{y+HEAD} v{-HEAD+5} a5,5 0 0,1 5,-5 h{w-10} a5,5 0 0,1 5,5 v{HEAD-5} z" fill="{color}"/>',
         T(x + 10, y + 19, name, 13, "bold", fill="#fff")]
    if sub:
        s.append(T(x + w - 8, y + 19, sub, 10, anchor="end", fill="#fff"))
    rows = {}
    for i, (key, fname, note) in enumerate(fields):
        ry = y + HEAD + ROW * i + ROW - 5
        rows[fname] = y + HEAD + ROW * i + ROW / 2 + 1
        if key:
            s.append(T(x + 8, ry, key, 9.5, "bold", fill="#b03030" if "PK" in key else "#7a5a00"))
        s.append(T(x + 46, ry, fname, 11.5, "bold" if key and "PK" in key else "normal"))
        if note:
            s.append(T(x + w - 8, ry, note, 10, anchor="end", fill="#777"))
    return "\n".join(s), rows, h


def rel(points, c1, c2, dashed=False, color="#333", label=None, lpos=None):
    d = "M" + " L".join(f"{px},{py}" for px, py in points)
    s = [f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.4" {"stroke-dasharray=\"6,4\"" if dashed else ""}/>']
    (x1, y1), (x2, y2) = points[0], points[-1]
    (ax, ay), (bx, by) = points[1], points[-2]

    def lab(px, py, qx, qy, txt, first=True):
        # place cardinality just off the endpoint, toward the next point
        ox = 8 if qx > px else (-8 if qx < px else 6)
        oy = (-6 if first else 15) if qy == py else (14 if qy > py else -6)
        anc = "start" if ox > 0 else "end"
        return T(px + ox, py + oy, txt, 11, "bold", anchor=anc, fill="#b03030")
    if c1:
        s.append(lab(x1, y1, ax, ay, c1))
    if c2:
        s.append(lab(x2, y2, bx, by, c2, first=False))
    if label:
        lx, ly = lpos
        for i, line in enumerate(label):
            s.append(T(lx, ly + 14 * i, line, 10.5, anchor="middle", fill=color, style='font-style="italic"'))
    return "\n".join(s)


def erd():
    W, H = 1420, 940
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">', DEFS,
         R(0, 0, W, H, fill="#fff", stroke="none", rx=0),
         T(W / 2, 32, "Nostr A2A — entity-relationship (relay vs. bridge)", 18, "bold", anchor="middle"),
         T(W / 2, 52, "PK = primary key · FK = foreign key · 1 / 0..* = cardinality · dashed = matched by value at runtime, not a stored link",
           11, anchor="middle", fill="#555")]
    # group frames
    o.append(R(20, 70, 390, 850, fill="#fdf1e6", stroke=C_RELAY, sw=1.2, rx=8, extra='stroke-dasharray="5,4"'))
    o.append(T(36, 94, "RELAY  (Proxmox container · relay.sqlite)", 13, "bold", fill=C_RELAY))
    o.append(T(36, 112, "Generic Nostr software: no agents, no topics, no sessions", 10.5, fill="#777"))
    o.append(R(430, 70, 970, 850, fill="#eef7f0", stroke=C_BRIDGE, sw=1.2, rx=8, extra='stroke-dasharray="5,4"'))
    o.append(T(446, 94, "BRIDGE  (one copy per machine · bridge.sqlite)", 13, "bold", fill=C_BRIDGE))
    o.append(T(446, 112, "nikopi5 has one, friend's laptop has its own — never shared, never uploaded", 10.5, fill="#777"))

    # relay entities
    s, r_al, h_al = entity(55, 140, 320, "ALLOWED_PUBKEY", [("PK", "pubkey", "npub allowed to post"),
                                                          ("", "note", "\"Scott\", \"Coder\"")], C_RELAY, "whitelist")
    o.append(s)
    s, r_ev, h_ev = entity(55, 280, 320, "EVENT", [("PK", "id", "hash of the event"),
                                                 ("FK", "pubkey", "author npub"),
                                                 ("", "kind", "invite / message / close"),
                                                 ("", "created_at", "unix time"),
                                                 ("", "content", "ENCRYPTED blob"),
                                                 ("", "sig", "author's signature")], C_RELAY)
    o.append(s)
    s, r_tg, h_tg = entity(55, 540, 320, "TAG", [("FK", "event_id", "→ EVENT.id"),
                                               ("", "name", "\"p\""),
                                               ("", "value", "npub or topic pubkey")], C_RELAY, "indexed")
    o.append(s)
    o.append(rel([(215, 140 + h_al), (215, 280)], "1", "0..*"))
    o.append(rel([(215, 280 + h_ev), (215, 540)], "1", "0..*"))

    # bridge entities
    s, r_id, h_id = entity(460, 140, 280, "IDENTITY", [("PK", "agent_id", "\"hack-lead\""),
                                                     ("", "npub", "public key"),
                                                     ("", "nsec", "SECRET key"),
                                                     ("", "deliver_cmd", "\"openclaw agent …\"")], C_BRIDGE, "local agent")
    o.append(s)
    s, r_pe, h_pe = entity(1130, 140, 250, "PEER", [("PK", "npub", "remote agent key"),
                                                  ("", "label", "\"Coder@friend\""),
                                                  ("", "owner", "\"friend\""),
                                                  ("", "trusted", "bool")], C_BRIDGE, "remote agent")
    o.append(s)
    s, r_rt, h_rt = entity(460, 390, 280, "ROUTE", [("PK FK", "topic_id", "→ TOPIC"),
                                                  ("PK FK", "agent_id", "→ IDENTITY"),
                                                  ("", "local_session_id", "\"S1\""),
                                                  ("", "created_at", "")], C_BRIDGE, "topic → session")
    o.append(s)
    s, r_to, h_to = entity(820, 360, 270, "TOPIC", [("PK", "topic_id", "\"zalu-api-7f3c\""),
                                                  ("", "topic_pubkey", "address on relay"),
                                                  ("", "topic_secret", "shared w/ members"),
                                                  ("", "title", "\"ZALU API\""),
                                                  ("", "created_by", "npub"),
                                                  ("", "status", "invited/active/closed"),
                                                  ("", "created_at", ""),
                                                  ("", "closed_at", "")], C_BRIDGE)
    o.append(s)
    s, r_me, h_me = entity(1130, 390, 250, "MEMBER", [("PK FK", "topic_id", "→ TOPIC"),
                                                    ("PK FK", "npub", "→ PEER or IDENTITY"),
                                                    ("", "role", "owner / member"),
                                                    ("", "joined_at", "")], C_BRIDGE)
    o.append(s)
    s, r_cu, h_cu = entity(460, 640, 280, "CURSOR", [("PK", "relay_url", "wss://…"),
                                                   ("", "last_seen_at", "catch-up point")], C_BRIDGE, "per relay")
    o.append(s)
    s, r_ms, h_ms = entity(820, 610, 270, "MESSAGE", [("PK", "event_id", "= EVENT.id"),
                                                    ("FK", "topic_id", "→ TOPIC"),
                                                    ("", "msg_id", ""),
                                                    ("", "task_id", "request + its answers"),
                                                    ("FK", "reply_to", "→ MESSAGE.msg_id"),
                                                    ("", "type", "request/response/status/ack"),
                                                    ("", "from_npub", ""),
                                                    ("", "direction", "in / out"),
                                                    ("", "body", "DECRYPTED text"),
                                                    ("", "state", "pending/sent/delivered/failed"),
                                                    ("", "created_at", "")], C_BRIDGE)
    o.append(s)

    # bridge relationships
    o.append(rel([(600, 140 + h_id), (600, 390)], "1", "0..*"))                      # IDENTITY - ROUTE
    o.append(rel([(740, r_rt["topic_id"]), (820, r_rt["topic_id"])], "0..*", "1"))   # ROUTE - TOPIC
    o.append(rel([(1090, r_me["topic_id"]), (1130, r_me["topic_id"])], "1", "0..*"))  # TOPIC - MEMBER
    o.append(rel([(1255, 140 + h_pe), (1255, 390)], "1", "0..*"))                     # PEER - MEMBER
    o.append(rel([(740, r_id["npub"]), (1112, r_id["npub"]), (1112, r_me["npub"]), (1130, r_me["npub"])],
                 "1", "0..*", dashed=True, color="#555", label=["or (own agent)"], lpos=(930, r_id["npub"] - 6)))
    o.append(rel([(955, 360 + h_to), (955, 610)], "1", "0..*"))                      # TOPIC - MESSAGE
    # self reference reply_to
    ry = r_ms["reply_to"]
    o.append(rel([(1090, ry), (1112, ry), (1112, r_ms["msg_id"]), (1090, r_ms["msg_id"])], "0..*", "1",
                 label=["reply_to"], lpos=(1150, (ry + r_ms["msg_id"]) / 2 + 4)))

    # cross-system runtime matches
    vy = r_tg["value"]
    o.append(rel([(375, vy), (780, vy), (780, r_to["topic_pubkey"]), (820, r_to["topic_pubkey"])], None, None,
                 dashed=True, color="#b03030",
                 label=["TAG.value = TOPIC.topic_pubkey", "(bridge filters #p on its topics)"], lpos=(560, vy - 22)))
    cy = r_ev["content"]
    o.append(rel([(375, cy), (420, cy), (420, 860), (800, 860), (800, r_ms["body"]), (820, r_ms["body"])], None, None,
                 dashed=True, color="#b03030", label=["EVENT.content  —decrypt→  MESSAGE.body"], lpos=(600, 854)))

    # lookup path box
    o.append(R(45, 700, 345, 128, fill="#fff", stroke="#999", rx=6))
    o.append(T(58, 722, "How an incoming event finds its session", 12, "bold"))
    for i, line in enumerate(["1. EVENT arrives, TAG p = topic_pubkey",
                              "2. → TOPIC with that topic_pubkey (decrypt)",
                              "3. → ROUTE for that topic_id",
                              "4. → agent_id + local_session_id",
                              "5. run IDENTITY.deliver_cmd for that session"]):
        o.append(T(58, 742 + 17 * i, line, 11.5))
    o.append(T(W / 2, 910, "Sessions exist ONLY in ROUTE on each machine. The relay stores EVENT + TAG and nothing else.",
               12, "bold", anchor="middle", fill="#2b3a55"))
    o.append("</svg>")
    return "\n".join(o)


open(f"{OUT}/1-dataflow.svg", "w").write(dataflow())
open(f"{OUT}/2-erd.svg", "w").write(erd())
print("ok")
