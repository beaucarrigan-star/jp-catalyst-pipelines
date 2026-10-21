"""Run the pipeline from your own computer - no Catalyst needed.

    python cli.py ingest 6715134000005520238
    python cli.py seed   <bid scenario id>
    python cli.py matrix <bid scenario id>
    python cli.py score  <bid scenario id> [--dry-run]

Credentials come from %USERPROFILE%\\.solpipe.env (see README). Python 3.9+,
standard library only.
"""
from __future__ import annotations

import argparse
import sys

import solpipe


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=sorted(solpipe.ACTIONS))
    p.add_argument("record_id")
    p.add_argument("--dry-run", action="store_true", help="read and compute, write nothing to CRM")
    p.add_argument("--budget", type=float, default=3600, help="ingest time budget in seconds (default 3600 locally)")
    a = p.parse_args()
    try:
        print(solpipe.dispatch(a.action, a.record_id, budget_s=a.budget, dry_run=a.dry_run))
        return 0
    except Exception as e:  # noqa: BLE001 - CLI surface
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
