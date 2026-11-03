"""Which model the scorers serve: a folder of model versions and a pointer to the current one.

    models/
      v20261103-1200/   lgbm_paysim.txt  metadata.json  reference_stats.json  report.json
      current.json      {"name": "v20261103-1200", "promoted": "...", "why": "..."}
      history.jsonl     one line per promotion, newest last

Scorers started with `MODEL_REGISTRY=<this folder>` serve what `current.json` names (the bundled model if there is
no pointer yet) and look at the pointer every few seconds. Promoting a version, or promoting an older one again
to roll back, is a single atomic rewrite of that small file, so a scorer never sees half a pointer. The folder
is shared with the scorers read-only (a bind mount, or a volume).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .lgbm import META_FILE, MODEL_FILE

POINTER = "current.json"
HISTORY = "history.jsonl"


class Registry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _pointer(self) -> dict | None:
        try:
            return json.loads((self.root / POINTER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def current(self) -> Path | None:
        """Folder of the promoted version, or None if nothing is promoted or the folder is incomplete."""
        pointer = self._pointer()
        if not pointer:
            return None
        folder = self.root / pointer["name"]
        return folder if (folder / MODEL_FILE).exists() and (folder / META_FILE).exists() else None

    def stamp(self) -> tuple[int, int] | None:
        """Changes whenever the pointer is rewritten; cheap enough to poll."""
        try:
            st = (self.root / POINTER).stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def versions(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / MODEL_FILE).exists())

    def promote(self, name: str, why: str = "") -> None:
        folder = self.root / name
        for required in (MODEL_FILE, META_FILE):
            if not (folder / required).exists():
                raise FileNotFoundError(f"{folder / required} not found; cannot promote {name!r}")
        entry = {"name": name, "promoted": time.strftime("%Y-%m-%d %H:%M:%S"), "why": why}
        tmp = self.root / (POINTER + ".tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, self.root / POINTER)  # atomic: a reader sees the old pointer or the new one
        with open(self.root / HISTORY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def history(self) -> list[dict]:
        try:
            lines = (self.root / HISTORY).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line.strip()]


def adopt(source: str | Path, root: str | Path, name: str) -> Path:
    """Copy a model folder (for example the bundled one) into the registry as a version, so it can be promoted."""
    import shutil

    target = Path(root) / name
    target.mkdir(parents=True, exist_ok=True)
    for item in Path(source).iterdir():
        if item.is_file():
            shutil.copy2(item, target / item.name)
    return target


def main() -> None:
    import argparse

    from .lgbm import ARTIFACT_DIR

    parser = argparse.ArgumentParser(description="Inspect and move the model pointer.")
    parser.add_argument("--root", default="models")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="versions, which one is current, and the promotion history")
    p = sub.add_parser("promote", help="serve this version (also how to roll back to an older one)")
    p.add_argument("name")
    p.add_argument("--why", default="manual")
    a = sub.add_parser("adopt-bundled", help="copy the model shipped in the package into the registry as a version")
    a.add_argument("--name", default=ARTIFACT_DIR.name)
    args = parser.parse_args()

    reg = Registry(args.root)
    if args.cmd == "list":
        current = reg.current()
        for v in reg.versions():
            print(("* " if current and current.name == v else "  ") + v)
        for h in reg.history():
            print(f"  {h['promoted']}  {h['name']}  {h['why']}")
    elif args.cmd == "promote":
        reg.promote(args.name, args.why)
        print(f"promoted {args.name}")
    else:
        print("adopted into", adopt(ARTIFACT_DIR, args.root, args.name))


if __name__ == "__main__":
    main()
