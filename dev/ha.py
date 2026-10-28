"""Calls to the dev Home Assistant with the local test login (dev/local-login.json): `python3 dev/ha.py METHOD PATH [JSON]`."""
import json, sys, urllib.parse, urllib.request, os

login = json.load(open(os.path.join(os.path.dirname(__file__), "local-login.json")))


def token():
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": login["refresh_token"], "client_id": login["client_id"]}).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(login["url"] + "/auth/token", body)))["access_token"]


def call(method, path, data=None):
    req = urllib.request.Request(login["url"] + path, json.dumps(data).encode() if data is not None else None,
                                 {"Authorization": "Bearer " + token(), "Content-Type": "application/json"}, method=method)
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:500]}


if __name__ == "__main__":
    print(json.dumps(call(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else None), indent=1))
