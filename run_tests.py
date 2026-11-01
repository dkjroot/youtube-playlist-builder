"""Run the offline checks from any working directory, with a failing exit status."""

import os
from pathlib import Path
import sys
import unittest


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    sys.path.insert(0, str(root / 'src'))
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if result.wasSuccessful():
        print(f'Passed {result.testsRun} checks ({len(result.skipped)} skipped).')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
