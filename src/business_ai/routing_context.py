"""A plain, mutable attribute bag shared across router modules (Phase 9).

`create_app()` builds one `RouteContext` instance and passes it to every
`register_*(app, svc, ctx)` function in `business_ai.routers`. Each
module that owns a cross-cutting helper (today, only `admin_bot.py`)
attaches it to `ctx` as an attribute; every other module reads it back
the same way (`ctx._resolve(...)`, `ctx._process_question(...)`, etc).

This works regardless of registration order because attribute lookups on
`ctx` happen at REQUEST time (inside a route handler), long after
`create_app()` has finished wiring every router — not at registration
time. It deliberately avoids FastAPI's dependency-injection machinery
here: every route in this codebase was written as a closure capturing
`svc` directly (a deliberate original design choice — see Services'
own docstring), and this object is the smallest change that lets that
pattern span multiple files instead of one, without rewriting every
route's signature.
"""

from __future__ import annotations


class RouteContext:
    pass
