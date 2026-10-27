"""Client import service facade redirecting to core_bot.services.client_import."""

import sys
import core_bot.services.client_import as _mod

sys.modules[__name__] = _mod
