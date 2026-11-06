"""The Postman collection covers every route the API serves, polls /ask until done, and holds no secret."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLLECTION = json.loads((ROOT / "postman" / "Research-Desk.postman_collection.json").read_text(encoding="utf-8"))


def _requests():
    return [(i["request"]["method"], "/" + "/".join(i["request"]["url"]["path"]), i) for i in COLLECTION["item"]]


def test_every_api_route_is_in_the_collection():
    from aws_lambda import api

    covered = set()
    for method, path, _ in _requests():
        concrete = re.sub(r"\{\{\w+_job\}\}", "0" * 32, path)
        for route_method, pattern, fn in api.ROUTES:
            if route_method == method and pattern.match(concrete):
                covered.add(fn.__name__)
    assert covered == {fn.__name__ for _, _, fn in api.ROUTES}
    assert ("GET", "/health") in {(m, p) for m, p, _ in _requests()}


def test_uses_variables_and_has_no_secret_values():
    variables = {v["key"]: v["value"] for v in COLLECTION["variable"]}
    assert variables["access_code"] == "" and "REPLACE" in variables["base_url"]
    for _, _, item in _requests():
        assert item["request"]["url"]["raw"].startswith("{{base_url}}/")
        for h in item["request"]["header"]:
            if h["key"] == "x-access-code":
                assert h["value"] == "{{access_code}}"


def test_ask_requests_are_polled_until_done():
    names = [i["name"] for i in COLLECTION["item"]]
    for mode in ("Quick", "Deep"):
        poll = next(i for i in COLLECTION["item"] if i["name"] == f"Poll {mode} answer")
        script = "\n".join(poll["event"][0]["script"]["exec"])
        assert "setNextRequest(pm.info.requestName)" in script and 'body.status === "pending"' in script
        assert "no context unless include_context" in script
        ask = next(n for n in names if n.startswith(f"Ask ({mode}"))
        assert names.index(f"Poll {mode} answer") == names.index(ask) + 1
