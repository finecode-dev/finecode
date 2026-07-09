"""Regression tests for the METHOD_TO_TYPES / feature() registration contract.

Bug context: ``er/userMessage`` was registered as a JSON-RPC client ``feature()``
handler but was **missing** from ``METHOD_TO_TYPES``. The client
(``finecode_jsonrpc.client``) gates and deserializes every incoming message against
that table, dropping any method absent from it with only a warning — so the handler
never fired and ER-originated user messages never reached any client.

These tests lock the fix and guard against the same class of omission for other
notification types.
"""
from __future__ import annotations

import inspect

from finecode_jsonrpc._converter import converter

from finecode.wm_server.runner import _internal_client_types as t


def test_er_user_message_is_registered() -> None:
    assert t.ER_USER_MESSAGE in t.METHOD_TO_TYPES, (
        "er/userMessage missing from METHOD_TO_TYPES — the client drops it at its "
        "message_types gate and on_er_user_message never fires"
    )
    notif_type, params_type, resp_type, result_type = t.METHOD_TO_TYPES[t.ER_USER_MESSAGE]
    assert notif_type is t.ErUserMessageNotification
    assert params_type is t.ErUserMessageParams
    assert resp_type is None and result_type is None  # it is a notification


def test_er_user_message_incoming_structures_to_params() -> None:
    # Mirrors client.py's incoming-notification path (``_converter.structure(message,
    # notification_type)``): the registered type must deserialize a real payload so the
    # handler receives usable ``.params``.
    msg = {
        "jsonrpc": "2.0",
        "method": t.ER_USER_MESSAGE,
        "params": {"message": "install failed", "type": "ERROR"},
    }
    notif_type = t.METHOD_TO_TYPES[t.ER_USER_MESSAGE][0]
    notif = converter.structure(msg, notif_type)
    assert notif.params.message == "install failed"
    assert notif.params.type == "ERROR"


def test_every_notification_type_is_registered() -> None:
    """Generalizable guard: any concrete ``BaseNotification`` subclass that declares a
    string ``method`` must appear in ``METHOD_TO_TYPES``. Sending and receiving both
    consult that table, so an unregistered notification type is a latent
    silently-dropped-message bug (the er/userMessage class of defect)."""
    registered = set(t.METHOD_TO_TYPES.keys())
    missing: list[str] = []
    for name, obj in vars(t).items():
        if (
            inspect.isclass(obj)
            and issubclass(obj, t.BaseNotification)
            and obj is not t.BaseNotification
        ):
            method = getattr(obj, "method", None)
            if isinstance(method, str) and method not in registered:
                missing.append(f"{name}(method={method!r})")
    assert not missing, (
        "Notification types defined but not in METHOD_TO_TYPES (client would drop "
        f"them): {', '.join(missing)}"
    )
