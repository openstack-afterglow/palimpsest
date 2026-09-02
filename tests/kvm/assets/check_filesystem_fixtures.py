#!/usr/bin/env python3
"""Verify the committed real-filesystem proof fixtures and source bindings."""

from palimpsest_local._oci_stage1_kvm_proof import load_proof_filesystems


def main() -> None:
    fixtures = load_proof_filesystems()
    print(fixtures.manifest_digest)


if __name__ == "__main__":
    main()
