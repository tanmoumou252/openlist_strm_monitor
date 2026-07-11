"""Bootstrap utilities - shared startup functions to avoid duplication."""

import os
import sys

# BASE_DIR = src/ directory (code directory)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_base_dir_first() -> None:
    """Ensure BASE_DIR is the first entry in sys.path to avoid module conflicts."""
    normalized_base_dir = os.path.normcase(os.path.abspath(BASE_DIR))
    sys.path[:] = [
        p
        for p in sys.path
        if os.path.normcase(os.path.abspath(p or os.getcwd())) != normalized_base_dir
    ]
    sys.path.insert(0, BASE_DIR)


def load_local_module(module_name: str, filename: str,
                      base_dir: str | None = None):
    """Load a local Python module from a file path.

    Args:
        module_name: Name to register the module under in sys.modules
        filename: Python filename to load
        base_dir: Base directory to look for the file (defaults to src/)

    Returns:
        The loaded module
    """
    import importlib.util

    if base_dir is None:
        base_dir = BASE_DIR
    module_path = os.path.join(base_dir, filename)
    if not os.path.isfile(module_path):
        raise FileNotFoundError(f"Local module file not found: {module_path}")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Cannot create module load spec: {module_name} ({module_path})")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
