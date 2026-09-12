from __future__ import annotations

import importlib
import sys

MODULES = ['app.core.helpers', 'app.storage.database', 'app.features.manual_queue', 'app.storage.profiles', 'app.features.parsers', 'app.features.ping', 'app.services.scraper_poster', 'app.services.pipeline', 'app.ui.keyboards', 'app.handlers.telegram_handlers', 'app.core.lifecycle']

def load_all_modules():
    loaded=[]
    for name in MODULES:
        loaded.append(importlib.import_module(name))
    # Merge every module's public and private globals into a shared compatibility
    # namespace. Functions keep their own module __globals__, so this makes
    # cross-module references resolve without requiring circular imports.
    merged={}
    for mod in loaded:
        merged.update(vars(mod))
    runtime=importlib.import_module('app.core.runtime')
    merged.update(vars(runtime))
    for mod in loaded:
        vars(mod).update(merged)
    return loaded, merged

