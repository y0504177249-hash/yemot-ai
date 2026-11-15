"""Assistant-specific prompt modules. Each assistant lives in its own file."""
from importlib import import_module

ASSISTANT_MODULES = {
    "smart": "smart",
    "edgy": "edgy",
    "psychologist": "psychologist",
    "yeshiva": "yeshiva",
    "yiddish": "yiddish",
    "tools": "tools",
    "astrology": "astrology",
    "central": "central",
}

def get_prompt(mode, yiddish=False):
    key = mode if mode in ASSISTANT_MODULES else ("yiddish" if yiddish else "smart")
    module = import_module(f".{ASSISTANT_MODULES[key]}", __name__)
    return module.get_prompt(yiddish=yiddish) if hasattr(module, "get_prompt") else module.PROMPT
