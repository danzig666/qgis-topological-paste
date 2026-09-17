# -*- coding: utf-8 -*-
"""Build the installable plugin ZIP (single top-level directory 'topological_paste/').

    python build_zip.py      ->  dist/topological_paste-<version>.zip
"""

import configparser
import os
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN = "topological_paste"
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_PREFIXES = ("gui_report_",)


def main():
    metadata = configparser.ConfigParser()
    metadata.read(os.path.join(ROOT, PLUGIN, "metadata.txt"), encoding="utf-8")
    version = metadata["general"]["version"]
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    target = os.path.join(ROOT, "dist", "%s-%s.zip" % (PLUGIN, version))
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for directory, subdirs, files in os.walk(os.path.join(ROOT, PLUGIN)):
            subdirs[:] = sorted(d for d in subdirs if d not in EXCLUDED_DIRS)
            for name in sorted(files):
                if name.endswith(EXCLUDED_SUFFIXES) or name.startswith(EXCLUDED_PREFIXES):
                    continue
                path = os.path.join(directory, name)
                archive.write(path, os.path.relpath(path, ROOT).replace(os.sep, "/"))
                count += 1
    print("%s (%d files)" % (target, count))


if __name__ == "__main__":
    main()
