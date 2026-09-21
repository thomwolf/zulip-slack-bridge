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


def test_deployment_check_is_read_only_and_hides_errors(monkeypatch):
    from unittest.mock import Mock

    from chat_bridge import space_app

    connection = Mock()
    connection.execute.return_value.fetchone.return_value = [1]
    monkeypatch.setattr(space_app.TursoConnection, "connect", Mock(return_value=connection))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "synthetic-private-value")
    monkeypatch.setenv("SLACK_APP_TOKEN", "synthetic-private-value")
    checks = space_app.deployment_checks()
    assert checks["Turso connection"] is True
    assert checks["Slack credentials"] is True
    connection.execute.assert_called_once_with("SELECT 1")
    connection.close.assert_called_once()
    connection.execute.side_effect = RuntimeError("synthetic-private-value")
    checks = space_app.deployment_checks()
    assert checks["Turso connection"] is False
    assert "synthetic-private-value" not in str(checks)


def test_live_readiness_tracks_socket_and_worker(monkeypatch):
    from unittest.mock import Mock

    from chat_bridge import space_app

    runtime = Mock()
    runtime.stop.is_set.return_value = False
    runtime.store.get.return_value = {"zulip": "connected"}
    live = Mock(runtimes=[runtime])
    live.socket.is_connected.return_value = True
    monkeypatch.setattr(space_app, "LIVE", live)
    monkeypatch.setattr(space_app, "MODE", "live")
    assert response("/readyz")[0] == 200
    live.socket.is_connected.return_value = False
    assert response("/readyz")[0] == 503
    live.socket.is_connected.return_value = True
    runtime.stop.is_set.return_value = True
    assert response("/readyz")[0] == 503
    runtime.stop.is_set.return_value = False
    runtime.store.get.return_value = {"zulip": "connection_retrying"}
    assert response("/readyz")[0] == 503
    runtime.store.get.side_effect = RuntimeError("secret-detail")
    assert response("/readyz")[0] == 503
    assert b"secret-detail" not in response("/")[2]
