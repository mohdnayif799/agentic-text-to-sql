"""Check the self-hosted fonts: tabular figures and the characters the UI uses.

Development only (not in the Docker image). Needs fontTools and brotli (woff2
is brotli-compressed), which are deliberately not in requirements-dev.txt
because only this one-off check uses them:

    .venv/Scripts/python.exe -m pip install fonttools brotli
    .venv/Scripts/python.exe -I scripts/dev/check_fonts.py static/fonts

For each .woff2 file it prints the family and weight, whether the OpenType
'tnum' feature is present, whether the default digits are already equal
width, and any UI character the file does not cover. A file passes when its
figures can be tabular (by default, or through tnum) and nothing is missing.

Why it matters: numbers in the run summary line and in tables line up only
with tabular (equal-width) figures. CSS asks for them with
font-variant-numeric: tabular-nums, which can only switch on a tnum feature
the font has. Default tabular digits need no feature at all, and they also
reach st.dataframe, whose grid is drawn on a canvas that CSS cannot style.

Result on 2026-10-08 (fontTools 4.66.1, @fontsource 5.3.0 Latin files): no
tnum in IBM Plex Sans or Mono, but every digit is 600 units wide by default,
so the figures are tabular everywhere. (Streamlit's bundled Source Sans has no
tnum either; its default digits are tabular too.)
"""

from __future__ import annotations

import sys
from pathlib import Path

from fontTools.ttLib import TTFont

DIGITS = "0123456789"
# Characters the app's own text relies on: the separator in the run summary
# line, currency, percent, and the punctuation of the answer prose.
UI_CHARS = DIGITS + "$%.,:;()-" + "·–—’“”"


def check(path: Path) -> bool:
    font = TTFont(path)
    names = font["name"]
    family = names.getDebugName(16) or names.getDebugName(1)
    weight = font["OS/2"].usWeightClass
    cmap = font.getBestCmap()
    gsub = font["GSUB"].table if "GSUB" in font else None
    features = ({r.FeatureTag for r in gsub.FeatureList.FeatureRecord}
                if gsub and gsub.FeatureList else set())
    widths = {font["hmtx"][cmap[ord(d)]][0] for d in DIGITS if ord(d) in cmap}
    missing = [c for c in UI_CHARS if ord(c) not in cmap]
    tabular_default = len(widths) == 1

    print(f"{path.name}: {family} {weight}")
    print(f"  tnum feature: {'yes' if 'tnum' in features else 'no'}"
          f"   pnum feature: {'yes' if 'pnum' in features else 'no'}")
    print(f"  default digits: {'tabular' if tabular_default else 'proportional'}"
          f" (advance widths {sorted(widths)})")
    print(f"  missing UI characters: {' '.join(missing) or 'none'}")
    return (tabular_default or "tnum" in features) and not missing


def main() -> None:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "static/fonts")
    files = sorted(folder.glob("*.woff2"))
    if not files:
        raise SystemExit(f"no .woff2 files in {folder}")
    results = [check(path) for path in files]
    print("\nEvery font has tabular figures and covers the UI characters."
          if all(results) else "\nSome fonts FAIL the check above.")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
