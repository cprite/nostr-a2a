#!/usr/bin/env python3
"""Relay smoke test: publish a signed NIP-01 event, then try to read it back.
Usage: relay_test.py <wss-url> [--resolve-public]   (key is generated fresh unless NOSTR_SK hex is set)"""
import asyncio, hashlib, json, os, socket, ssl, sys, time
import coincurve, websockets

url = sys.argv[1]
sk = coincurve.PrivateKey(bytes.fromhex(os.environ["NOSTR_SK"])) if os.environ.get("NOSTR_SK") else coincurve.PrivateKey()
pk = sk.public_key_xonly.format().hex()

def event(kind, content, tags=()):
    ev = {"pubkey": pk, "created_at": int(time.time()), "kind": kind, "tags": list(tags), "content": content}
    ser = json.dumps([0, ev["pubkey"], ev["created_at"], kind, ev["tags"], content], separators=(",", ":"), ensure_ascii=False)
    ev["id"] = hashlib.sha256(ser.encode()).hexdigest()
    ev["sig"] = sk.sign_schnorr(bytes.fromhex(ev["id"])).hex()
    return ev

async def main():
    kw = {}
    if "--resolve-public" in sys.argv:
        # Bypass MagicDNS/tailnet: resolve via public DNS and connect over the internet.
        import urllib.request
        host = url.split("//")[1].split("/")[0]
        ans = json.load(urllib.request.urlopen(urllib.request.Request(
            f"https://cloudflare-dns.com/dns-query?name={host}&type=A", headers={"accept": "application/dns-json"})))
        ip = [a["data"] for a in ans.get("Answer", []) if a["type"] == 1][0]
        print("public IP of funnel:", ip)
        kw["sock"] = socket.create_connection((ip, 443), timeout=10)
        kw["server_hostname"] = host
        kw["ssl"] = ssl.create_default_context()
    async with websockets.connect(url, open_timeout=15, **kw) as ws:
        ev = event(4242, "relay smoke test")
        await ws.send(json.dumps(["EVENT", ev]))
        print("pubkey:", pk[:16] + "…")
        for _ in range(3):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            print("<-", json.dumps(msg)[:200])
            if msg[0] == "OK":
                break
        await ws.send(json.dumps(["REQ", "t1", {"ids": [ev["id"]]}]))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            print("<-", json.dumps(msg)[:200])
            if msg[0] in ("EOSE", "CLOSED"):
                break

asyncio.run(main())
