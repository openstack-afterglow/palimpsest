## Why

The production Keystone excludes direct system assignments from an `effective=True` role-assignment query. Hub 0.3.1 consequently rejects a legitimate administrator even though a direct user/system-all query returns `admin`.

## What Changes

- Use the dedicated read-only validator for a direct user/system-all administrator lookup, without effective expansion.
- Regress recognition and immediate revocation through the installed Keystone SDK against HTTP.
- Publish the explicitly approved compatible root/Hub 0.3.2 release; preserve original subject scope and all existing privilege gates.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `scoped-package-capabilities`: qualify the verified-system-admin builder/GC gate through fresh direct system assignments rather than project-oriented effective expansion.

## Impact

Hub authentication, its native HTTP regression fixture, synchronized version metadata and Kolla image-tag default, architecture and installation guidance. No endpoint/schema change, ordinary user grant, service-admin fallback, native/KVM activation or shared-CAS expansion. Deployment remains the separately approved controller1-only canonical Kolla cutover.
