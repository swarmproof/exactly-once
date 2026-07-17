"""Email example — the context-manager form for an inline effect.

`with once(...) as guard:` is the ergonomic choice when the effect is a few lines
inline rather than a standalone function. `guard.fresh` tells you whether to run it;
on any replay the block is skipped and `guard.result` holds the stored value.

    python examples/email.py
"""

from __future__ import annotations

from exactly_once import Store, once

_sent: list[str] = []


def send_email(to: str, subject: str) -> str:
    message_id = f"msg_{len(_sent) + 1}"
    _sent.append(message_id)
    return message_id


def send_welcome_once(store: Store, user_id: str) -> str:
    # Key on the logical event, not on anything mutable.
    with once(store, key=f"welcome-email:{user_id}") as guard:
        if guard.fresh:
            guard.result = send_email(to=user_id, subject="Welcome!")
        return guard.result


def main() -> None:
    store = Store.memory()

    first = send_welcome_once(store, "user-4471")
    print(f"first  → sent {first}")

    # Called again on a retry / re-run: the email is NOT sent a second time.
    again = send_welcome_once(store, "user-4471")
    print(f"replay → {again} (skipped; no second email)")

    # A different user is a different key, so their welcome does send.
    other = send_welcome_once(store, "user-9999")
    print(f"other  → sent {other}")

    assert first == again
    assert _sent == [first, other], _sent
    print(f"\nemails actually sent: {_sent} — one per user. ✅")


if __name__ == "__main__":
    main()
