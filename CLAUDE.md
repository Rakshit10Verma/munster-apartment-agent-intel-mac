# Münster Apartment Agent — Claude Code Operating Contract

This repository is a real production housing-application agent. Treat the user's observed product behavior as the source of truth. Tests are supporting evidence, not the finish line.

## gstack

Use gstack for substantial engineering work:
- /office-hours for ambiguous product behavior
- /autoplan before multi-file changes
- /investigate before fixing production failures
- /review after implementation
- /qa for browser/user-flow validation
- /ship only after the change is verified

For web/browser QA prefer gstack /browse and /qa. Never claim a browser behavior is fixed only because a unit test passes.

## Primary objective

Bring the Münster apartment agent to unattended production reliability with Telegram as the normal human control surface.

A normal listing should be discoverable, analyzed, drafted and sent without laptop intervention. An exceptional listing should be resolvable from Telegram whenever the missing decision/fact can reasonably be supplied there.

## Product truth hierarchy

1. What actually happened on WG-Gesucht / Telegram.
2. Persisted send/contact evidence and screenshots/log artifacts.
3. Deterministic code state.
4. Automated tests.
5. Model interpretation.

If these disagree, investigate the disagreement. Do not change code merely to make tests green.

## Non-negotiable send-integrity rules

Never:
- erase or rewrite real production contact history to make a run look clean;
- retry Send after a click when sent-state is uncertain;
- bypass send_state_unknown; reconcile it read-only first;
- send a duplicate when a conversation already exists;
- fabricate applicant facts;
- claim a document/attachment is present without browser evidence;
- treat provider/model confidence as proof of a browser action;
- weaken duplicate-send protection for convenience.

A real application that imperfectly matches a landlord/WG preference may still be sent when the user explicitly confirms. A potentially duplicated application may not.

## Human confirmation semantics

Explicit user confirmation is authoritative for SOFT issues.

After the user taps Confirm Send, these may not independently block sending:
- age mismatch;
- optional/preference mismatches;
- missing WG experience;
- parental guarantor/Bürgschaft not confirmed;
- non-critical personal facts;
- contract-stage requirements such as SCHUFA or Haftpflicht where first contact is still possible;
- other truthful omissions that can be left out of the message.

When a soft fact is unknown:
- never invent it;
- omit the claim, ask through Telegram, or offer a truthful "Send anyway" path;
- if the user supplies the fact, persist it only in the appropriate explicit profile/answer store.

Hard technical blockers remain non-overridable:
- existing/possible previous Send requiring reconciliation;
- duplicate/existing conversation;
- listing identity mismatch;
- missing/expired login or CAPTCHA/human verification;
- no sendable message body;
- high-confidence scam or clearly prohibited housing rule if policy says never contact;
- browser state where the program cannot safely identify the target Send control.

## Phone-only rule

Routine production operation must not require opening the development Mac.

For review_required states, Telegram should provide one of:
- a direct answer choice;
- a free-text reply path;
- Regenerate;
- Edit/Change;
- Send anyway (for soft blockers);
- Reject;
- Reconcile (for ambiguous send state);
- Open listing.

Do not strand the user at "open /review on your Mac" when the required decision can be made from Telegram.

## Human-observation QA

For browser/Telegram changes, verification should imitate a human operator.

A production-quality QA pass should, when safely possible:
1. open the real WG-Gesucht listing;
2. compare visible listing facts with extracted facts;
3. inspect the exact draft for naturalness, truthfulness and listing-specific instructions;
4. observe the real Telegram notification/card;
5. judge whether the notification explains the situation clearly without internal implementation knowledge;
6. exercise the actual Telegram buttons;
7. exercise Confirm Send where a real send is intentionally part of the test;
8. observe the real WG-Gesucht composer, policy modal, Premium state and Bewerbermappe state;
9. verify the outgoing message in WG-Gesucht Messages;
10. verify exactly one message was sent;
11. verify Telegram's reported final state matches WG-Gesucht;
12. record any confusing/dead-end behavior as a product bug even if all automated tests passed.

Never fabricate a QA observation. If the real external surface was not inspected, say so.

## Real production tests

Real application sending is permitted during this hardening phase when the configured environment allows it. Treat these sends as valuable production evidence.

Before a real send:
- respect current housing hard rules;
- preserve duplicate protection;
- preserve truthful messaging;
- record the exact attempted message and fingerprint before waiting for verification.

After a real Send click:
- never auto-retry;
- verify via WG-Gesucht Messages/conversation evidence;
- otherwise persist send_state_unknown and reconcile later.

## Fix workflow

For a production bug:
1. reproduce or inspect real evidence;
2. state what the user actually experienced;
3. identify root cause before changing code;
4. implement the smallest durable fix;
5. add/update regression tests;
6. run ruff, mypy and pytest;
7. run gstack /review;
8. repeat the real browser/Telegram scenario when safely possible;
9. only then call the issue fixed.

If automated tests and real product behavior disagree, real behavior wins.

## Current housing rules

Keep config.yaml authoritative. Important current production intent includes:
- WG-Gesucht is the active source while other parsers remain paused;
- max warm rent for normal single accommodation: EUR 600;
- Zwischenmiete is acceptable at <= 6 months;
- Zwischenmiete below EUR 400/month is acceptable regardless of duration;
- age mismatch is never by itself a skip/review blocker;
- normal Kaution is not a furniture/processing fee;
- ordinary furniture Abschlag is informational unless suspicious;
- Bewerbermappe includes the user's enrollment certificate;
- never invent hobbies, WG experience, guarantor status or other personal facts.

## Completion standard

Do not summarize progress as "N tests pass" and stop.

A task involving user-facing behavior is complete only when:
- code/test checks pass;
- no new send-integrity regression is introduced;
- the user flow is coherent end-to-end;
- actual browser/Telegram behavior has been observed when safely possible;
- the user is not left in a dead end that requires manual drafting on the Mac for a resolvable soft issue.
