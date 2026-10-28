"""A websocket command to the dev Home Assistant: `python3 dev/ws.py TYPE '{"entity_id": ...}'` (subscriptions: the
events for a few seconds, `--listen SECONDS`)."""
import asyncio
import json
import os
import sys

import websockets

sys.path.insert(0, os.path.dirname(__file__))
from ha import login, token  # noqa: E402


async def main(kind: str, data: dict, listen: float) -> None:
    async with websockets.connect(login["url"].replace("http", "ws") + "/api/websocket", max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token()}))
        await ws.recv()
        await ws.send(json.dumps({"id": 1, "type": kind, **data}))
        end = asyncio.get_event_loop().time() + listen
        while True:
            timeout = max(0.1, end - asyncio.get_event_loop().time()) if listen else 60
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            except TimeoutError:
                return
            print(json.dumps(msg.get("result", msg.get("event", msg)), indent=1)[:3000])
            if not listen and msg.get("type") == "result":
                return


args = [a for a in sys.argv[1:] if not a.startswith("--")]
listen = float(sys.argv[sys.argv.index("--listen") + 1]) if "--listen" in sys.argv else 0
if listen:
    args = [a for a in args if a != str(sys.argv[sys.argv.index("--listen") + 1])]
asyncio.run(main(args[0], json.loads(args[1]) if len(args) > 1 else {}, listen))
