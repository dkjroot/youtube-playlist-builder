import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from youtube_playlist_builder.videos import duration_seconds
from youtube_playlist_builder.cli import main
from youtube_playlist_builder.state import State


class VideoTests(unittest.TestCase):
    def test_durations_and_unknown_values(self):
        for value, expected in [('PT3M',180), ('PT3M1S',181), ('PT1H2M3S',3723),
                                ('P1DT2H',93600), ('PT0S',0), ('PT1.5S',1.5),
                                (None,None), ('',None), ('P',None), ('PT',None), ('invalid',None)]:
            with self.subTest(value=value):
                self.assertEqual(duration_seconds(value), expected)

    def test_setting_persists_and_can_be_disabled(self):
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            path = Path(directory)/'state.db'
            for number in ('181','0'):
                self.assertEqual(main(['--state',str(path),'settings','--minimum-video-length',number]),0)
                state = State(path)
                self.assertEqual(state.meta('minimum_video_seconds'),int(number))
                state.close()
            with self.assertRaises(SystemExit):
                main(['--state',str(path),'settings','--minimum-video-length','-1'])
