## Why

Native Hub package authority currently treats ordinary member/reader roles as service permission and lets download authority and key writes survive insufficiently granular role checks. Align original tokens and attenuated keys with current effective Keystone Palimpsest capabilities without granting tenant users global builder authority.

## What Changes

- **BREAKING**: require exact Palimpsest leaf or parent service roles instead of plain member/reader for package and native artifact access.
- Separate inventory/manifests from content download; require publish for package/cache/tag writes, keys editor for issuance and keys admin for own-key revocation.
- Intersect issue-time token authority with current effective assignments; intersect delegated key actions with current owner authority at every use and publication commit.
- Preserve original actor, exact namespace/project/package scope, key-only native package publication, protected identities and global system-admin builder/GC gates.
- Add synthetic Keystone-bound HTTP regression cases and document isolated smoke setup; run no checks during integration.

## Capabilities

### New Capabilities
- `scoped-package-capabilities`: Native Hub leaf/parent authorization, least-privilege package keys and downgrade revalidation.

### Modified Capabilities
None.

## Impact

Hub auth, package DTO/action validation, package registry and native artifact route dependencies; existing Hub tests and package authorization documentation. No production Keystone/Hub changes, new delete/public policy APIs, VM launch authority, deployment or publication.
