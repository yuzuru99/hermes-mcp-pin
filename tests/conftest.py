import sys
from pathlib import Path

# Import the plugin as a package named ``mcp_pin`` (its folder name has a hyphen).
ROOT = Path(__file__).resolve().parents[1]
if "mcp_pin" not in sys.modules:
    import importlib.util

    spec = importlib.util.spec_from_file_location("mcp_pin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcp_pin"] = mod
    spec.loader.exec_module(mod)
