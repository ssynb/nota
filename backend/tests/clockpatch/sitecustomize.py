# Put on PYTHONPATH of service processes under test: starts nota's wall clock at TEST_NOW.
import os

_start = os.environ.get("TEST_NOW")
if _start:
    import time
    from datetime import datetime

    import nota.clock

    _base = datetime.fromisoformat(_start.replace("Z", "+00:00"))
    _booted = time.monotonic()

    def _now():
        from datetime import timedelta

        return _base + timedelta(seconds=time.monotonic() - _booted)

    nota.clock.now = _now
