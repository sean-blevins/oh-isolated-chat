"""Entry point: parse the managed-backend argv, find the store, and serve."""

from __future__ import annotations

import argparse
import threading
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


def _start_spawner(data_dir: Path) -> None:
    """Start the container-reachable spawn listener when deployment config exists.

    A missing ``spawner.json`` is normal -- background spawns are opt-in -- so it
    only logs. A malformed one logs why and leaves the feature off rather than
    taking down the whole backend.
    """
    path = config_path(data_dir)
    Handler.spawner_config_path = str(path)
    try:
        config = load_spawner_config(data_dir)
    except SpawnerConfigError as exc:
        print(f"[oh-isolated-chat] spawner disabled: {exc}", flush=True)
        return
    if config is None:
        print(
            f"[oh-isolated-chat] background spawns disabled (no {path})",
            flush=True,
        )
        return

    client = AgentServerClient(
        config.agent_server_url, config.session_key, config.request_timeout
    )
    Handler.spawner_config = config
    Handler.agent_client = client
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), SpawnHandler)
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
    _start_spawner(Path(args.data_dir))

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[oh-isolated-chat] listening on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
