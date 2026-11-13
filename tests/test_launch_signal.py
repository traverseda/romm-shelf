"""Steam stops a game by signalling the process group: `play` must survive it to push the saves."""

import os
import signal
import sys
import threading
import time

from romm_shelf import native


def test_a_stop_signal_reaches_the_game_and_launch_still_returns(tmp_path):
    script = tmp_path / "game.py"
    script.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *a: sys.exit(7))\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    ready = tmp_path / "out.txt"
    plan = native.RunPlan(program=sys.executable, args=(str(script),), env={}, cwd=tmp_path, seed={})
    old = signal.getsignal(signal.SIGINT)
    try:
        def stop():
            for _ in range(100):
                if ready.exists() and "ready" in ready.read_text():
                    break
                time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGINT)  # what Steam's "Exit game" does

        threading.Thread(target=stop, daemon=True).start()
        with ready.open("w") as out:
            code = native.launch(plan, output=out)
    finally:
        signal.signal(signal.SIGINT, old)
    assert code == 7  # the game got the signal, and launch returned instead of raising KeyboardInterrupt
