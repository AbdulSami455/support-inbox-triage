"""Cloudflare Python Worker entry point (free plan).

Static UI is served by Workers Assets; /api/* and /healthz reach this FastAPI app.
Settings come from Worker vars/secrets and storage is the D1 binding.
"""

from workers import WorkerEntrypoint, asgi

from app.config import load_settings
from app.db import D1DB
from app.main import create_app

_app = None
_db = None


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        global _app, _db
        env = self.env
        if _app is None:  # built on the first request: env is only available here
            def get(key):
                value = getattr(env, key, None)
                return value if isinstance(value, str) else None

            _db = D1DB(env.DB)
            _app = create_app(load_settings(get), db=_db)
        _db.binding = env.DB
        return await asgi.fetch(_app, request, env)
