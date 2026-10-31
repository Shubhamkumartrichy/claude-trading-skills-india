"""Static checks on every SKILL.md: frontmatter, naming, description length, referenced paths."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILLS = sorted(p.parent for p in (ROOT / "skills").glob("*/SKILL.md"))


def frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    fm, key, buf = {}, None, []
    for line in m.group(1).splitlines():
        km = re.match(r"^([a-z_-]+):\s*(.*)$", line)
        if km and not line.startswith(" "):
            if key:
                fm[key] = "\n".join(buf).strip()
            key, buf = km.group(1), [km.group(2).strip("|").strip()]
        else:
            buf.append(line.strip())
    if key:
        fm[key] = "\n".join(buf).strip()
    return fm


class TestSkills(unittest.TestCase):
    def test_found_all(self):
        self.assertEqual(len(SKILLS), 23)

    def test_frontmatter(self):
        for d in SKILLS:
            fm = frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
            with self.subTest(skill=d.name):
                self.assertEqual(fm.get("name"), d.name)
                desc = fm.get("description", "")
                self.assertGreater(len(desc), 100)
                self.assertLessEqual(len(desc), 1024, f"{d.name} description is {len(desc)} chars")

    def test_referenced_paths_exist(self):
        pat = re.compile(r"((?:\.\./[a-z-]+/)?(?:scripts|references)/[A-Za-z0-9_.-]+\.(?:py|md))")
        for d in SKILLS:
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            for ref in set(pat.findall(text)):
                with self.subTest(skill=d.name, ref=ref):
                    self.assertTrue((d / ref).resolve().exists(), f"{d.name}: missing {ref}")

    def test_cross_skill_mentions_exist(self):
        names = {d.name for d in SKILLS}
        pat = re.compile(r"`([a-z]+(?:-[a-z]+)+)`")
        for d in SKILLS:
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            for ref in set(pat.findall(text)):
                if ref.endswith(("-pct", "-size", "-lot")) or ref in {"mcp-remote", "kite-mcp-server", "upstox-skill"}:
                    continue
                if any(ref == n for n in names) or "-" not in ref:
                    continue
                looks_like_skill = any(ref.split("-")[0] == n.split("-")[0] for n in names)
                if looks_like_skill and ref not in names:
                    self.fail(f"{d.name} mentions unknown skill `{ref}`")


if __name__ == "__main__":
    unittest.main()
