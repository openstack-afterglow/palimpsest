## ADDED Requirements

### Requirement: Trusted per-record incremental commitments

The supervisor journal SHALL build compact trusted per-record chain commitments during its independent startup verification stream and extend them only after successful own durable append. Every requested frame SHALL match its indexed commitment before any batch or session exposes that frame, including non-tail frames. Metadata equality SHALL NOT substitute for content validation. Integrity failures SHALL permanently latch. Steady append and pull SHALL NOT reread the historical prefix or change journal disk/wire formats.

#### Scenario: Same-stamp non-tail payload rewrite

- **GIVEN** an open journal with multiple output frames and identical observed size/mtime/ctime before and after a same-inode rewrite
- **WHEN** a consumer requests the rewritten frame with either an invalid or recomputed payload checksum
- **THEN** it receives JOURNAL_CORRUPT before altered output is returned
- **AND** restoring the original bytes does not clear the failure latch.

#### Scenario: Reopen and independent cursors

- **WHEN** a valid journal is reopened, appended and consumed from independent existing positions
- **THEN** startup constructs the complete compact seal baseline and each pull reads only its next requested frame in order, without prefix rescans.

### Requirement: Explicit audit boundary

Cursor and snapshot metadata checks SHALL NOT be described as whole-history integrity audits. A same-stamp rewrite of an unrequested historical frame MAY remain undetected until that frame is read. Startup SHALL retain independent full-stream validation, without claiming an external pre-startup historical witness.

#### Scenario: Unrequested historical corruption

- **GIVEN** unchanged observed metadata and a modified frame before a consumer's current position
- **WHEN** only the cursor or later frames are requested
- **THEN** no instant historical audit is claimed; requested frames remain individually checked against trusted commitments.

### Requirement: Foreground web stop-handler ownership

Main-thread foreground web serving SHALL install its existing stop callback for SIGINT and SIGTERM regardless of inherited ignored dispositions and SHALL restore both actual original handlers on exit. Existing browser threading, deadlines and transport/socket cleanup SHALL remain unchanged.

#### Scenario: Launcher ignores SIGINT

- **GIVEN** a launcher explicitly sets SIGINT to SIG_IGN before executing the real web CLI
- **WHEN** the serving process receives SIGINT or SIGTERM
- **THEN** it exits0 within the existing shutdown bound and closes its listening socket.

#### Scenario: Embedded main-thread caller preserves handlers

- **GIVEN** distinct original callable handlers or ignored dispositions
- **WHEN** web serving handles either stop signal and exits
- **THEN** both exact original handlers are restored and the server socket is closed.
