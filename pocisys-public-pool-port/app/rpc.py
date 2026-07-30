import base64
import json
import urllib.error
import urllib.request


class RpcError(RuntimeError):
    pass


class BitcoinRpc:
    def __init__(self, url, username="", password="", timeout=10):
        self.url = url.rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        self.request_id = 0

    def call(self, method, params=None):
        self.request_id += 1
        payload = json.dumps({
            "jsonrpc": "1.0", "id": self.request_id, "method": method, "params": params or []
        }).encode()
        request = urllib.request.Request(self.url, data=payload, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RpcError(str(exc)) from exc
        if body.get("error") is not None:
            raise RpcError(str(body["error"]))
        return body.get("result")

