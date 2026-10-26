## Why

Native amd64 qualification of released 0.3.8 found that same-size output rewrites can retain identical mtime/ctime. A valid recomputed payload checksum can then return altered non-tail output before the rolling chain reaches the trusted tail. Metadata equality is not content authority.

## What Changes

Retain one trusted 32-byte chain seal per record in a compact bytearray, built in the existing independent startup stream and extended only after a successful own durable append. Validate every requested frame against its indexed seal before adding it to a returned batch. Preserve descriptor/path binding, early metadata-change rejection, bounded one-frame pulls, corruption latching and the version-1 journal format. Prepare unpublished patch 0.3.9 without moving 0.3.8.

## Impact

Only inactive host supervisor journal integrity, regression fixtures, current contracts and release metadata change. Memory grows O(record count), 32 bytes per record plus bytearray capacity overhead, not payload size. Steady append/pull never rescans the historical prefix. Cursor/snapshot metadata reads do not independently audit unrequested historical payloads. No native KVM, guest, authority, wire/disk schema, production or publication change.

## Authorized qualification follow-through

Canonical root qualification exposed SIGINT web shutdown failures. A separate actual CLI control established inherited SIG_IGN prevents SIGINT shutdown while SIGTERM still works. Main authorized the minimal same-patch correction: main-thread `ui.serve` saves actual SIGINT/SIGTERM dispositions, installs the existing stop callback for both, and restores both in existing cleanup. Add deterministic inherited-ignore actual-process and actual original-handler restoration regressions; preserve deadlines/browser/server lifecycle and earlier failures. This narrowly expands the repair scope beyond the original journal defect.
