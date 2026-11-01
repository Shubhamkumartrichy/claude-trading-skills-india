import importlib.util
import pathlib
import unittest
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_freshness", ROOT / "tools" / "check_freshness.py")
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)
spec = importlib.util.spec_from_file_location("make_demo", ROOT / "tools" / "make_demo.py")
md = importlib.util.module_from_spec(spec)
spec.loader.exec_module(md)


class TestFreshness(unittest.TestCase):
    def test_fresh_on_release_day(self):
        self.assertEqual(cf.check(date(2026, 10, 31)), [])

    def test_flags_old_rates_holidays_and_tax_year(self):
        problems = " ".join(cf.check(date(2027, 4, 15)))
        self.assertIn("Trading charges", problems)
        self.assertIn("holiday calendar", problems)
        self.assertIn("Tax year 2027-28", problems)

    def test_tax_year(self):
        self.assertEqual(cf.current_tax_year(date(2027, 3, 31)), "2026-27")
        self.assertEqual(cf.current_tax_year(date(2027, 4, 1)), "2027-28")


class TestDemo(unittest.TestCase):
    def test_svg_is_valid_and_keeps_columns(self):
        import xml.etree.ElementTree as ET
        svg = md.build([("Q & A?", "python3 x.py --a 1", ["Broker   Total", "Dhan  <₹1>"])])
        root = ET.fromstring(svg)
        self.assertTrue(root.tag.endswith("svg"))
        self.assertIn("Broker   Total", svg)       # spaces survive as NBSP
        self.assertIn("&lt;₹1&gt;", svg)

    def test_every_scene_command_runs(self):
        for _, cmd, _ in md.SCENES:
            with self.subTest(cmd=cmd.split()[1]):
                self.assertTrue(md.run(cmd))


if __name__ == "__main__":
    unittest.main()
