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
