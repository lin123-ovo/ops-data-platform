"""Run offline regression tests. Any unmocked embedding request fails before network I/O."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent))
    with patch('dashscope.TextEmbedding.call', side_effect=AssertionError('真实模型调用已被测试入口禁止')), \
         patch('dashscope.Generation.call', side_effect=AssertionError('真实聊天模型调用已被测试入口禁止')):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(not result.wasSuccessful())
