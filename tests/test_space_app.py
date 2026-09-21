import json

from chat_bridge.space_app import response


def test_liveness_does_not_claim_forwarding_readiness():
    status, _, data = response("/healthz")
    assert status == 200
    assert json.loads(data)["forwarding"] is False
    status, _, data = response("/readyz")
    assert status == 503
    assert json.loads(data)["mode"] == "setup"


def test_setup_server_has_no_credentials_or_config_endpoint(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "secret-not-for-browser")
    assert b"secret-not-for-browser" not in response("/")[2]
    for path in ("/.env", "/config", "/status", "/?token=secret-not-for-browser"):
        assert response(path)[0] == 404
