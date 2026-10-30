"""Host-side backend for the oh-isolated-chat Canvas App.

Runs as a host process owned by the Agent Server (a Canvas App managed backend)
and never inside a conversation container. Its job is the host half of isolated
conversations: promote their memory into the durable store the host seeds new
conversations from, and report the state the page renders.

Standard library only. The browser-facing assets live beside this package in
``web/`` and are served by :mod:`isolated_chat_backend.server`.
"""
