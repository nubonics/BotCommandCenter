"""Compatibility shim for the OSClient wall.

The wall now lives as a router integrated into the main FastAPI app.
Keep this module so older imports keep working while the app structure
moves away from a standalone wall service.
"""

from .router import *  # noqa: F401,F403
