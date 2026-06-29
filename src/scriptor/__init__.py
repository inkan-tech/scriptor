"""
Inkan Doc - A comprehensive document management system.

This package provides tools for creating, building, and managing professional 
documents with Pandoc integration and reMarkable synchronization.
"""

__version__ = "0.1.10"
__author__ = "Inkan"
__email__ = "contact@inkan.tech"

from .config import ProjectConfig
from .pandoc_utils import build_document
from .project_scaffolding import create_new_project

__all__ = [
    "ProjectConfig",
    "build_document",
    "create_new_project",
    "__version__",
    "notebook",
]
