"""Entry point: parse the managed-backend argv, find the store, and serve."""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

from .persistence import find_persistence_root
from .server import Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="isolated chat backend")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()

    root = find_persistence_root(Path(args.data_dir))
    print(f"[oh-isolated-chat] persistence root: {root}", flush=True)
    print(f"[oh-isolated-chat] data dir: {args.data_dir}", flush=True)

    Handler.root = root
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[oh-isolated-chat] listening on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
