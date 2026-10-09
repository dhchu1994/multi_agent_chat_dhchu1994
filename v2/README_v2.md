# Interaction Portal v2

**Multi-agent chat simulation for leadership and team coordination research**

## Overview

Interaction Portal v2 is a web application where one participant works with a team of five AI agents on seven short client tasks, then returns to a Qualtrics survey. This is version 2 of the system, refactored from the original "multi-agent chat GUI" to implement the full specification from `Interaction-Portal-Build-Spec_v2.md`.

## Key Features

- **10 Conditions**: 9 variations with different participant roles and orchestrator panel levels, plus 1 NOAI condition
- **5 AI Agents**: 4 specialists (Nia, Theo, Rhys, Mira) + 1 orchestrator
- **7 Client Tasks**: 6 randomised + 1 fixed last (seventh client)
- **13-Page Flow**: Entry → Orientation → Meet Your Team → Your Role → Practice → Client Brief → Task → Submission → Check-in → Break → Seventh Client → Exit
- **Comprehensive Logging**: 20+ event types with span-level provenance tracking
- **Server-Side Timers**: All timers run on the server, survive page reloads
- **Configurable**: All content and settings in version-controlled YAML files

## Architecture

```
v2/
├── config_v2.yaml          # All configuration (conditions, agents, clients, text, etc.)
├── config_loader_v2.py     # Configuration loader with typed dataclasses
├── agents_v2.py            # 5 agents with behavior rules and harness support
├── card_editor.py          # Card editor with span-level provenance tracking
├── conversation_logger_v2.py # Comprehensive event logging
├── orchestrator_v2.py      # Orchestrator panel, folded blocks, message routing
├── simulation_v2.py        # Core simulation loop and session state
├── main_v2.py              # Gradio web interface
├── README_v2.md            # This file
└── content/
    ├── clients/             # Client brief files (C1-C7)
    │   ├── C1_brief.md
    │   └── ...
    ├── private/             # Specialist private material
    │   ├── C1_nia.md
    │   ├── C1_theo.md
    │   └── ...
    └── reference_packs/     # Reference packs for NOAI condition
        └── ...
```

## Configuration

All application behavior is controlled through `config_v2.yaml`:

### Main Settings
- **Model Configuration**: API endpoint, version, timeout, retries
- **Qualtrics Integration**: Can be disabled for testing (set `qualtrics.enabled: false`)
- **Session Settings**: Task timer, break timer, reading times, client order
- **Device Restrictions**: Desktop-only mode with custom error messages

### Conditions (10 total)
Each condition has:
- `role`: passive, evaluative, generative, or none
- `panel_level`: coordination, task_focused, developmental, or none
- `editor`: Type-specific settings (read_only, send_back_needs_reason, min_typed_share)
- `panel`: Word budget, refresh interval, sections to show

### Agents
- **Nia** (Client Analyst): Holds client notes and unstated priorities
- **Theo** (Creative): Holds asset library and brand guidelines
- **Rhys** (Compliance): Holds licence status and restrictions
- **Mira** (Production): Holds platform specs, timeline, and budget
- **Orchestrator**: Assigns work among specialists

Each specialist has:
- System prompt with behavior rules
- Private material per client
- Hidden items that must NOT be disclosed

### Clients (7 total)
Each client has:
- Name and brief
- Private material for each specialist
- 4 hidden items (one per specialist)
- Fixed last client (C7) always comes last

## Behavior Rules

The agents follow 4 strict rules (from spec section 7.3):

1. **Never disclose hidden items unless explicitly asked** → Decline to answer
2. **Never volunteer information about hidden items** → Don't offer unsolicited
3. **Always answer askable questions about domain** → If asked, answer fully
4. **Never answer outside domain** → Redirect to appropriate specialist

Two harnesses verify these rules:
- **Harness A**: Checks rules 1-2 (no disclosure)
- **Harness B**: Checks rules 3-4 (answering and redirecting)

## Page Flow

1. **Entry**: Validates link, loads condition, creates session
2. **Orientation**: 3-minute minimum reading time
3. **Meet Your Team**: Random order of specialists, orchestrator description varies by panel level
4. **Your Role**: Describes participant role (passive/evaluative/generative)
5. **Practice**: 4 exercises to familiarize with interface
6. **Client Brief**: Shows requirements and card fields
7. **Task Page**: Three-column layout
   - Left: Brief and role reminder
   - Middle: Team chat with folded blocks
   - Right: Orchestrator panel + card editor
8. **Submission**: Review card before final submission
9. **Check-in**: 5 questions (4 for NOAI)
10. **Break**: 2-minute break after client 3
11. **Seventh Client**: Alone, no team
12. **Exit**: Completion code and redirect to Qualtrics

## Card Editor Modes

### Passive Mode
- Card is read-only
- Can only submit as-is
- No editing allowed

### Evaluative Mode
- Can use **Keep**, **Cut**, and **Send Back** controls
- Send Back may require a reason (configurable)
- Each action is logged with provenance

### Generative Mode
- Can type text into card fields
- Minimum typed share required for submission (configurable)
- Typed text is tracked with author attribution

