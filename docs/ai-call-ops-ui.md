# AI Call Ops UI Design

## Goal

Add an operations console on top of the existing `qwen-telephony` voice stack. The voice stack handles one call and one AI conversation. The ops console manages many calls: campaigns, contacts, queues, call status, summaries, and analytics.

## MVP Scope

The first version focuses on an outbound-call operations loop. It does not implement a complex workflow editor for outbound campaigns; the dialogue-script editor lives in the dialogue management page.

Pages:

- Dashboard: total calls, answer rate, average duration, high-intent contacts, failed calls, active calls.
- Campaigns: create campaign, set prompt, concurrency, retry limit, and enqueue contacts.
- Contacts: create and manage names, phone numbers, tags, and notes.
- Calls: view and filter queued calls, dial placeholder, simulate answer/hangup/no-answer/busy states.
- Analytics: call status distribution and intent-level distribution.
- Settings: local service endpoints and operational notes.

## Architecture

```text
Browser UI
  -> REST JSON
  -> FastAPI management server
  -> SQLite
  -> Campaign / Contact / Call records
  -> future LiveKit SIP outbound API integration
```

## Data Model

Main tables:

- `campaigns`: campaign name, status, prompt, concurrency, retry limit.
- `contacts`: name, phone, tags, notes.
- `calls`: campaign/contact link, phone, status, room name, duration, summary, intent level.
- `call_messages`: future call transcript records.

## Outbound Call Flow

The MVP keeps real outbound SIP as a future integration point. The planned production flow is:

1. Pick pending contacts from a campaign.
2. Create or reuse a LiveKit room.
3. Pass campaign/contact/dialogue metadata to the agent.
4. Call LiveKit SIP `CreateSIPParticipant` after an outbound trunk is configured.
5. Track room, participant, and agent events.
6. Persist call status, transcript, summary, and intent level.

Until an outbound trunk exists, the UI provides MicroSIP-style simulation buttons so the business workflow can be tested without real outbound telephony.

## UI Principles

- Workbench first screen, no marketing landing page.
- Dense but readable operational layout.
- Left navigation, top health state, KPI strip, tables, and charts.
- Clear status colors for call states.
- No decorative graphics in workflow-heavy views.

## Later Extensions

- CSV contact import.
- Real outbound SIP trunk configuration.
- Concurrent dialing scheduler and retry strategy.
- WebSocket push for live call state.
- Agent result callback endpoint.
- Recording index and playback.
- Dialogue template variables.
- Automatic intent extraction.
- Multi-user permissions.
