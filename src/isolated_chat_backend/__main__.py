"""Entry point: parse the managed-backend argv, find the store, and serve."""

from __future__ import annotations

import argparse
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

from .persistence import find_persistence_root
from .server import Handler, SpawnHandler
from .spawner import (
    AgentServerClient,
    SpawnerConfigError,
    config_path,
    load_spawner_config,
)


def _start_spawner(data_dir: Path, root: Path) -> None:
    """Start the container-reachable spawn listener when config can be resolved.

    Configuration is discovered from the Agent Server process by default, so no
    ``spawner.json`` is needed; a file only overrides an exotic deployment. A
    malformed file or a discovery failure logs and leaves the feature off rather
    than taking down the whole backend.
    """
    path = config_path(data_dir)
    Handler.spawner_config_path = str(path)
    try:
        config = load_spawner_config(data_dir, root)
    except SpawnerConfigError as exc:
        print(f"[oh-isolated-chat] spawner disabled: {exc}", flush=True)
        return
    if config is None:
        print(
            "[oh-isolated-chat] background spawns disabled "
            "(no session key discovered and no spawner.json)",
            flush=True,
        )
        return

    Handler.spawner_source = "file" if path.is_file() else "discovered"
    client = AgentServerClient(
        config.agent_server_url, config.session_key, config.request_timeout
    )
    Handler.spawner_config = config
    Handler.agent_client = client
    try:
        server = ThreadingHTTPServer(
            (config.listen_host, config.listen_port), SpawnHandler
        )
    except OSError as exc:
        # The default port may be taken by something else; the URL baked into a
        # campaign's orchestrator profile is only reachable if the port is
        # stable, so a fallback is a degraded state, not a normal one.
        print(
            f"[oh-isolated-chat] spawner port {config.listen_port} unavailable "
            f"({exc}); falling back to an ephemeral port",
            flush=True,
        )
        server = ThreadingHTTPServer((config.listen_host, 0), SpawnHandler)
        config = replace(config, listen_port=server.server_address[1])
        Handler.spawner_config = config
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(
        "[oh-isolated-chat] spawner listening on "
        f"{config.listen_host}:{config.listen_port} "
        f"(containers reach {config.base_url}{config.spawn_path})",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="isolated chat backend")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()

    root = find_persistence_root(Path(args.data_dir))
    print(f"[oh-isolated-chat] persistence root: {root}", flush=True)
    print(f"[oh-isolated-chat] data dir: {args.data_dir}", flush=True)

    Handler.root = root
    _start_spawner(Path(args.data_dir), root)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[oh-isolated-chat] listening on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
