# Examples

Runnable, dependency-free (each uses an in-memory fake for the external service).

| File | Shows |
|---|---|
| [`crash_mid_payment.py`](./crash_mid_payment.py) | **The flagship.** An agent crashes mid-payment, resumes, and does *not* double-charge — WITH vs WITHOUT exactly-once, side by side. This is the demo. |
| [`payment.py`](./payment.py) | The `@once` decorator with **provider-key passthrough** — the recommended pattern for money movement. |
| [`email.py`](./email.py) | The `with once(...) as guard:` context manager for an inline effect. |

```bash
python examples/crash_mid_payment.py
python examples/payment.py
python examples/email.py
```

Each keys on **business identity** (`order_id`, `user_id`), never on a mutable value
like an amount — see the anti-pattern note in the [README](../README.md).
