"""Entry point: parse the managed-backend argv, find the store, and serve."""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import notes
from .persistence import find_persistence_root
from .server import Handler
from .spawner import SpawnerConfigError, Watcher, load_config


def start_watcher(root: Path, data_dir: Path) -> Watcher:
    """Start the background watcher: notes capture always, spawns when possible.

    The watcher has two jobs. Notes capture needs nothing but the host
    filesystem -- a conversation's ``NOTES.md`` is already bind-mounted into its
    runtime directory -- so it always runs. Spawning a conversation needs Agent
    Server access, discovered from the parent process (with an optional
    ``spawner.json`` override); when there is nothing to discover, spawns are
    disabled and ``/api/spawner`` reports why, but capture keeps working.
    """
    try:
        config = load_config(data_dir, root)
    except SpawnerConfigError as exc:
        print(f"[oh-isolated-chat] spawns disabled: {exc}", flush=True)
        config = None
    if config is None:
        print(
            "[oh-isolated-chat] spawns disabled: no Agent Server credentials "
            "discovered (set <data-dir>/spawner.json to enable); notes capture "
            "still on",
            flush=True,
        )
    watcher = Watcher(
        root,
        config,
        logger=lambda message: print(f"[oh-isolated-chat] {message}", flush=True),
    )
    watcher.start()
    target = (
        f"via {config.agent_server_url}" if config is not None else "(capture only)"
    )
    print(
        f"[oh-isolated-chat] watcher on {root / 'runtime-data'}/* "
        f"(notes capture + spawns {target})",
        flush=True,
    )
    return watcher


def main() -> None:
    parser = argparse.ArgumentParser(description="isolated chat backend")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()

    root = find_persistence_root(Path(args.data_dir))
    print(f"[oh-isolated-chat] persistence root: {root}", flush=True)
    print(f"[oh-isolated-chat] data dir: {args.data_dir}", flush=True)

    migrated = notes.migrate_legacy_store(root)
    if migrated:
        print(
            "[oh-isolated-chat] migrated legacy memory store to notes: "
            + ", ".join(migrated),
            flush=True,
        )

    Handler.root = root
    Handler.watcher = start_watcher(root, Path(args.data_dir))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[oh-isolated-chat] listening on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
