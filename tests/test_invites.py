import json
import time
import urllib.error
import urllib.request

import pytest

from memyselfandi.store import Store
from memyselfandi.supervisor import session_tools
from test_e2e import TOKEN, hub  # noqa: F401  (fixture)


def test_invite_lifecycle():
    s = Store(":memory:")
    inv = s.create_invite("alice", days=14)
    assert inv["token"].startswith("mmi_") and inv["active"]
    assert s.check_invite(inv["token"]) == "alice"
    assert s.check_invite("mmi_wrong") is None
    assert inv["token"] not in json.dumps(s._q("SELECT * FROM invites"))  # only the hash is stored
    s._x("UPDATE invites SET expires_at=? WHERE name='alice'", (time.time() - 1,))
    assert s.check_invite(inv["token"]) is None and not s.list_invites()[0]["active"]
    again = s.create_invite("alice", days=1)  # re-invite issues a fresh token
    assert s.check_invite(again["token"]) == "alice" and s.check_invite(inv["token"]) is None
    s.revoke_invite("alice")
    assert s.check_invite(again["token"]) is None and s.list_invites()[0]["revoked_at"]
    with pytest.raises(ValueError):
        s.create_invite("bob", days=0)


def req(url, path, token, body=b"{}"):
    r = urllib.request.Request(url + path, data=body, method="POST",
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_invite_tokens_against_live_hub(hub):  # noqa: F811
    url, _ = hub
    code, inv = req(url, "/api/admin/create_invite", TOKEN, b'{"name": "alice", "days": 7}')
    assert code == 200
    alice = inv["token"]
    assert req(url, "/api/tool/list_projects", alice)[0] == 200             # agent tools: ok
    assert req(url, "/api/admin/list_invites", alice)[0] == 403              # admin: no
    assert req(url, "/api/admin/create_invite", alice, b'{"name": "x", "days": 999}')[0] == 403
    assert req(url, "/api/tool/list_projects", "mmi_not-a-real-token")[0] == 401
    assert req(url, "/api/admin/revoke_invite", TOKEN, b'{"name": "alice"}')[0] == 200
    assert req(url, "/api/tool/list_projects", alice)[0] == 401              # revoked: out


def test_sessions_can_only_read_their_project(tmp_path):
    assert session_tools(str(tmp_path)) == f"mcp__memyselfandi,Read(/{tmp_path.resolve()}/**)"
    assert ",Read," not in session_tools(str(tmp_path)) + ","  # never a bare Read
