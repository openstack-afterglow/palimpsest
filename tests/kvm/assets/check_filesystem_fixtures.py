#!/usr/bin/env python3
"""Verify committed filesystem, recursive source-tree, and workload bindings."""

import hashlib
from pathlib import Path

from palimpsest_local._oci_stage1_kvm_proof import load_proof_filesystems


def main() -> None:
    fixtures = load_proof_filesystems()
    print(fixtures.manifest_digest)
    helper = Path(__file__).with_name("workload-proof.x86_64").read_bytes()
    print(f"workload-proof=sha256:{hashlib.sha256(helper).hexdigest()}")


if __name__ == "__main__":
    main()
