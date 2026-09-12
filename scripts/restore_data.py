#!/usr/bin/env python3
"""Restores a backup produced by backup_data.py / create_backup() over
the live data/ directory. The CURRENT data directory is renamed aside
(never deleted) before extraction — see business_ai/ops.py's
restore_backup() docstring — so a restore from the wrong archive is
itself trivially reversible.

Refuses to run without --confirm: this overwrites the running app's own
data directory, which is exactly the kind of action that should never
happen from a typo'd command.

Usage:
    python scripts/restore_data.py --archive backups/business_ai_backup_20260101T000000Z.tar.gz --confirm
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from business_ai.ops import restore_backup  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    project_root = Path(__file__).resolve().parent.parent
    parser.add_argument("--archive", required=True, help="Path to a business_ai_backup_*.tar.gz archive.")
    parser.add_argument("--data-dir", default=str(project_root / "data"))
    parser.add_argument("--confirm", action="store_true", help="Required — actually perform the restore.")
    args = parser.parse_args()

    if not args.confirm:
        print("Refusing to restore without --confirm. This will replace the current data/ directory.")
        print(f"Would restore {args.archive!r} over {args.data_dir!r}.")
        raise SystemExit(1)

    restored_path = restore_backup(Path(args.archive), Path(args.data_dir))
    print(f"Restored {args.archive} into {restored_path}. The previous data directory was moved aside, not deleted.")


if __name__ == "__main__":
    main()
