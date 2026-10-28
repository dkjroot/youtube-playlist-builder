import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from youtube_playlist_builder.terminal import prompt_session, MenuCancelled, waiting, progress
from youtube_playlist_builder.menu import run_menu
from youtube_playlist_builder.state import State


class TerminalTests(unittest.TestCase):
    def test_escape_cancels_without_enter(self):
        with create_pipe_input() as pipe:
            session = prompt_session(input=pipe, output=DummyOutput())
            pipe.send_text('unfinished\x1b')
            with self.assertRaises(MenuCancelled):
                session.prompt('Value: ')

    def test_arrow_editing_is_not_treated_as_escape(self):
        with create_pipe_input() as pipe:
            session = prompt_session(input=pipe, output=DummyOutput())
            pipe.send_text('ac\x1b[Db\r')
            self.assertEqual(session.prompt('Value: '), 'abc')

    def test_cancel_nested_edit_returns_to_menu_without_update(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(state=Path(directory)/'state.db', token=Path('token'),
                                   client_secrets=Path('client'), llm_config=Path('llm'))
            state = State(args.state)
            state.add('chess')
            state.close()
            calls = []
            with patch('youtube_playlist_builder.menu.ask', side_effect=['3', '1', '3', MenuCancelled(), '0']), patch('builtins.print'):
                self.assertEqual(run_menu(args, lambda words: calls.append(words)), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-3:], ['topics', 'show', 'chess'])

    def test_cancel_settings_does_not_save_partial_form(self):
        args = SimpleNamespace(state=Path('unused.db'), token=Path('token'),
                               client_secrets=Path('client'), llm_config=Path('llm'))
        calls = []
        with patch('youtube_playlist_builder.menu.ask', side_effect=['14', '1000', MenuCancelled(), '0']), patch('builtins.print'):
            run_menu(args, lambda words: calls.append(words))
        self.assertEqual(len(calls), 1)  # Only the initial settings display.
        self.assertEqual(calls[0][-1], 'settings')

    def test_progress_does_not_contaminate_redirected_output(self):
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            progress('invisible')
            with waiting('also invisible'):
                pass
        self.assertEqual(stderr.getvalue(), '')

    def test_waiting_reports_stage_and_cleans_up_on_failure(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        output = Terminal()
        before = set(threading.enumerate())
        with patch('sys.stderr', output):
            with self.assertRaises(ValueError):
                with waiting('Reading channels…'):
                    raise ValueError('network failure')
        self.assertIn('Reading channels', output.getvalue())
        self.assertEqual(set(threading.enumerate()), before)
