"""Terminal interaction helpers shared by the menu and slow operations.

Prompt-toolkit supplies editing and Escape cancellation. Human progress goes to
TTY stderr so JSON and redirected stdout remain usable. The waiting heartbeat
only prints elapsed time; API calls and SQLite stay on their calling thread.
"""
from contextlib import contextmanager
import sys
import os
import threading
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings


class MenuCancelled(Exception):
    """Leave the current action without submitting its unfinished input."""


def prompt_session(**kwargs):
    """Create a line editor whose Escape key raises MenuCancelled without needing Enter."""
    bindings = KeyBindings()

    @bindings.add('escape', eager=True)
    def cancel(event):
        """Exit the current input application, allowing the menu loop to catch cancellation."""
        event.app.exit(exception=MenuCancelled())

    session = PromptSession(key_bindings=bindings, **kwargs)
    session.app.ttimeoutlen = 0.1
    return session


def ask(message):
    """Use editable prompts on a terminal and plain input for redirected/scripted menu usage."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        return prompt_session().prompt(message)
    # Keep redirected input and existing scripted menu usage working.
    value = input(message)
    if '\x1b' in value:
        raise MenuCancelled()
    return value


def progress(message):
    """Emit flushed human progress only to terminal stderr, keeping JSON stdout parseable."""
    if sys.stderr.isatty() or os.environ.get('YT_PLAYLISTS_PROGRESS') == '1':
        print(message, file=sys.stderr, flush=True)


@contextmanager
def waiting(message):
    """Keep slow requests visibly alive, without altering JSON/piped stdout."""
    if not (sys.stderr.isatty() or os.environ.get('YT_PLAYLISTS_PROGRESS') == '1'):
        yield
        return
    progress(message)
    stop = threading.Event()
    started = time.monotonic()

    def heartbeat():
        """Report elapsed waiting time only; this background thread never accesses state or APIs."""
        while not stop.wait(10):
            progress(f'{message} — still waiting ({int(time.monotonic() - started)}s)')

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
