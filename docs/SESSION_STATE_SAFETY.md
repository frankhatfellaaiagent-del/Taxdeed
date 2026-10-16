# Account state and synchronization review

This change is isolated from landing-page PR #133. It starts from main
`b85e314dff280615c7da94fe6c4ff96a1425c2bf`. The application HTML at that base
exactly matches the audited public snapshot (SHA-256
`565bd9c3cf57a21961e2e0884e326e69741378176e14fbddea278f5f19404266`).

## Changes

- Browser notes, personal lists, preferences and recent properties belong to a
  specific account. Guest work stays separate. Shared-state caches and pending
  shared writes additionally belong to a specific team.
- Every auth transition increments a generation, cancels queued requests and
  invalidates old profile, realtime, Undo, research, upgrade and form callbacks.
  Private rendered screens are cleared immediately, including when switching
  directly from one account to another while viewing Saved or a property.
- Outgoing data is cloned when queued and bound to the captured user/team.
  Requests drain in order. An older acknowledgement cannot mark a newer edit
  saved. Later reversals are rebased against the acknowledged payload; failed
  requests retain the earlier baseline so unsent fields survive retry. A failed
  key remains retryable even if another state key succeeds.
- Initialization reads must succeed before replacement writes are allowed.
  Notes, preferences, personal lists, shared lists and existing buy-box changes
  made while initialization is pending are retained and drained before success.
- Pending edits have an account/team journal. Explicit shared changes are
  rebased onto a freshly read row, preserving unrelated remote fields, list
  entries and memberships. The journal is restored before new initialization
  edits can persist, including when initial reads fail. A missing buy-box seed
  uses `ignoreDuplicates` on initial attempts and retries, then adopts the stored
  winner. Login by itself does not replace existing shared rows.
- Initial reads, seed readback and accepted realtime rows retain timestamp
  watermarks, so older callbacks cannot replace newer state in the same session.
- Settings provides sync retry and ownership-confirmed recovery of older or
  guest browser notes/lists. Recovery creates a local backup of both inputs,
  retains the original keys, and requires an unchecked-by-default confirmation
  naming the destination account. Shared criteria are never imported silently.
- Promo copy waits for backend validation; duplicate submit and stale form
  responses are guarded. Interrupted signup links retain their code/plan until
  a successful sign-in. No trial entitlement or checkout endpoint was added.

## Validation

The audit's nine baseline behavior checks and five original issue reproductions
were run against the exact base. Its incomplete 37-check candidate was mapped
and its two shared-state replay defects reproduced before integration.

Run the current aggregate client suite with Node 22:

```sh
node tests/app-regressions.cjs
node tests/app-session-regressions.cjs
node --test tests/state-sync.test.cjs
npm ci --prefix tests/browser
(cd tests/browser && npx playwright install chromium)
node tests/browser/session-browser.cjs
```

The tests use synthetic identities and fake stores. Browser tests render the
actual app in new isolated contexts and intercept every network request;
Supabase is a stub module and no customer account is created or modified.
The browser suite covers rendered notes/lists across account changes, pending
saves, late profiles/realtime, shared-state initialization, failure/retry,
recovery, and desktop/mobile reflow. It does not use the shared signed-in demo.
The aggregate contains 37 audit checks, 23 additional application regressions,
20 queue/replay checks and 8 isolated browser flows. Independent review found
four additional ordering/retry cases; each now has a regression covering the
observed failure, including in-flight success and failure.

A read-only GitHub Actions workflow runs these tests for application PRs and the
review branch. It has no deploy step and no credentials beyond repository read.

## Boundaries and release review

This patch improves client ownership and ordering. It does not certify database
RLS, real Supabase reconnect/expiry behavior, FEEDBACK acceptance, payments, or
existing-account promo redemption. Those need an approved staging/backend test.
No production or customer-state mutation is part of this review.

Shared rows still use the existing whole-row API. Three-way replay narrows the
changes applied to the freshest read, but it is not a database transaction or
compare-and-swap: truly simultaneous remote writers can still race between the
read and upsert. A future server-side conditional-write/RPC protocol can close
that concurrency window without guessing backend migrations here.

Original unscoped data is retained and recoverable, not proof of ownership.
People must confirm the correct account before import. Criteria/preferences in
an older backup require manual review; they are not automatically assigned to a
team. Browser storage must be available to retain offline work across closing a
tab. Do not advertise this local test result as proof of customer isolation.
