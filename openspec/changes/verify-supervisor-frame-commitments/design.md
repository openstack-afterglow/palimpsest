## Decision

Use a bytearray containing the SHA-256 rolling chain seal after each one-based journal cursor. During `_verify_full`, append seals to a candidate bytearray as each frame passes existing checksum, identity, cursor and semantic validation; publish it with the verified tail only after the stream and metadata checks succeed. During own append, extend the trusted array only after write, fsync, binding and final size checks succeed. Reopening always constructs an independent baseline from the existing stream.

For a pull from cursor c, hash the trusted session chain state plus the exact next frame and compare to bytes [32*c:32*(c+1)] before retaining the decoded frame or advancing its position. A payload rewrite with either its old or recomputed payload checksum cannot return non-tail output under identical timestamps. The existing error path permanently latches corruption. Each independent session retains its own position and reads only its requested next frame.

## Boundary and tradeoff

The index is trusted process memory, not a journal format extension or an externally authenticated historical witness. Startup checks the full existing stream but cannot prove pre-startup bytes against an absent prior independent witness. Live cursor/snapshot/append metadata checks detect changed size/mtime/ctime early; equal metadata does not prove old bytes unchanged. A same-stamp rewrite in an unrequested historical prefix is detected when that frame is requested, not by an instant whole-history audit. No prefix reread, timestamp sleep/nonce, retry or integrity bypass is introduced. Bytearray payload uses 32 bytes per record with ordinary amortized capacity overhead; no decoded historical output is retained.

## Regression scope

Use synthetic coarse `_content_state` values only to reproduce measured timestamp equality while retaining real content, inode/path binding, mode and ownership checks. Test invalid and recomputed checksums at the first and later non-tail output, direct pulls and consumer-visible sessions, repair-after-detection permanent latch, reopen/append and independent cursor positions. Assert requested/new-frame byte bounds and event ordering, not incidental hashing/decoding/read-call implementation counts. Explicit stamp-change fixtures force a distinct mtime without sleeps.

## Authorized web shutdown qualification repair

Actual standalone CLI control proved inherited SIGINT SIG_IGN remains ignored during serving; SIGTERM still exits cleanly. Preserve the existing KeyboardInterrupt stop callback and cleanup structure. Only main-thread `ui.serve` saves both actual SIGINT/SIGTERM handlers, installs the callback for both, and restores originals in finally. Extend the existing real-process browser/signal matrix with an explicit SIG_IGN-before-exec launcher; use real os.kill plus actual handler capture/restoration for callable and ignored originals. Do not change browser threads, shutdown timeouts or serve loop behavior. The earlier three failed full-suite child dispositions were not observed and are not retroactively attributed with certainty.
