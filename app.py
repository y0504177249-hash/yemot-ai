"""Compatibility entrypoint for Render services configured as gunicorn app:app."""
from server import app

__all__ = ["app"]
