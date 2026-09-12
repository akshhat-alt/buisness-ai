#!/usr/bin/env python3
"""One-time secret migration / key-rotation tool for Phase 9's at-rest
encryption (see business_ai/secrets_vault.py).

Two jobs, same mechanism:
  1. First-time encryption: every tenant registered before
     SECRET_ENCRYPTION_KEY existed has plaintext whatsapp_access_token/
     razorpay_key_secret on disk. Run this once after setting the key to
     encrypt them immediately, instead of waiting for each tenant's
     config to naturally be re-saved (the lazy-migration path
     TenantRegistry already handles on its own for any write).
  2. Key rotation: pass --old-key to decrypt under the key being retired
     and re-encrypt under the current SECRET_ENCRYPTION_KEY (or
     --new-key). Any tenant already encrypted under a THIRD, unrelated
     key is left untouched and reported, never silently corrupted.

Defaults to a dry run — prints what it would change, writes nothing —
because this touches every tenant's stored credentials at once. Pass
--apply to actually write.

Usage:
    python scripts/rotate_secrets.py                          # dry run, encrypt under $SECRET_ENCRYPTION_KEY
    python scripts/rotate_secrets.py --apply                  # actually write
    python scripts/rotate_secrets.py --old-key OLD --apply    # rotate from OLD to $SECRET_ENCRYPTION_KEY
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from business_ai.constants import DATA_ROOT  # noqa: E402
from business_ai.secrets_vault import decrypt_secret, encrypt_secret  # noqa: E402
from business_ai.tenant import TenantRegistry  # noqa: E402

SECRET_FIELDS = ("whatsapp_access_token", "razorpay_key_secret")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old-key", default=None, help="Key to decrypt existing values under, if rotating away from one. Omit for first-time encryption of legacy plaintext.")
    parser.add_argument("--new-key", default=None, help="Key to encrypt under. Defaults to $SECRET_ENCRYPTION_KEY.")
    parser.add_argument("--data-root", default=str(DATA_ROOT), help="Defaults to this project's own data/ directory.")
    parser.add_argument("--apply", action="store_true", help="Actually write changes. Without this, only reports what would change.")
    args = parser.parse_args()

    import os

    new_key = args.new_key or os.getenv("SECRET_ENCRYPTION_KEY")
    if not new_key:
        print("No target key: pass --new-key or set SECRET_ENCRYPTION_KEY.", file=sys.stderr)
        return 1

    db_path = Path(args.data_root) / "tenants.db"
    if not db_path.is_file():
        print(f"No tenants.db found at {db_path}.", file=sys.stderr)
        return 1

    # Read every tenant's CURRENT stored secret values back out under the
    # OLD key (or as legacy plaintext if --old-key wasn't given), then
    # re-register each one through a registry keyed with the NEW key —
    # the exact same write path every normal tenant update already goes
    # through, so this is provably the same code as the lazy migration,
    # just run proactively over every tenant at once.
    read_registry = TenantRegistry(db_path, secret_encryption_key=args.old_key)
    write_registry = TenantRegistry(db_path, secret_encryption_key=new_key)

    changed, unchanged, unrecoverable = [], [], []
    for tenant in read_registry.list_all():
        before = {field: getattr(tenant, field) for field in SECRET_FIELDS}
        needs_write = any(before[field] for field in SECRET_FIELDS)
        if not needs_write:
            unchanged.append(tenant.tenant_id)
            continue

        # Detect a value neither the old key nor "plaintext" can explain
        # — i.e. still enc:v1: after read_registry's decrypt attempt —
        # rather than silently re-encrypting an opaque blob as if it
        # were the real secret.
        still_encrypted = any(
            before[field] and before[field].startswith("enc:v1:") for field in SECRET_FIELDS
        )
        if still_encrypted:
            unrecoverable.append(tenant.tenant_id)
            continue

        changed.append(tenant.tenant_id)
        if args.apply:
            write_registry.update_config(tenant.tenant_id, **before)

    verb = "Encrypted" if args.apply else "Would encrypt"
    print(f"{verb} {len(changed)} tenant(s): {', '.join(changed) or '(none)'}")
    print(f"Skipped {len(unchanged)} tenant(s) with no secrets set.")
    if unrecoverable:
        print(
            f"WARNING: {len(unrecoverable)} tenant(s) have a secret this tool could not decrypt "
            f"under --old-key — left untouched, not corrupted: {', '.join(unrecoverable)}",
            file=sys.stderr,
        )
    if not args.apply and changed:
        print("Dry run only — re-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
