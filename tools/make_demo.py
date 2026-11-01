#!/usr/bin/env python3
"""Render the README demo: an animated terminal SVG built from real calculator output.

Runs each calculator, captures its output, and writes a looping SVG (SMIL animation, no
JavaScript) that GitHub plays inline. Re-run after changing rates so the demo never drifts
from what the scripts actually print.

  python3 tools/make_demo.py                         # writes docs/assets/demo.svg
  python3 tools/make_demo.py --out /tmp/demo.svg
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parents[1]

# (question the user asks Claude, command Claude runs, which output lines to keep)
SCENES = [
    ("Buy 200 INFY at 1,480, sell at 1,520. Which broker is cheapest?",
     "python3 skills/trading-costs/scripts/charges.py --segment delivery --buy 1480 --sell 1520 "
     "--qty 200 --broker all",
     None),
    ("When are the next NIFTY expiries?",
     "python3 skills/fno-options-analysis/scripts/options.py expiries NIFTY --from 2026-11-01 --count 6",
     None),
    ("18L salary, 60k STCG, 2.4L LTCG, 1.1L F&O loss. My tax?",
     "python3 skills/indian-tax-for-traders/scripts/tax.py estimate --salary 1800000 --stcg 60000 "
     "--ltcg 240000 --fno -110000",
     lambda ls: [ln for ln in ls if ln.strip() and "ITR-3" not in ln and "87A rebate is not" not in ln]),
]

FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', 'Liberation Mono', monospace"
SIZE, LINE, CHAR_W = 13, 19, 7.9          # font px, line height, approx. monospace advance
PAD_X, TOP = 20, 52
TYPE_PER_CHAR, TYPE_MAX = 0.025, 2.2      # seconds
LINE_STEP, HOLD = 0.06, 5.0

NBSP = "\u00a0"                             # browsers collapse plain spaces in SVG text; tables need them
BG, FG, DIM, GREEN, AMBER, ACCENT = "#0d1117", "#e6edf3", "#8b949e", "#3fb950", "#d29922", "#58a6ff"


def run(cmd: str) -> list[str]:
    argv = cmd.split()
    argv[0] = sys.executable
    out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return out.rstrip("\n").splitlines()


def colour(line: str) -> str:
    s = line.strip()
    if s.startswith("⚠"):
        return AMBER
    if s.startswith("TOTAL") or "moved: holiday" in s or s.startswith("•"):
        return GREEN
    if set(s) <= set("-") or s.startswith(("Broker ", "Statutory")):
        return DIM
    return FG


def build(scenes: list[tuple[str, str, list[str]]]) -> str:
    rows = max(len(lines) for _, _, lines in scenes) + 3
    cols = max(max(len(c) + 2 for _, c, _ in scenes),
               max(len(ln) for _, _, lines in scenes for ln in lines))
    width = int(PAD_X * 2 + cols * CHAR_W)
    height = TOP + rows * LINE + 16

    # Timeline: every element animates over the same loop, so they never drift apart.
    spans, t = [], 0.0
    for q, cmd, lines in scenes:
        typing = min(TYPE_MAX, len(cmd) * TYPE_PER_CHAR)
        start, typed = t, t + 0.6 + typing
        end = typed + 0.3 + len(lines) * LINE_STEP + HOLD
        spans.append((start, typed, end))
        t = end
    total = t

    def kt(x: float) -> str:
        return f"{x / total:.5f}"

    def visible(on: float, off: float) -> str:
        return (f'<animate attributeName="opacity" dur="{total:.2f}s" repeatCount="indefinite" '
                f'calcMode="discrete" values="0;1;0" keyTimes="0;{kt(on)};{kt(off)}"/>')

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" role="img" aria-label="India Trading Skills calculators: '
           f'broker charges, NIFTY expiries and tax estimate">',
           f'<title>India Trading Skills calculators</title>',
           f'<rect width="{width}" height="{height}" rx="10" fill="{BG}"/>',
           '<circle cx="20" cy="18" r="6" fill="#ff5f56"/><circle cx="40" cy="18" r="6" fill="#ffbd2e"/>'
           '<circle cx="60" cy="18" r="6" fill="#27c93f"/>',
           f'<text x="{width / 2:.0f}" y="22" fill="{DIM}" font-family="{FONT}" font-size="12" '
           f'text-anchor="middle">india-trading-skills · real calculator output</text>',
           f'<g font-family="{FONT}" font-size="{SIZE}" xml:space="preserve" style="white-space:pre">']
    for i, ((q, cmd, lines), (start, typed, end)) in enumerate(zip(scenes, spans)):
        y = TOP
        cmd_w = (len(cmd) + 2) * CHAR_W + 10
        out.append(f'<g opacity="0">{visible(start, end)}')
        out.append(f'<text x="{PAD_X}" y="{y}" fill="{DIM}"># You ask Claude: “{escape(q)}”</text>')
        y += LINE
        out.append(f'<clipPath id="type{i}"><rect x="{PAD_X}" y="{y - LINE}" height="{LINE + 4}" width="0">'
                   f'<animate attributeName="width" dur="{total:.2f}s" repeatCount="indefinite" '
                   f'values="0;0;{cmd_w:.0f};{cmd_w:.0f}" keyTimes="0;{kt(start + 0.6)};{kt(typed)};1"/>'
                   f'</rect></clipPath>')
        out.append(f'<text x="{PAD_X}" y="{y}" clip-path="url(#type{i})"><tspan fill="{ACCENT}">$</tspan>'
                   f'<tspan fill="{FG}"> {escape(cmd)}</tspan></text>')
        y += LINE
        for j, ln in enumerate(lines):
            on = typed + 0.3 + j * LINE_STEP
            out.append(f'<text x="{PAD_X}" y="{y}" fill="{colour(ln)}" opacity="0">{visible(on, end)}'
                       f'{escape(ln).replace(" ", NBSP)}</text>')
            y += LINE
        out.append('</g>')
    out.append('</g></svg>')
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "docs/assets/demo.svg"))
    a = ap.parse_args(argv)
    scenes = []
    for q, cmd, keep in SCENES:
        lines = run(cmd)
        scenes.append((q, cmd, keep(lines) if keep else lines))
    pathlib.Path(a.out).write_text(build(scenes), encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
