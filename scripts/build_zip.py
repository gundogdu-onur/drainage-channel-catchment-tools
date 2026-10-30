"""Build the installable plugin ZIP in dist/.

Usage: python scripts/build_zip.py
"""

import configparser
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "drainage_channel_catchment_tools"


def main():
    metadata = configparser.ConfigParser(interpolation=None)
    metadata.read(PLUGIN / "metadata.txt", encoding="utf-8")
    version = metadata["general"]["version"]
    target = ROOT / "dist" / f"{PLUGIN.name}-{version}.zip"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PLUGIN.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(ROOT).as_posix())
    print(target)


if __name__ == "__main__":
    main()
