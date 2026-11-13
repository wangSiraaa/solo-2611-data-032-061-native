"""统一测试入口：python -m tests.run_tests"""

import sys
import unittest

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_planopt")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
