"""Import a skill script by path (skills are folders, not packages)."""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def load(skill: str, script: str):
    path = ROOT / "skills" / skill / "scripts" / f"{script}.py"
    name = f"skill_{skill.replace('-', '_')}_{script}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