### NOAI Mode
- No chat, no agents, no panel
- Reference pack with 4 collapsible sections
- Card editor available

## Layout

The page layouts match the images in `Interaction-Portal-Build-Spec_v2.md`:

- **Full-page layouts**: Entry, Orientation, Meet Your Team, Your Role, Practice, Client Brief, Submission, Check-in, Break, Exit
- **Three-column layout**: Task Page
  - Left (25%): Brief and role reminder
  - Middle (50%): Team chat with folded blocks
  - Right (25%): Orchestrator panel + card editor
- **Two-column layout**: Seventh Client (optional)

## Event Logging

All events are logged with:
- `pid`: Participant ID
- `timestamp`: UTC timestamp to millisecond
- `client`: Current client ID
- `page`: Current page

### Event Types (20+)

| Event | Description |
|-------|-------------|
| `session_start` | Session begins |
| `session_end` | Session ends |
| `page_enter` | Enter a page |
| `page_leave` | Leave a page |
| `window_blur` | Tab loses focus |
| `window_focus` | Tab regains focus |
| `practice_step` | Complete practice exercise |
| `role_check` | Answer role check question |
| `message_sent` | Participant sends message |
| `agent_reply` | Specialist replies |
| `orch_message` | Orchestrator to specialist |
| `fold_open` | Open folded block |
| `fold_close` | Close folded block |
| `panel_shown` | Panel updates |
| `card_action` | Card edit action |
| `card_submit` | Card submitted |
| `checkin_answer` | Check-in question answered |
| `pack_open` | Open reference pack section (NOAI) |
| `pack_close` | Close reference pack section (NOAI) |
| `timer_expired` | Task timer runs out |
| `break_start` | Break begins |
| `break_end` | Break ends |
| `agent_error` | Agent API call fails |
| `scroll_sample` | Scroll position sample |

## Testing Without Qualtrics

The application can be tested without Qualtrics by setting:

```yaml
qualtrics:
  enabled: false
  survey_url: ""
  return_url: ""
```

In this mode:
- No Qualtrics links are required
- Sessions can be started with any PID
- All functionality works normally
- No redirect to Qualtrics on exit

## Running the Application

### Prerequisites

```bash
pip install gradio pyyaml
```

### Quick Start

```bash
cd v2
python main_v2.py
```

The application will be available at `http://localhost:7860`

### With Custom Configuration

Edit `config_v2.yaml` to customize:
- Model settings
- Condition configurations
- Client content
- Text and UI settings

### Content Files

Create content files in the `content/` directory:

```bash
mkdir -p content/clients content/private content/reference_packs
```

Each client needs:
- `content/clients/{client_id}_brief.md` - Client brief
- `content/private/{client_id}_{agent}.md` - Private material for each specialist

## Data Storage

- **Logs**: `logs/events.log` and `logs/events.jsonl`
- **Sessions**: `data/sessions.json`
- **Backups**: `backups/` (daily backups)

## Export

Events can be exported in CSV or JSON format from the admin interface (to be implemented).

## Build Order (from spec section 11)

1. ✅ Agent prompts and harness A
2. ⏳ Autopilot mode and harness B
3. ✅ Entry, config loading, event store, export
4. ✅ Session flow with server timers
5. ✅ Team chat with routing, private material, structured replies, folded blocks
6. ✅ Card editor per role, with provenance
7. ✅ Orchestrator panel
8. ⏳ No-AI condition
9. ⏳ Practice, check-in, break, seventh client, exit

## Future Work

- [ ] Implement Harness A and Harness B
- [ ] Implement autopilot mode
- [ ] Complete NOAI condition implementation
- [ ] Complete all practice exercises
- [ ] Implement admin export page
- [ ] Add Qualtrics integration
- [ ] Performance optimization for 30 concurrent sessions
- [ ] Production deployment configuration

## Questions for Researchers

From spec section 12:

1. **Interrupted sessions**: Resume or end after a gap? (Currently: resume within 24 hours)
2. **Single-use links**: How to prevent PID reuse? (Currently: session tracking)
3. **Structured replies**: Can specialists return structured data in one call? (Implemented: yes)
4. **Panel word budget**: Can model calls be held to fixed word range? (To be tested)
5. **Card provenance**: Span-level tracking implemented
6. **One build or several**: Currently configurable for reuse across studies

## Open Decisions (from spec section 13)

- Seventh client: reference pack available? (Currently: `false` in config)
- NOAI card editor: What controls? (Currently: generative mode)
- Panel naming specialists not yet asked? (Currently: `false` in config)
- Task timer length: (Currently: 600 seconds, configurable)

## License

This application is built for research purposes at the University of [Institution]. All data collected is anonymous (PID only, no personal information).

## References

- [Interaction-Portal-Build-Spec_v2.md](../Interaction-Portal-Build-Spec_v2.md) - Full specification
- [Interaction_Portal_v2_Migration_Prompt.md](../Interaction_Portal_v2_Migration_Prompt.md) - Migration guide
- [portal-mockups/](../portal-mockups/) - Mock-up HTML files for each page
