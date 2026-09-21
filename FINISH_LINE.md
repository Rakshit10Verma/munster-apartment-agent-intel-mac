# Finish Line — Münster Apartment Agent

This is the behavioral acceptance contract for the project. The goal is not zero warnings or a green test counter. The goal is a trustworthy autonomous housing operator.

## Definition of done

The agent may be considered production-finished when repeated real runs demonstrate that:

- new WG-Gesucht listings are discovered quickly and classified correctly;
- valid listings are not lost because of stale/over-strict review rules;
- generated messages are natural, truthful and listing-specific;
- hidden questions and exact commands are handled correctly;
- Telegram is sufficient for routine operation and exceptional soft decisions;
- Confirm Send behaves like a real human authorization;
- Bewerbermappe, Premium behavior and modal handling work on the real site;
- real sends are independently verified in WG-Gesucht Messages;
- ambiguous sends never cause blind retries;
- existing conversations never receive duplicate applications;
- Telegram's final status matches what actually happened on WG-Gesucht.

## Human-style production scenarios

Every scenario is evaluated by observable user behavior, not only assertions.

### Normal eligible WG
Expected: discover -> draft -> Telegram card -> Send -> Confirm Send -> real WG send -> exactly one outgoing message -> Telegram confirms sent.

### Missing WG experience
Expected: listing is not stranded. Telegram allows a truthful answer, omission/Send anyway, or regeneration. No invented experience. Mac is not required.

### Elternbürgschaft required but not confirmed
Expected: Telegram explains the mismatch. User can state yes/no or Send anyway. If Send anyway is chosen, the outgoing draft must not falsely claim that a guarantor is available.

### Contract-stage documents
SCHUFA/Haftpflicht/Bürgschaft requested for later stages must not automatically block first contact unless the listing explicitly requires them before contact.

### Age mismatch
Never a standalone skip/review blocker. If mentioned in the draft, mention once naturally.

### Exact hidden command
Example: "Sonnenblume" must be the first word. The command must be mechanically validated before Send.

### Hidden-question duplication
Duplicate extraction may never create a false unanswered question after the content was already answered.

### Kaution
Normal deposit must not be classified as suspicious Abschlag/processing fee.

### Furniture Abschlag
Normal negotiated furniture takeover must not block unless payment structure is actually suspicious.

### Bewerbermappe
If required by our policy, confirm it visibly in the composer before Send. Handle the WG security/policy modal when it blocks the control.

### Premium
Rotating UI text must not be the sole locator. Unverified Premium should warn rather than strand an otherwise valid application unless strict mode is intentionally enabled.

### Confirm Send with soft mismatch
The explicit human confirmation must continue to a real send attempt, not bounce back to the same soft review reason.

### Send-state uncertainty
After Send is clicked and confirmation cannot be established: persist send_state_unknown, never click Send again, reconcile through the conversation/messages surface.

### Existing conversation
Detect before drafting/sending when possible. Never create a second application.

## Telegram UX acceptance

A person who knows nothing about the code should understand:
- why the listing needs attention;
- what exact fact/decision is missing;
- what each button will do;
- whether pressing Confirm Send actually means "send despite soft mismatch";
- whether the final application was sent, not sent or uncertain.

Avoid internal instructions such as "open /review on your Mac" as the only resolution path for ordinary missing facts.

## Production observation log

For every important live QA scenario, record:
- listing DB id and URL;
- what WG visibly showed;
- what the agent extracted;
- exact draft sent/attempted;
- Telegram card wording and buttons;
- button sequence exercised;
- browser evidence before Send;
- browser/conversation evidence after Send;
- final DB status;
- confusing UX or unexpected behavior;
- screenshots/log paths where available.

## Known historical bug classes to keep covered

- stale EUR 550 threshold after EUR 600 change;
- wrong Zwischenmiete filtering;
- AStA/NaDann extraction errors;
- private-landlord vs WG context misclassification;
- rigid message-length blocking;
- AI-like/repetitive application text;
- age mismatch overblocking;
- duplicate hidden questions;
- exact-command failures;
- weak provider fallback behavior;
- Kaution/Abschlag confusion;
- later-stage document requirements treated as pre-contact blockers;
- Premium UI text brittleness;
- stale premium failure state;
- false send_failed after a real send;
- blind resend risk;
- duplicate/existing conversation risk;
- Bewerbermappe attachment failures;
- WG security modal intercepting controls;
- weak attachment verification;
- misleading/generic review reasons;
- software failures mixed with genuine human decisions;
- valid cheap listings filtered by old logic;
- production-state resets that destroy evidence;
- unresolved send attempts not reconciled first;
- Telegram Confirm Send blocked by unresolved_required_facts;
- optional WG-experience prompt hidden in UI but still blocking in backend.
