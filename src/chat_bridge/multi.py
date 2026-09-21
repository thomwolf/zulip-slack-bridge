"""One Slack socket dispatches to isolated pairs sharing a bot on each platform."""

import threading
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any
from urllib.parse import urlparse

from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse

from .adapters import LiveTransport
from .config import Config, secret
from .runtime import Runtime
from .store import Store
from .turso import TursoConnection


def open_store(cfg: Config) -> Store:
    """Open the selected pair's database without exposing credentials."""
    connection = (
        TursoConnection.connect(secret(cfg.turso_url_env), secret(cfg.turso_token_env))
        if cfg.storage_backend == "turso"
        else None
    )
    return Store(cfg.database, connection)


class MultiRuntime:
    """Route by workspace/channel before acknowledgement, with independent workers."""

    def __init__(self, runtimes: list[Runtime], socket: Any) -> None:
        self.runtimes = runtimes
        self.socket = socket
        self.routes = {(r.cfg.slack_team, r.cfg.slack_channel): r for r in runtimes}
        if len(self.routes) != len(runtimes):
            raise ValueError("Duplicate Slack channel routing")
        socket.socket_mode_request_listeners.append(self.receive_slack)

    def receive_slack(self, client: Any, request: Any) -> None:
        """Commit only to the matching pair; unknown channels have no stored payload."""
        payload = request.payload or {}
        raw = payload.get("event", {})
        route = self.routes.get(
            (
                str(payload.get("team_id", "")),
                str(raw.get("channel", raw.get("item", {}).get("channel", ""))),
            )
        )
        if request.type == "events_api" and route:
            route.receive_slack(client, request)
        else:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))

    def run(
        self, accept_gap: bool = False, observe: Callable[["MultiRuntime"], None] | None = None
    ) -> None:
        """Prepare all queues first; a failed pair does not stop another worker."""
        threads = []

        def worker(runtime: Runtime) -> None:
            try:
                runtime.run(prepared=True, manage_socket=False)
            except Exception:
                runtime.stop.set()
                print(f"Pair {runtime.cfg.pair_id} stopped; inspect its status.", flush=True)

        try:
            for runtime in self.runtimes:
                runtime.prepare(accept_gap)
            for runtime in self.runtimes:
                thread = threading.Thread(target=worker, args=(runtime,), daemon=True)
                thread.start()
                threads.append(thread)
            self.socket.connect()
            if observe:
                observe(self)
            while any(t.is_alive() for t in threads):
                threads[0].join(timeout=0.5)
                # The first pair can stop while another stays healthy.
                if not threads[0].is_alive():
                    threading.Event().wait(0.5)
            raise RuntimeError("All pair workers stopped; inspect status")
        finally:
            self.socket.close()
            for runtime in self.runtimes:
                runtime.stop.set()
                runtime.store.wakeup.set()
            for thread in threads:
                thread.join()


def run_pairs(
    configs: list[Config],
    command: str,
    accept_gap: bool = False,
    observe: Callable[[MultiRuntime], None] | None = None,
) -> dict:
    """Operate a group with isolated databases and one shared Slack ingress."""
    if command not in {"check", "run", "status"}:
        raise ValueError("Select --pair for retry, resolve, or release-owner")
    # Different env names can still point at the same database: reject before opening.
    remote_hosts = [
        urlparse(secret(c.turso_url_env)).hostname for c in configs if c.storage_backend == "turso"
    ]
    if len(remote_hosts) != len(set(remote_hosts)):
        raise ValueError("Each pair must use a different Turso database")
    results = {}
    with ExitStack() as stack:
        stores = []
        for cfg in configs:
            store = open_store(cfg)
            stores.append(store)
            stack.callback(store.close)
        if command == "status":
            return {c.pair_id: s.status() for c, s in zip(configs, stores, strict=True)}
        for store in stores:
            stack.enter_context(store.exclusive())
        transports = []
        identities = []
        for cfg, store in zip(configs, stores, strict=True):
            transport = LiveTransport(cfg)
            identity = transport.preflight()
            store.set("zulip_policy", identity["zulip_policy"])
            results[cfg.pair_id] = identity
            if command == "run":
                store.bind(
                    {
                        **cfg.identity(),
                        "slack_bot": identity["slack_bot"],
                        "zulip_bot": identity["zulip_bot"],
                    }
                )
            transports.append(transport)
            identities.append(identity)
        if command == "check":
            return results
        socket = SocketModeClient(
            app_token=secret("SLACK_APP_TOKEN"), web_client=transports[0].slack, concurrency=1
        )
        runtimes = [
            Runtime(c, s, t, i, socket=socket)
            for c, s, t, i in zip(configs, stores, transports, identities, strict=True)
        ]
        MultiRuntime(runtimes, socket).run(accept_gap, observe)
    return results
