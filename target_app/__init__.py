"""LegacyCore: a deliberately hostile mock core-banking app used as the automation target.

All data is fake and generated from a fixed seed.
"""

from target_app.app import create_app

__all__ = ["create_app"]
