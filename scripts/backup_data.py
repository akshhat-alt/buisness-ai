#!/usr/bin/env python3
"""Manual/cron-triggered snapshot of the entire data/ directory (every
tenant's SQLite files plus the Chroma vector store) into a timestamped
tar.gz — see business_ai/ops.py for the mechanism. The same snapshot is
also reachable over HTTP via POST /api/v1/admin/backup/run, for a
platform that would rather trigger it from an external cron than shell
into the instance directly.

Usage:
    python scripts/backup_data.py                          # data/ -> backups/
    python scripts/backup_data.py --data-dir /custom/data --output-dir /custom/backups
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from business_ai.ops import create_backup  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    project_root = Path(__file__).resolve().parent.parent
    parser.add_argument("--data-dir", default=str(project_root / "data"))
    parser.add_argument("--output-dir", default=str(project_root / "backups"))
    args = parser.parse_args()

    result = create_backup(Path(args.data_dir), Path(args.output_dir))
    print(f"Backup created: {result.archive_path} ({result.size_bytes:,} bytes) at {result.created_at}")


if __name__ == "__main__":
    main()
