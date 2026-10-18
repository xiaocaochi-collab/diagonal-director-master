import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('render_emotion_curve', ROOT / 'scripts' / 'render_emotion_curve.py')
curve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(curve)


class EmotionCurveTests(unittest.TestCase):
    def test_branding_changes_only_the_reserved_footer(self):
        with tempfile.TemporaryDirectory() as temporary:
            branded = Path(temporary) / 'branded.png'
            plain = Path(temporary) / 'plain.png'
            records = [dict(scene='第1场', emotion='紧张', entry=2, peak=4, exit=3)]
            curve.render(records, branded, '全片情绪曲线')
            curve.render(records, plain, '全片情绪曲线', branding=False)
            with Image.open(branded) as left, Image.open(plain) as right:
                bounds = ImageChops.difference(left, right).getbbox()
                self.assertIsNotNone(bounds)
                self.assertGreaterEqual(bounds[1], 1005)
                self.assertLess(bounds[3], curve.HEIGHT)

    def test_cli_can_export_without_branding(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source.md'
            output = Path(temporary) / 'cli.png'
            expected = Path(temporary) / 'expected.png'
            source.write_text('| 场次 | 主导情绪 | 入场强度 | 场内峰值 | 离场强度 |\n'
                              '| --- | --- | --- | --- | --- |\n'
                              '| 第1场 | 紧张 | 2 | 4 | 3 |\n', encoding='utf-8')
            subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/render_emotion_curve.py'),
                            str(source), str(output), '--no-branding'],
                           check=True, capture_output=True, text=True)
            curve.render(curve.parse_markdown(source), expected, '全片情绪曲线', branding=False)
            with Image.open(output) as left, Image.open(expected) as right:
                self.assertIsNone(ImageChops.difference(left, right).getbbox())

    def test_scene_labels_preserve_already_formatted_scene_numbers(self):
        self.assertEqual(curve.scene_label('第1场'), '第1场')
        self.assertEqual(curve.scene_label('2'), '第2场')
        self.assertEqual(curve.scene_label('回忆A'), '回忆A')

    def test_many_scenes_expand_the_canvas(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'curve.png'
            records = [dict(scene=f'第{index}场', emotion='紧张', entry=2, peak=4, exit=3)
                       for index in range(1, 21)]
            curve.render(records, output, '全片情绪曲线')
            with Image.open(output) as image:
                self.assertGreater(image.width, curve.WIDTH)
                self.assertEqual(image.height, curve.HEIGHT)
