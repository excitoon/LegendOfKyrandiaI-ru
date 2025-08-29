import os
import unittest

import tools.package


class TestPackage(unittest.TestCase):
    def test_read(self):
        path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = [
            'A_E.PAK',
            'DAT.PAK',
            'F_L.PAK',
            'MAP_5.PAK',
            'MSC.PAK',
            'M_S.PAK',
            'S_Z.PAK',
            'WSA1.PAK',
            'WSA2.PAK',
            'WSA3.PAK',
            'WSA4.PAK',
            'WSA5.PAK',
            'WSA6.PAK',
        ]
        for file in files:
            with self.subTest(file=file):
                with open(os.path.join(path, 'original', file), 'rb') as f:
                    data = f.read()
                content = tools.package.decode(data)
                self.assertGreater(len(content), 0)
                for name, chunk in content.items():
                    self.assertGreater(len(name), 0)
                    self.assertGreater(len(chunk), 0)
                    with open(os.path.join(path, 'unpacked', name), 'rb') as f:
                        self.assertEqual(chunk, f.read())
