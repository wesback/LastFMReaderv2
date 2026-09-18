import sys
import unittest


def main() -> int:
    suite = unittest.defaultTestLoader.discover(
        start_dir="tests",
        top_level_dir=".",
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
