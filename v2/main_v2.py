"""
Interaction Portal v2 - Main Application

Gradio-based web interface for the Interaction Portal v2.
Implements the complete 13-page flow with server-side timers and state.

Layout (from spec images):
- Entry, Orientation, Meet Your Team, Your Role: Full-page
- Practice: Full-page with exercises
- Client Brief: Full-page with brief and card fields
- Task Page: Three-column layout
  - Left: Brief and role reminder (25% width)
  - Middle: Team chat with folded blocks (50% width)
  - Right: Orchestrator panel or card editor (25% width)
- Submission: Full-page with card review
- Check-in: Full-page with questions
- Break: Full-page with timer
- Seventh Client: Two-column (brief + card) or three-column
- Exit: Full-page

The layout matches the images in Interaction-Portal-Build-Spec_v2.md
"""

import gradio as gr
import uuid
import time
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

from config_loader_v2 import get_config, reset_config_loader
from simulation_v2 import (
    SimulationV2, get_simulation, cleanup_simulation,
    Session, SessionState, PageType
)
from conversation_logger_v2 import get_logger, reset_logger

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class InteractionPortalV2:
    """Main Gradio application for Interaction Portal v2"""
    
    def __init__(self):
        self.config = get_config()
        self.simulations: Dict[str, SimulationV2] = {}
        
        # Initialize logger
        self.logger = get_logger()
        
        # Create Gradio interface
        self._create_interface()
    
    def _create_interface(self):
        """Create the Gradio interface"""
        
        # Main state
        self.state = {
            "pid": None,
            "condition_code": None,
            "current_page": PageType.ENTRY.value,
            "session_start_time": None,
            "last_activity": None
        }
    
    def _get_simulation(self, pid: str, condition_code: str, user_agent: str, viewport: Dict) -> SimulationV2:
        """Get or create simulation for participant"""
        if pid in self.simulations:
            return self.simulations[pid]
        
        # Create new simulation
        sim = SimulationV2(pid, condition_code, user_agent, viewport)
        self.simulations[pid] = sim
        return sim
    
    def _get_current_simulation(self, pid: Optional[str] = None) -> Optional[SimulationV2]:
        """Get current simulation from state or pid"""
        target_pid = pid or self.state.get("pid")
        if target_pid and target_pid in self.simulations:
            return self.simulations[target_pid]
        if len(self.simulations) == 1:
            return next(iter(self.simulations.values()))
        return None
    
    def _update_state(self, **kwargs):
        """Update application state"""
        self.state.update(kwargs)
        self.state["last_activity"] = time.time()
    
    def _get_page_html(self, page_type: PageType, sim: SimulationV2, data: Dict = None) -> str:
        """Generate HTML for a specific page"""
        if data is None:
            data = {}
        
        page_content = sim.get_page_content(page_type, data.get("client_id"))
        
        # Generate HTML based on page type
        if page_type == PageType.ENTRY:
            return self._render_entry_page(page_content)
        
        elif page_type == PageType.ORIENTATION:
            return self._render_orientation_page(page_content)
        
        elif page_type == PageType.MEET_TEAM:
            return self._render_meet_team_page(page_content)
        
        elif page_type == PageType.YOUR_ROLE:
            return self._render_role_page(page_content)
        
        elif page_type == PageType.PRACTICE:
            return self._render_practice_page(page_content)
        
        elif page_type == PageType.CLIENT_BRIEF:
            return self._render_client_brief_page(page_content)
        
        elif page_type == PageType.TASK:
            return self._render_task_page(page_content)
        
        elif page_type == PageType.SUBMISSION:
            return self._render_submission_page(page_content)
        
        elif page_type == PageType.CHECKIN:
            return self._render_checkin_page(page_content)
        
        elif page_type == PageType.BREAK:
            return self._render_break_page(page_content)
        
        elif page_type == PageType.SEVENTH_CLIENT:
            return self._render_seventh_client_page(page_content)
        
        elif page_type == PageType.EXIT:
            return self._render_exit_page(page_content)
        
        return "<p>Page not found</p>"
    
    def _render_entry_page(self, content: Dict) -> str:
        """Render entry page"""
        config = self.config
        
        # Check for errors
        error = content.get("error", "")
        if error:
            return f"<div class='error'>{error}</div>"
        
        # Entry page validates link and starts session
        html = """
        <div class="entry-page">
            <h1>Interaction Portal</h1>
            <p>Loading your session...</p>
            <div style="margin-top: 25px;">
                <button onclick="continueFromEntry()" class="continue-button">Enter Portal</button>
            </div>
        </div>
        
        <script>
        function continueFromEntry() {
            handleAction('validate_link', {});
        }
        setTimeout(continueFromEntry, 600);
        </script>
        """
        return html
    
    def _render_orientation_page(self, content: Dict) -> str:
        """Render orientation page (full-page, matches spec image 3)"""
        html = f"""
        <div class="orientation-page">
            <h1>{content.get('title', 'Orientation')}</h1>
            <div class="content">
                {content.get('content', '')}
            </div>
            <div class="continue-section">
                <p class="minimum-reading">{content.get('minimum_reading_message', '')}</p>
                <button onclick="continueFromOrientation()" 
                        id="orientation-continue-btn"
                        class="continue-button" 
                        {"disabled" if not content.get('can_continue', False) else ""}>
                    {content.get('continue_button', 'Continue')}
                </button>
                <span class="timer">Time remaining: <span id="orientation-timer">{content.get('timer_remaining', '')}</span></span>
                <div style="margin-top: 10px;">
                    <a href="javascript:void(0)" onclick="skipReadingTimer()" style="font-size: 12px; color: #95a5a6; text-decoration: underline;">(Skip reading timer for testing)</a>
                </div>
            </div>
        </div>
        
        <script>
        function checkOrientationTimer() {{
            handleAction('timer_check', {{}}, (response) => {{
                if (response && response.timer) {{
                    const el = document.getElementById('orientation-timer');
                    if (el) el.textContent = response.timer;
                }}
                if (response && (response.expired || response.can_continue)) {{
                    const btn = document.getElementById('orientation-continue-btn');
                    if (btn) btn.disabled = false;
                }}
            }});
        }}
        
        function skipReadingTimer() {{
            handleAction('reading_complete', {{}}, () => {{
                const btn = document.getElementById('orientation-continue-btn');
                if (btn) btn.disabled = false;
                const el = document.getElementById('orientation-timer');
                if (el) el.textContent = '00:00';
            }});
        }}
        
        function continueFromOrientation() {{
            handleAction('continue', {{}});
        }}
        
        setInterval(checkOrientationTimer, 1000);
        checkOrientationTimer();
        </script>
        """
        return html
    
    def _render_meet_team_page(self, content: Dict) -> str:
        """Render meet your team page (full-page, matches spec image 4)"""
        team_members = content.get('team_members', [])
        
        members_html = ""
        for member in team_members:
            members_html += f"""
            <div class="team-member">
                <h3>{member['name']}</h3>
                <p class="label">{member['label']}</p>
                <p class="description">{member['description']}</p>
            </div>
            """
        
        html = f"""
        <div class="meet-team-page">
            <h1>{content.get('title', 'Meet Your Team')}</h1>
            <p class="introduction">{content.get('introduction', '')}</p>
            
            <div class="team-grid">
                {members_html}
            </div>
            
            <button onclick="continueFromMeetTeam()" class="continue-button">
                Continue
            </button>
        </div>
        
        <script>
        function continueFromMeetTeam() {{
            handleAction('continue', {{}});
        }}
        </script>
        """
        return html
    
    def _render_role_page(self, content: Dict) -> str:
        """Render your role page (full-page, matches spec image 5)"""
        html = f"""
        <div class="role-page">
            <h1>{content.get('title', 'Your Role')}</h1>
            <div class="content">
                {content.get('content', '')}
            </div>
            <div class="continue-section">
                <button onclick="continueFromRole()" 
                        id="role-continue-btn"
                        class="continue-button" 
                        {"disabled" if not content.get('can_continue', False) else ""}>
                    {content.get('continue_button', 'Continue')}
                </button>
                <span class="timer">Time remaining: <span id="role-timer">{content.get('timer_remaining', '')}</span></span>
                <div style="margin-top: 10px;">
                    <a href="javascript:void(0)" onclick="skipRoleTimer()" style="font-size: 12px; color: #95a5a6; text-decoration: underline;">(Skip reading timer for testing)</a>
                </div>
            </div>
        </div>
        
        <script>
        function checkRoleTimer() {{
            handleAction('timer_check', {{}}, (response) => {{
                if (response && response.timer) {{
                    const el = document.getElementById('role-timer');
                    if (el) el.textContent = response.timer;
                }}
                if (response && (response.expired || response.can_continue)) {{
                    const btn = document.getElementById('role-continue-btn');
                    if (btn) btn.disabled = false;
                }}
            }});
        }}
        
        function skipRoleTimer() {{
            handleAction('reading_complete', {{}}, () => {{
                const btn = document.getElementById('role-continue-btn');
                if (btn) btn.disabled = false;
                const el = document.getElementById('role-timer');
                if (el) el.textContent = '00:00';
            }});
        }}
        
        function continueFromRole() {{
            handleAction('continue', {{}});
        }}
        
        setInterval(checkRoleTimer, 1000);
        checkRoleTimer();
        </script>
        """
        return html
    
    def _render_practice_page(self, content: Dict) -> str:
        """Render practice page with 4 exercises (matches spec images 6-9)"""
        exercises = content.get('exercises', [])
        current_exercise = content.get('current_exercise', 0)
        practice_complete = content.get('practice_complete', False)
        
        exercises_html = ""
        for i, exercise in enumerate(exercises):
            completed = i < current_exercise or practice_complete
            is_current = i == current_exercise and not practice_complete
            
            ex_btn = ""
            if is_current:
                ex_btn = f'<button onclick="submitCurrentExercise(\'{exercise["id"]}\')" class="continue-button" style="margin-top: 10px; padding: 6px 16px; font-size: 14px;">Complete Exercise</button>'
            
            exercises_html += f"""
            <div class="exercise {'completed' if completed else ''} {'current' if is_current else ''}">
                <h3>{exercise['title']}</h3>
                <p>{exercise['description']}</p>
                {'<p class="check-question">' + exercise.get('check_question', '') + '</p>' if 'check_question' in exercise else ''}
                {'<input type="text" id="practice-ans" class="exercise-input" placeholder="Your answer">' if is_current and 'check_question' in exercise else ''}
                {f'<p class="correct-answer">Correct answer: {exercise.get("correct_answer", "")}</p>' if completed and 'correct_answer' in exercise else ''}
                {ex_btn}
            </div>
            """
        
        html = f"""
        <div class="practice-page">
            <h1>{content.get('title', 'Practice Exercises')}</h1>
            <p>{content.get('introduction', '')}</p>
            
            <div class="exercises">
                {exercises_html}
            </div>
            
            <div style="margin-top: 25px; text-align: center;">
                <button onclick="completePractice()" class="continue-button">Continue to Client Brief</button>
            </div>
        </div>
        
        <script>
        function submitCurrentExercise(exerciseId) {{
            const input = document.getElementById('practice-ans');
            const answer = input ? input.value : '';
            handleAction('complete_exercise', {{exercise_id: exerciseId, answer: answer}}, () => {{
                loadPage('practice');
            }});
        }}
        
        function completePractice() {{
            handleAction('practice_complete', {{}});
        }}
        </script>
        """
        return html
    
    def _render_client_brief_page(self, content: Dict) -> str:
        """Render client brief page (full-page, matches spec image 10)"""
        brief = content.get('brief', '')
        card_fields = content.get('card_fields', [])
        
        fields_html = ""
        for field in card_fields:
            fields_html += f"""
            <div class="card-field">
                <label>{field['label']}</label>
                <p class="placeholder">{field['placeholder']}</p>
            </div>
            """
        
        html = f"""
        <div class="client-brief-page">
            <h1>{content.get('title', 'Client Brief')}</h1>
            
            <h2>{content.get('requirements_label', 'Requirements:')}</h2>
            <div class="brief-content">
                {brief}
            </div>
            
            <h2>{content.get('card_fields_label', 'Card Fields:')}</h2>
            <div class="card-fields">
                {fields_html}
            </div>
            
            <button onclick="continueFromBrief()" class="continue-button">
                {content.get('continue_button', 'Start Task')}
            </button>
        </div>
        
        <script>
        function continueFromBrief() {{
            handleAction('continue', {{}});
        }}
        </script>
        """
        return html
    
    def _render_task_page(self, content: Dict) -> str:
        """Render task page (three-column layout, matches spec image 11)"""
        is_noai = content.get('is_noai', False)
        
        # Left column: Brief and role reminder
        role_reminder = content.get('role_reminder', '')
        client_name = content.get('title', 'Task Page').replace('Task Page - ', '')
        brief_text = content.get('brief', '')
        
        # Middle column and Right column
        if is_noai:
            # NOAI condition: Middle is reference pack, Right is card editor
            middle_column = f"""
            <div class="middle-column">
                <h2>Reference Pack</h2>
                {self._render_reference_pack(content.get('panel', {}))}
            </div>
            """
            card_editor = content.get('card_editor', {})
            right_column = f"""
            <div class="right-column">
                <div class="card-editor">
                    <h3>{content.get('card_editor_label', 'Card Editor')}</h3>
                    {self._render_card_editor(card_editor)}
                </div>
            </div>
            """
        else:
            # Regular condition: Middle is chat, Right is panel + card editor
            transcript = content.get('transcript', [])
            folded_blocks = content.get('folded_blocks', [])
            chat_html = self._render_chat(transcript, folded_blocks)
            
            middle_column = f"""
            <div class="middle-column">
                <h2>{content.get('team_chat_label', 'Team Chat')}</h2>
                {chat_html}
                
                <div class="message-input">
                    <textarea id="message-text" placeholder="Type your message..." rows="3"></textarea>
                    <div class="message-actions">
                        <button onclick="sendMessage()">Send</button>
                        <span class="mention-help">Use @name to message a specific agent</span>
                    </div>
                </div>
            </div>
            """
            
            panel = content.get('panel', {})
            card_editor = content.get('card_editor', {})
            right_column = f"""
            <div class="right-column">
                <div class="orchestrator-panel">
                    <h3>{content.get('panel_label', 'Orchestrator Panel')}</h3>
                    <div class="panel-content">
                        {panel.get('text', '')}
                    </div>
                    <p class="word-count">Words: {panel.get('word_count', 0)}</p>
                </div>
                <div class="card-editor">
                    <h3>{content.get('card_editor_label', 'Card Editor')}</h3>
                    {self._render_card_editor(card_editor)}
                </div>
            </div>
            """
        
        timer = content.get('timer_remaining', '')
        
        html = f"""
        <div class="task-page">
            <div class="header">
                <h1>{client_name}</h1>
                <span class="timer">
                    {content.get('timer_label', 'Time remaining')}: <span id="task-timer">{timer}</span>
                </span>
            </div>
            
            <div class="three-column">
                <!-- Left: Brief and Role Reminder -->
                <div class="left-column">
                    <h2>Brief</h2>
                    <div class="brief-summary" style="max-height: 260px; overflow-y: auto; margin-bottom: 15px; font-size: 13px; line-height: 1.5;">
                        <pre style="white-space: pre-wrap; font-family: inherit;">{brief_text}</pre>
                    </div>
                    <div class="role-reminder-box" style="padding: 10px; background: #eaf2f8; border-radius: 4px; border-left: 3px solid #3498db;">
                        <strong>Role Reminder:</strong>
                        <p style="margin-top: 5px; font-size: 13px;">{role_reminder}</p>
                    </div>
                </div>
                
                <!-- Middle Column -->
                {middle_column}
                
                <!-- Right Column -->
                {right_column}
            </div>
            
            <button onclick="submitCard()" class="submit-button">
                {content.get('submit_button', 'Submit Card')}
            </button>
        </div>
        
        <script>
        // Update timer every second
        function updateTimer() {{
            handleAction('timer_check', {{}}, (response) => {{
                if (response && response.expired) {{
                    document.getElementById('task-timer').textContent = '00:00';
                }} else if (response && response.timer) {{
                    document.getElementById('task-timer').textContent = response.timer;
                }}
            }});
        }}
        
        setInterval(updateTimer, 1000);
        
        function sendMessage() {{
            const text = document.getElementById('message-text').value;
            if (!text.trim()) return;
            
            // Extract @mentions
            const mentions = text.match(/@\\w+/g) || [];
            const addressees = mentions.map(m => m.substring(1));
            const atMentionUsed = mentions.length > 0;
            
            handleAction('send_message', {{
                text: text,
                addressees: addressees,
                at_mention_used: atMentionUsed
            }}, (response) => {{
                loadPage(currentPage);
            }});
            
            document.getElementById('message-text').value = '';
        }}
        
        function submitCard() {{
            handleAction('submit', {{}});
        }}
        
        // Allow Enter key to send message if input exists
        const msgInput = document.getElementById('message-text');
        if (msgInput) {{
            msgInput.addEventListener('keypress', (e) => {{
                if (e.key === 'Enter' && !e.shiftKey) {{
                    e.preventDefault();
                    sendMessage();
                }}
            }});
        }}
        
        // Auto scroll chat transcript
        const chatTr = document.querySelector('.chat-transcript');
        if (chatTr) {{ chatTr.scrollTop = chatTr.scrollHeight; }}
        
        updateTimer();
        </script>
        """
        return html
    
    def _render_chat(self, transcript: List[Dict], folded_blocks: List[Dict]) -> str:
        """Render team chat with folded blocks"""
        chat_html = "<div class='chat-transcript'>"
        
        # Add folded blocks
        for block in folded_blocks:
            chat_html += f"""
            <div class="folded-block {'open' if block.get('is_open', False) else 'closed'}" 
                 onclick="toggleBlock('{block['block_id']}')">
                <div class="block-header">
                    <span class="block-label">{block.get('label', '')}</span>
                    <span class="block-toggle">{'+' if not block.get('is_open', False) else '-'}</span>
                </div>
                {'<div class="block-messages">' + ''.join(f'<div class="message"><strong>{msg["sender"]}:</strong> {msg["text"]}</div>' for msg in block.get('messages', [])) + '</div>' if block.get('is_open', False) else ''}
            </div>
            """
        
        # Add regular messages
        for msg in transcript:
            if not msg.get('is_orchestrator', False):
                chat_html += f"""
                <div class="message">
                    <strong>{msg.get('sender', '')}:</strong> {msg.get('text', '')}
                </div>
                """
        
        chat_html += "</div>"
        
        return chat_html + """
        <script>
        function toggleBlock(blockId) {
            handleAction('fold_toggle', {block_id: blockId}, (response) => {
                // Update UI to reflect new state
                const block = document.querySelector(`.folded-block[data-block-id="${blockId}"]`);
                if (block) {
                    block.classList.toggle('open');
                    block.classList.toggle('closed');
                    const header = block.querySelector('.block-toggle');
                    if (header) {
                        header.textContent = block.classList.contains('open') ? '-' : '+';
                    }
                }
            });
        }
        </script>
        """
    
    def _render_reference_pack(self, panel: Dict) -> str:
        """Render reference pack for NOAI condition"""
        sections = [
            ("Client Analysis", "content/reference_packs/nia_pack.md"),
            ("Creative & Assets", "content/reference_packs/theo_pack.md"),
            ("Compliance & Licences", "content/reference_packs/rhys_pack.md"),
            ("Production Specs", "content/reference_packs/mira_pack.md"),
        ]
        
        sections_html = ""
        for title, filepath in sections:
            content_text = ""
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content_text = f.read()
            except Exception:
                content_text = f"Reference material for {title}."
                
            sections_html += f"""
            <div class="pack-section" style="margin-bottom: 12px; border: 1px solid #ddd; border-radius: 4px; padding: 10px;">
                <h4 style="margin-bottom: 6px; cursor: pointer; color: #2c3e50;" onclick="this.nextElementSibling.style.display = (this.nextElementSibling.style.display === 'none' ? 'block' : 'none')">▼ {title}</h4>
                <div class="pack-content" style="font-size: 13px; max-height: 180px; overflow-y: auto;">
                    <pre style="white-space: pre-wrap; font-family: inherit;">{content_text}</pre>
                </div>
            </div>
            """
        
        return f"""
        <div class="reference-pack">
            <h3>Reference Pack</h3>
            {sections_html}
        </div>
        """
    
    def _render_card_editor(self, card_editor: Dict) -> str:
        """Render card editor"""
        fields = card_editor.get('fields', {})
        role = card_editor.get('role', 'passive')
        editor_type = card_editor.get('editor_type', 'read_only')
        
        fields_html = ""
        for fid, field in fields.items():
            content = field.get('content', '')
            label = field.get('label', '')
            placeholder = field.get('placeholder', '')
            
            if role == 'passive':
                # Read-only
                fields_html += f"""
                <div class="card-field">
                    <label>{label}</label>
                    <div class="field-content readonly">{content or placeholder}</div>
                </div>
                """
            elif role == 'evaluative':
                # Keep/Cut/Send-back controls
                fields_html += f"""
                <div class="card-field">
                    <label>{label}</label>
                    <div class="field-content editable">
                        <div class="field-text">{content or placeholder}</div>
                        <div class="eval-controls">
                            <button onclick="fieldAction('{fid}', 'keep')" class="keep-btn">Keep</button>
                            <button onclick="fieldAction('{fid}', 'cut')" class="cut-btn">Cut</button>
                            <button onclick="fieldAction('{fid}', 'send_back')" class="send-back-btn">Send Back</button>
                        </div>
                    </div>
                </div>
                """
            else:  # generative
                # Typed text
                fields_html += f"""
                <div class="card-field">
                    <label>{label}</label>
                    <textarea class="field-text" 
                              placeholder="{placeholder}" 
                              oninput="updateField('{fid}', this.value)">
                        {content}
                    </textarea>
                </div>
                """
        
        # Typed share for generative
        typed_share_html = ""
        if role == 'generative':
            min_share = card_editor.get('min_typed_share', 0)
            current_share = card_editor.get('typed_share_length', 0)
            
            typed_share_html = f"""
            <div class="typed-share">
                <label>Your Contributions:</label>
                <textarea id="typed-share" placeholder="Add your typed contributions here..." 
                          oninput="updateTypedShare(this.value)"></textarea>
                <p>Characters: <span id="share-count">{current_share}</span> / {min_share} minimum</p>
            </div>
            """
        
        return f"""
        <div class="card-editor">
            {fields_html}
            {typed_share_html}
        </div>
        
        <script>
        function fieldAction(fieldId, action) {{
            handleAction('card_action', {{
                field: fieldId,
                action: action
            }}, () => {{
                loadPage(currentPage);
            }});
        }}
        
        function updateField(fieldId, value) {{
            handleAction('card_action', {{
                field: fieldId,
                action: 'type',
                text: value
            }});
        }}
        
        function updateTypedShare(value) {{
            document.getElementById('share-count').textContent = value.length;
            handleAction('card_action', {{
                action: 'typed_share',
                text: value
            }});
        }}
        </script>
        """
    
    def _render_submission_page(self, content: Dict) -> str:
        """Render submission page (full-page, matches spec image 15)"""
        card = content.get('card', {})
        fields = card.get('fields', {})
        
        fields_html = ""
        for fid, field in fields.items():
            fields_html += f"""
            <div class="card-field">
                <label>{field.get('label', '')}</label>
                <div class="field-content readonly">{field.get('content', '')}</div>
            </div>
            """
        
        html = f"""
        <div class="submission-page">
            <h1>{content.get('title', 'Review Submission')}</h1>
            <p>{content.get('instruction', '')}</p>
            
            <div class="card-review">
                {fields_html}
            </div>
            
            <div class="submission-actions">
                <button onclick="backToEdit()" class="back-button">
                    {content.get('back_button', 'Back to Edit')}
                </button>
                <button onclick="confirmSubmit()" class="submit-button">
                    {content.get('submit_button', 'Confirm Submission')}
                </button>
            </div>
        </div>
        
        <script>
        function backToEdit() {{
            handleAction('back', {{}});
        }}
        
        function confirmSubmit() {{
            handleAction('submit', {{}});
        }}
        </script>
        """
        return html
    
    def _render_checkin_page(self, content: Dict) -> str:
        """Render check-in page (full-page, matches spec image 16)"""
        items = content.get('items', [])
        
        items_html = ""
        for item in items:
            if item.get('type') == 'likert':
                options_html = ""
                for option in item.get('options', []):
                    options_html += f'<option value="{option}">{option}</option>'
                
                items_html += f"""
                <div class="checkin-item">
                    <label>{item.get('question', '')}</label>
                    <select onchange="answerCheckin('{item['id']}', this.value)">
                        <option value="">Select an option</option>
                        {options_html}
                    </select>
                </div>
                """
            else:  # multiple_choice
                options_html = ""
                for option in item.get('options', []):
                    options_html += f'<label><input type="radio" name="{item["id"]}" value="{option}" onchange="answerCheckin(\'{item["id"]}\', this.value)"> {option}</label>'
                
                items_html += f"""
                <div class="checkin-item">
                    <label>{item.get('question', '')}</label>
                    <div class="options">
                        {options_html}
                    </div>
                </div>
                """
        
        html = f"""
        <div class="checkin-page">
            <h1>{content.get('title', 'Check-in Questions')}</h1>
            <p>{content.get('instruction', '')}</p>
            
            <form class="checkin-form">
                {items_html}
            </form>
            
            <button onclick="submitCheckin()" class="submit-button">
                {content.get('submit_button', 'Submit Answers')}
            </button>
        </div>
        
        <script>
        const checkinAnswers = {{}};
        
        function answerCheckin(itemId, value) {{
            checkinAnswers[itemId] = {{
                value: value,
                time: Date.now()
            }};
        }}
        
        function submitCheckin() {{
            handleAction('submit_answers', {{
                answers: checkinAnswers
            }});
        }}
        </script>
        """
        return html
    
    def _render_break_page(self, content: Dict) -> str:
        """Render break page"""
        html = f"""
        <div class="break-page">
            <h1>{content.get('title', 'Break Time')}</h1>
            <p>{content.get('message', '')}</p>
            
            <div class="break-timer">
                Time remaining: <span id="break-timer">{content.get('timer_remaining', '')}</span>
            </div>
            
            <button onclick="endBreak()" class="continue-button">
                {content.get('end_break_button', 'End Break Early')}
            </button>
        </div>
        
        <script>
        function updateBreakTimer() {{
            handleAction('timer_check', {{}}, (response) => {{
                if (response.expired) {{
                    // Auto-advance when break ends
                    document.getElementById('break-timer').textContent = '00:00';
                    endBreak();
                }} else {{
                    document.getElementById('break-timer').textContent = response.timer || '00:00';
                }}
            }});
        }}
        
        setInterval(updateBreakTimer, 1000);
        
        function endBreak() {{
            handleAction('end_break', {{ended_early: true}});
        }}
        
        updateBreakTimer();
        </script>
        """
        return html
    
    def _render_seventh_client_page(self, content: Dict) -> str:
        """Render seventh client page (alone, no team)"""
        brief = content.get('brief', '')
        card_editor = content.get('card_editor', {})
        timer = content.get('timer_remaining', '')
        
        html = f"""
        <div class="seventh-client-page">
            <h1>{content.get('title', 'Final Client Task')}</h1>
            <p>{content.get('instruction', '')}</p>
            
            <div class="brief-section">
                <h2>Client Brief</h2>
                <div class="brief-content">
                    {brief}
                </div>
            </div>
            
            <div class="card-section">
                <h2>Card Editor</h2>
                {self._render_card_editor(card_editor)}
            </div>
            
            <div class="timer">
                Time remaining: <span id="seventh-timer">{timer}</span>
            </div>
            
            <button onclick="submitSeventhClient()" class="submit-button">
                Submit Card
            </button>
        </div>
        
        <script>
        function updateSeventhTimer() {{
            handleAction('timer_check', {{}}, (response) => {{
                if (response.expired) {{
                    document.getElementById('seventh-timer').textContent = '00:00';
                }} else {{
                    document.getElementById('seventh-timer').textContent = response.timer || '00:00';
                }}
            }});
        }}
        
        setInterval(updateSeventhTimer, 1000);
        
        function submitSeventhClient() {{
            handleAction('submit', {{}});
        }}
        
        updateSeventhTimer();
        </script>
        """
        return html
    
    def _render_exit_page(self, content: Dict) -> str:
        """Render exit page"""
        html = f"""
        <div class="exit-page">
            <h1>{content.get('title', 'Thank You!')}</h1>
            <p>{content.get('message', '')}</p>
            
            <div class="completion-code">
                <label>{content.get('completion_code_label', 'Your completion code:')}</label>
                <div class="code">{content.get('completion_code', '')}</div>
            </div>
            
            <p>{content.get('redirect_message', '')}</p>
            
            <script>
            // In production, this would redirect to Qualtrics
            // For demo, just show the code
            setTimeout(() => {{
                // Could redirect here if Qualtrics is enabled
            }}, 3000);
            </script>
        </div>
        """
        return html
    
    def handle_action(self, action: str, data: Dict, pid: Optional[str] = None, page: Optional[str] = None) -> Dict:
        """Handle user action from frontend"""
        sim = self._get_current_simulation(pid)
        if not sim:
            return {"success": False, "error": "No active session"}
        
        # Determine current page: prefer explicit page parameter if valid
        if page:
            try:
                current_page = PageType(page)
            except ValueError:
                current_page = sim.session.current_page
        else:
            current_page = sim.session.current_page if hasattr(sim, 'session') and hasattr(sim.session, 'current_page') else PageType(self.state.get("current_page", PageType.ENTRY.value))
        
        # Handle action through simulation
        try:
            result = sim.handle_action(current_page, action, data)
            
            # Update state based on result
            if result.get("next_page"):
                self._update_state(current_page=result["next_page"])
            
            return result
            
        except Exception as e:
            logger.error(f"Error handling action: {e}")
            return {"success": False, "error": str(e)}
    
    def get_current_page(self, pid: Optional[str] = None) -> str:
        """Get current page HTML"""
        sim = self._get_current_simulation(pid)
        if not sim:
            # Show entry page
            return self._wrap_with_layout(self._render_entry_page({}), PageType.ENTRY)
        
        current_page = sim.session.current_page if hasattr(sim, 'session') and hasattr(sim.session, 'current_page') else PageType(self.state.get("current_page", PageType.ENTRY.value))
        
        # Get page content
        page_html = self._get_page_html(current_page, sim)
        
        # Wrap with common layout
        return self._wrap_with_layout(page_html, current_page)
    
    def _wrap_with_layout(self, content: str, page_type: PageType) -> str:
        """Wrap content with common layout and scripts"""
        config = self.config
        
        # Check if Qualtrics is enabled
        qualtrics_enabled = config.qualtrics.enabled
        pid_val = self.state.get("pid", "") or ""
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Interaction Portal</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                {self._get_css()}
            </style>
        </head>
        <body>
            <div class="app-container">
                {content}
            </div>
            
            <script>
            // Global state
            window.currentPage = '{page_type.value}';
            var currentPage = window.currentPage;
            window.currentPid = '{pid_val}';
            
            // Handle actions
            window.handleAction = function(action, data, callback) {{
                const pid = window.currentPid || '{pid_val}';
                fetch('/action', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        action: action,
                        data: data || {{}},
                        page: window.currentPage,
                        pid: pid
                    }})
                }})
                .then(response => response.json())
                .then(result => {{
                    if (result && result.next_page) {{
                        window.currentPage = result.next_page;
                        loadPage(window.currentPage);
                    }}
                    if (callback) {{
                        callback(result);
                    }}
                }})
                .catch(error => {{
                    console.error('Error in handleAction:', error);
                }});
            }};
            var handleAction = window.handleAction;
            
            // Load page
            window.loadPage = function(page) {{
                if (page) {{
                    window.currentPage = page;
                }}
                const pid = window.currentPid || '{pid_val}';
                fetch('/page?pid=' + encodeURIComponent(pid))
                    .then(response => response.text())
                    .then(html => {{
                        const parser = new DOMParser();
                        const doc = parser.parseFromString(html, 'text/html');
                        const newContainer = doc.querySelector('.app-container');
                        const currentContainers = document.querySelectorAll('.app-container');
                        if (newContainer && currentContainers.length > 0) {{
                            const target = currentContainers[currentContainers.length - 1];
                            target.innerHTML = newContainer.innerHTML;
                            // Re-execute scripts
                            const scripts = target.querySelectorAll('script');
                            scripts.forEach(oldScript => {{
                                const newScript = document.createElement('script');
                                Array.from(oldScript.attributes).forEach(attr => {{
                                    newScript.setAttribute(attr.name, attr.value);
                                }});
                                newScript.text = oldScript.text;
                                oldScript.parentNode.replaceChild(newScript, oldScript);
                            }});
                        }}
                    }})
                    .catch(error => {{
                        console.error('Error in loadPage:', error);
                    }});
            }};
            var loadPage = window.loadPage;
            
            // Window focus/blur tracking
            window.addEventListener('blur', () => {{
                handleAction('window_blur', {{}});
            }});
            
            window.addEventListener('focus', () => {{
                handleAction('window_focus', {{}});
            }});
            
            // Scroll tracking
            window.addEventListener('scroll', () => {{
                handleAction('scroll_sample', {{
                    scroll_position: window.scrollY
                }});
            }});
            </script>
        </body>
        </html>
        """
        return html
    
    def _get_css(self) -> str:
        """Get CSS styles for the application"""
        return """
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            line-height: 1.6;
            color: #333;
            background-color: #f8f9fa;
        }
        
        .app-container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        
        h1, h2, h3, h4 {
            color: #2c3e50;
        }
        
        /* Entry Page */
        .entry-page {
            text-align: center;
            padding: 100px 0;
        }
        
        /* Orientation Page */
        .orientation-page .content {
            max-width: 800px;
            margin: 0 auto 40px;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .continue-section {
            text-align: center;
            margin-top: 40px;
        }
        
        .continue-button {
            padding: 12px 32px;
            font-size: 16px;
            background: #3498db;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: background 0.2s;
        }
        
        .continue-button:hover {
            background: #2980b9;
        }
        
        .continue-button:disabled {
            background: #95a5a6;
            cursor: not-allowed;
        }
        
        .timer {
            display: block;
            margin-top: 20px;
            color: #7f8c8d;
            font-size: 14px;
        }
        
        /* Meet Team Page */
        .meet-team-page .introduction {
            margin-bottom: 40px;
            font-size: 18px;
        }
        
        .team-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }
        
        .team-member {
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .team-member h3 {
            margin-bottom: 8px;
        }
        
        .team-member .label {
            color: #7f8c8d;
            font-size: 14px;
            margin-bottom: 8px;
        }
        
        .team-member .description {
            font-size: 14px;
            line-height: 1.5;
        }
        
        /* Role Page */
        .role-page .content {
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        /* Practice Page */
        .practice-page .exercises {
            max-width: 800px;
            margin: 0 auto;
        }
        
        .exercise {
            padding: 20px;
            margin-bottom: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            opacity: 0.6;
        }
        
        .exercise.completed {
            opacity: 1;
            border-left: 4px solid #27ae60;
        }
        
        .exercise.current {
            opacity: 1;
            border-left: 4px solid #3498db;
        }
        
        /* Client Brief Page */
        .client-brief-page .brief-content {
            max-width: 800px;
            margin: 20px auto;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .card-fields {
            margin-top: 30px;
        }
        
        .card-field {
            margin-bottom: 15px;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 4px;
        }
        
        .card-field label {
            display: block;
            font-weight: bold;
            margin-bottom: 5px;
        }
        
        .card-field .placeholder {
            color: #95a5a6;
            font-style: italic;
        }
        
        /* Task Page - Three Column Layout */
        .task-page .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        
        .task-page .timer {
            font-size: 18px;
            font-weight: bold;
            color: #e74c3c;
        }
        
        .three-column {
            display: grid;
            grid-template-columns: 25% 50% 25%;
            gap: 20px;
            margin-bottom: 20px;
        }
        
        .left-column, .middle-column, .right-column {
            padding: 15px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .left-column h2, .middle-column h2, .right-column h2 {
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid #ecf0f1;
        }
        
        /* Chat */
        .chat-transcript {
            height: 400px;
            overflow-y: auto;
            border: 1px solid #ecf0f1;
            border-radius: 4px;
            padding: 10px;
            margin-bottom: 15px;
        }
        
        .message {
            margin-bottom: 10px;
            padding: 8px 12px;
            background: #f8f9fa;
            border-radius: 4px;
        }
        
        .message strong {
            color: #3498db;
        }
        
        .folded-block {
            margin-bottom: 10px;
            border: 1px solid #bdc3c7;
            border-radius: 4px;
            overflow: hidden;
        }
        
        .folded-block.closed .block-messages {
            display: none;
        }
        
        .block-header {
            padding: 8px 12px;
            background: #ecf0f1;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
        }
        
        .block-label {
            font-weight: bold;
        }
        
        .block-toggle {
            font-weight: bold;
            color: #7f8c8d;
        }
        
        .message-input textarea {
            width: 100%;
            padding: 10px;
            border: 1px solid #bdc3c7;
            border-radius: 4px;
            resize: vertical;
            font-family: inherit;
        }
        
        .message-actions {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 8px;
        }
        
        .message-actions button {
            padding: 8px 16px;
            background: #3498db;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }
        
        .mention-help {
            font-size: 12px;
            color: #7f8c8d;
        }
        
        /* Panel */
        .orchestrator-panel {
            margin-bottom: 20px;
        }
        
        .panel-content {
            padding: 15px;
            background: #f8f9fa;
            border-radius: 4px;
            border: 1px solid #ecf0f1;
        }
        
        .word-count {
            font-size: 12px;
            color: #7f8c8d;
            text-align: right;
            margin-top: 8px;
        }
        
        /* Card Editor */
        .card-editor {
            margin-top: 20px;
        }
        
        .card-editor .card-field {
            margin-bottom: 15px;
        }
        
        .field-content {
            padding: 10px;
            background: white;
            border: 1px solid #ecf0f1;
            border-radius: 4px;
            min-height: 80px;
        }
        
        .field-content.readonly {
            padding: 10px;
            background: #f8f9fa;
        }
        
        .field-content.editable {
            padding: 10px;
        }
        
        .field-text {
            width: 100%;
            border: none;
            resize: none;
            background: transparent;
            font-family: inherit;
            min-height: 60px;
        }
        
        .eval-controls {
            display: flex;
            gap: 8px;
            margin-top: 8px;
        }
        
        .eval-controls button {
            padding: 4px 12px;
            font-size: 12px;
            border: none;
            border-radius: 3px;
            cursor: pointer;
        }
        
        .keep-btn { background: #27ae60; color: white; }
        .cut-btn { background: #e74c3c; color: white; }
        .send-back-btn { background: #f39c12; color: white; }
        
        /* Typed Share */
        .typed-share {
            margin-top: 20px;
        }
        
        .typed-share textarea {
            width: 100%;
            min-height: 100px;
            padding: 10px;
            border: 1px solid #bdc3c7;
            border-radius: 4px;
            resize: vertical;
            font-family: inherit;
        }
        
        /* Submission Page */
        .submission-page .card-review {
            max-width: 800px;
            margin: 30px auto;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .submission-actions {
            display: flex;
            gap: 20px;
            justify-content: center;
            margin-top: 30px;
        }
        
        .back-button {
            padding: 12px 32px;
            background: #95a5a6;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }
        
        .submit-button {
            padding: 12px 32px;
            background: #27ae60;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }
        
        /* Check-in Page */
        .checkin-page .checkin-form {
            max-width: 600px;
            margin: 0 auto;
        }
        
        .checkin-item {
            margin-bottom: 25px;
            padding: 15px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .checkin-item label {
            display: block;
            margin-bottom: 8px;
            font-weight: bold;
        }
        
        .checkin-item select {
            width: 100%;
            padding: 8px;
            border: 1px solid #bdc3c7;
            border-radius: 4px;
        }
        
        .options {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        
        /* Break Page */
        .break-page {
            text-align: center;
            padding: 100px 0;
        }
        
        .break-timer {
            font-size: 24px;
            margin: 40px 0;
        }
        
        /* Seventh Client Page */
        .seventh-client-page .brief-section,
        .seventh-client-page .card-section {
            margin-bottom: 30px;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        /* Exit Page */
        .exit-page {
            text-align: center;
            padding: 100px 0;
        }
        
        .completion-code {
            margin: 40px 0;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            display: inline-block;
        }
        
        .completion-code .code {
            font-size: 24px;
            font-weight: bold;
            color: #2c3e50;
            letter-spacing: 3px;
        }
        
        /* Reference Pack */
        .reference-pack {
            padding: 15px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .pack-section {
            margin-bottom: 15px;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 4px;
        }
        
        /* Utility */
        .error {
            color: #e74c3c;
            padding: 20px;
            background: #fadbd8;
            border-radius: 4px;
        }
        
        /* Responsive */
        @media (max-width: 768px) {
            .three-column {
                grid-template-columns: 100%;
            }
            
            .left-column, .middle-column, .right-column {
                margin-bottom: 20px;
            }
        }
        """


# Global application instance
_app: Optional[InteractionPortalV2] = None


def get_app() -> InteractionPortalV2:
    """Get or create application instance"""
    global _app
    if _app is None:
        _app = InteractionPortalV2()
    return _app


def create_gradio_interface():
    """Create and return the Gradio interface"""
    app = get_app()
    
    # Create Gradio interface that serves the HTML
    with gr.Blocks(title="Interaction Portal v2") as demo:
        gr.Markdown("# Interaction Portal v2")
        gr.Markdown("Multi-agent chat simulation for leadership and team coordination research")
        
        # For testing, provide a way to start a session
        with gr.Row():
            pid_input = gr.Textbox(label="Participant ID", value="test_001")
            condition_input = gr.Dropdown(
                label="Condition Code",
                choices=list(app.config.conditions.keys()),
                value="GEN-COORD"
            )
        
        start_btn = gr.Button("Start Session")
        
        # Display area
        display = gr.HTML(label="Portal")
        
        # Action handler
        def start_session(pid, condition):
            # Reset logger for new session
            reset_logger()
            
            # Create simulation
            sim = SimulationV2(
                pid=pid,
                condition_code=condition,
                user_agent="Gradio Interface",
                viewport={"width": 1024, "height": 768}
            )
            
            # Store in app
            sim.session.current_page = PageType.ORIENTATION
            app.simulations[pid] = sim
            app._update_state(pid=pid, condition_code=condition, current_page=PageType.ORIENTATION.value)
            
            # Get orientation page
            return app.get_current_page(pid)
        
        def handle_action_wrapper(action, data_str, page):
            """Wrapper for handle_action that works with Gradio"""
            try:
                data = json.loads(data_str) if data_str else {}
                app = get_app()
                result = app.handle_action(action, data, page=page)
                
                # If there's a next page, update and return it
                if result.get("next_page"):
                    app._update_state(current_page=result["next_page"])
                return app.get_current_page()
            except Exception as e:
                return f"Error: {str(e)}"
        
        start_btn.click(
            fn=start_session,
            inputs=[pid_input, condition_input],
            outputs=[display]
        )
        
        # For testing actions
        with gr.Accordion("Test Actions", open=False):
            action_input = gr.Textbox(label="Action")
            data_input = gr.Textbox(label="Data (JSON)")
            page_input = gr.Textbox(label="Page")
            test_btn = gr.Button("Send Action")
            
            test_btn.click(
                fn=handle_action_wrapper,
                inputs=[action_input, data_input, page_input],
                outputs=[display]
            )

    # Attach FastAPI endpoints for client-side fetch requests
    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse
    
    @demo.app.post("/action")
    async def api_action(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        action = payload.get("action", "")
        data = payload.get("data", {})
        pid = payload.get("pid")
        page_val = payload.get("page")
        app_inst = get_app()
        if pid and pid in app_inst.simulations:
            app_inst._update_state(pid=pid)
        result = app_inst.handle_action(action, data, pid=pid, page=page_val)
        return JSONResponse(content=result)
        
    @demo.app.get("/page")
    async def api_page(request: Request, pid: Optional[str] = None):
        app_inst = get_app()
        if pid and pid in app_inst.simulations:
            app_inst._update_state(pid=pid)
        html_content = app_inst.get_current_page(pid=pid)
        return HTMLResponse(content=html_content)
        
    @demo.app.get("/portal")
    async def api_portal(request: Request, pid: str = "test_001", condition: str = "GEN-COORD"):
        app_inst = get_app()
        reset_logger()
        sim = app_inst._get_simulation(
            pid, condition,
            request.headers.get("user-agent", "Browser"),
            {"width": 1024, "height": 768}
        )
        sim.session.current_page = PageType.ENTRY
        app_inst.simulations[pid] = sim
        app_inst._update_state(pid=pid, condition_code=condition, current_page=PageType.ENTRY.value)
        html_content = app_inst.get_current_page(pid=pid)
        return HTMLResponse(content=html_content)
    
    return demo


if __name__ == "__main__":
    # Create and launch Gradio interface
    demo = create_gradio_interface()
    demo.launch(server_name="0.0.0.0", server_port=7860, share=False, theme=gr.themes.Soft())
