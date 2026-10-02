"""
Compatibility re-export layer.

This module provides backward-compatible imports for AppService and related classes.
All actual implementation lives in app_service_core.py. This wrapper exists solely
for import path compatibility with older code that imports from 'app_service'.
"""

# autopep8: off
# isort: off

from __future__ import annotations

from app_service_core import AppService, StrmStorageInfo, StrmStorageManager

__all__ = ["AppService", "StrmStorageInfo", "StrmStorageManager"]

# autopep8: on
# isort: on
