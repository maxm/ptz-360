"""Adds a camera to the dev Home Assistant with the ONVIF login from Frigate's config (on a server, over ssh),
without printing it: `python3 dev/add-from-frigate.py [camera] [ssh host] [config path]`."""
import json
import subprocess
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from ha import call  # noqa: E402

camera = sys.argv[1] if len(sys.argv) > 1 else "casa_norte"
host = sys.argv[2] if len(sys.argv) > 2 else "carpincho-server"
path = sys.argv[3] if len(sys.argv) > 3 else "/home/max/docker/frigate/config/config.yml"

read = f"""python3 -c 'import yaml, json; c = yaml.safe_load(open("{path}")); print(json.dumps(c["cameras"]["{camera}"]["onvif"]))'"""
onvif = json.loads(subprocess.run(["ssh", host, read], check=True, capture_output=True, text=True).stdout)
flow = call("POST", "/api/config/config_entries/flow", {"handler": "ptz_360"})
result = call("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", {
    "host": onvif["host"], "port": int(onvif.get("port", 80)), "username": onvif["user"], "password": onvif["password"]})
if result.get("type") == "create_entry":
    print(f"added: {result.get('title')}")
else:
    print(f"not added: {result.get('errors') or result.get('reason') or result}")
    call("DELETE", f"/api/config/config_entries/flow/{flow['flow_id']}")
