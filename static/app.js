document.addEventListener('DOMContentLoaded', () => {
    fetchBriefing();
    setInterval(fetchBriefing, 5000); // Poll every 5s for updates
    
    document.getElementById('btn-toggle-record').addEventListener('click', toggleRecording);
    document.getElementById('btn-highlight').addEventListener('click', logHighlight);
});

let isRecording = false;
let _briefingFailureCount = 0;

async function fetchBriefing() {
    try {
        const response = await fetch('/api/briefing');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();

        _briefingFailureCount = 0;
        const errBanner = document.getElementById('error-banner');
        if (errBanner) errBanner.classList.add('hidden');
        updateUI(data);
    } catch (e) {
        console.error("Failed to fetch briefing:", e);
        _briefingFailureCount += 1;
        // After two consecutive failures, stop pretending: replace any
        // widget still showing its initial loading shimmer with an explicit
        // offline message, and raise the error banner once. Without this, a
        // backend that is down at page-load leaves every widget shimmering
        // forever with no indication anything is wrong.
        if (_briefingFailureCount === 2) {
            showFetchError('Cannot reach the local backend (/api/briefing). Is `meeting-agent web` still running?');
            const offline = '<p style="color: var(--text-muted); text-align: center; margin-top: 2rem;">Backend unreachable — data unavailable.</p>';
            for (const id of ['task-list', 'calendar-list', 'full-calendar-list', 'full-task-list']) {
                const el = document.getElementById(id);
                if (el && (el.querySelector('.loading-shimmer') || /Loading/i.test(el.textContent))) {
                    el.innerHTML = offline;
                }
            }
        }
    }
}

function updateUI(data) {
    // Determine today's date
    const todayStr = new Date().toISOString().split('T')[0];
    
    // Split calendar data
    const todaysEvents = [];
    const allEvents = data.calendar || [];
    
    allEvents.forEach(m => {
        if (m.date === todayStr) {
            todaysEvents.push(m);
        }
    });

    // Update Dashboard Calendar List
    const calendarList = document.getElementById('calendar-list');
    const meetingCount = document.getElementById('meeting-count');
    
    if (todaysEvents.length > 0) {
        calendarList.innerHTML = todaysEvents.map((m, idx) => `
            <div class="meeting-card" style="display: flex; justify-content: space-between; align-items: center; ${(new Date().toTimeString().slice(0,5) >= m.start && new Date().toTimeString().slice(0,5) <= m.end) ? 'border: 2px solid var(--accent); background: rgba(184, 134, 46, 0.10);' : ''}">
                <div style="flex: 1;">
                    <div class="meeting-time" style="${(new Date().toTimeString().slice(0,5) >= m.start && new Date().toTimeString().slice(0,5) <= m.end) ? 'color: var(--accent); font-weight: bold;' : ''}">${escHtml(m.start)} - ${escHtml(m.end)} ${(new Date().toTimeString().slice(0,5) >= m.start && new Date().toTimeString().slice(0,5) <= m.end) ? '(NOW)' : ''}</div>
                    <div>
                        <h4>${escHtml(m.subject)}</h4>
                        <small style="color: var(--text-muted)">Org: ${escHtml(m.organizer)}</small>
                    </div>
                </div>
                <button class="btn-primary" style="padding: 5px 10px; font-size: 0.8rem; margin-left: 10px;" onclick="recordMeetingFromCalendar(this)" data-title="${escHtml(m.subject)}" data-org="${escHtml(m.organizer)}" data-start="${escHtml(m.start)}" data-body="${escHtml(m.body || '')}" data-participants="${escHtml((m.participants || []).join(', '))}">
                    <i class="fa-solid fa-microphone"></i> Record
                </button>
            </div>
        `).join('');
        meetingCount.innerText = todaysEvents.length;
    } else {
        calendarList.innerHTML = '<p style="color: var(--text-muted); text-align: center; margin-top: 2rem;">No meetings scheduled today.</p>';
        meetingCount.innerText = "0";
    }
    
    // Update Full Calendar Tab
    const fullCalendarList = document.getElementById('full-calendar-list');
    if (fullCalendarList) {
        if (allEvents.length > 0) {
            fullCalendarList.innerHTML = allEvents.map((m, idx) => `
                <div class="meeting-card" style="display: flex; justify-content: space-between; align-items: center;">
                    <div style="flex: 1;">
                        <div class="meeting-time">${escHtml(m.date)} | ${escHtml(m.start)} - ${escHtml(m.end)}</div>
                        <div>
                            <h4>${escHtml(m.subject)}</h4>
                            <small style="color: var(--text-muted)">Org: ${escHtml(m.organizer)}</small>
                        </div>
                    </div>
                    <button class="btn-primary" style="padding: 5px 10px; font-size: 0.8rem; margin-left: 10px;" onclick="recordMeetingFromCalendar(this)" data-title="${escHtml(m.subject)}" data-org="${escHtml(m.organizer)}" data-start="${escHtml(m.start)}" data-body="${escHtml(m.body || '')}" data-participants="${escHtml((m.participants || []).join(', '))}">
                        <i class="fa-solid fa-microphone"></i> Record
                    </button>
                </div>
            `).join('');
        } else {
            fullCalendarList.innerHTML = '<p style="color: var(--text-muted); text-align: center; margin-top: 2rem;">No upcoming meetings found.</p>';
        }
    }

    // Update Tasks
    const taskList = document.getElementById('task-list');
    const fullTaskCount = document.getElementById('full-task-count');

    const allTasks = [];
    if (data.tasks) {
        if (data.tasks.overdue) allTasks.push(...data.tasks.overdue);
        if (data.tasks.due_today) allTasks.push(...data.tasks.due_today);
        if (data.tasks.due_this_week) allTasks.push(...data.tasks.due_this_week);
        if (data.tasks.later) allTasks.push(...data.tasks.later);
        if (data.tasks.no_date) allTasks.push(...data.tasks.no_date);
    }

    if (allTasks.length > 0) {
        // Dashboard widget stays a lightweight preview (not filterable/editable).
        const tasksHTML = allTasks.map(t => `
            <div class="task-card" onclick="completeTask('${escHtml(t.id)}', this)">
                <div class="task-checkbox">
                    <i class="fa-solid fa-check"></i>
                </div>
                <div class="task-content">
                    <h4>${escHtml(t.description)}</h4>
                    <div class="task-meta">
                        <span><i class="fa-solid fa-user"></i> ${escHtml(t.owner) || 'Unassigned'}</span>
                        <span><i class="fa-solid fa-calendar"></i> ${escHtml(t.due_date) || 'No date'}</span>
                    </div>
                </div>
            </div>
        `).join('');
        if (taskList) taskList.innerHTML = tasksHTML;
    } else {
        const emptyState = '<p style="color: var(--text-muted); text-align: center; margin-top: 2rem;">All caught up! No pending tasks.</p>';
        if (taskList) taskList.innerHTML = emptyState;
    }

    // Tasks tab: full-featured list with filter/status/notes (renderFullTaskList).
    if (document.getElementById('full-task-list')) {
        renderFullTaskList(allTasks);
    } else if (fullTaskCount) {
        fullTaskCount.innerText = allTasks.length;
    }

    _renderDueAlertCards(data.tasks);
    
    // Update Notes
    const notesList = document.getElementById('notes-list'); // Dashboard widget
    const notesCount = document.getElementById('notes-count');
    const fullNotesList = document.getElementById('full-notes-list'); // Past Meetings tab
    
    if (data.notes && data.notes.length > 0) {
        // Map all notes
        const allNotesHTML = data.notes.map(n => {
            const htmlContent = escHtml(n.content).replace(/^- (.*$)/gim, '<li>$1</li>')
                                         .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
                                         .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
            return `
            <div class="note-card glass-panel" style="margin-bottom: 1rem; padding: 1rem; cursor: pointer;"
                 onclick="openMeetingDetail('${escHtml(n.session_id)}')">
                <h4 style="margin-bottom: 0.5rem; color: var(--primary);">Session: ${escHtml(n.session_id)}</h4>
                <div class="note-content" style="font-size: 0.9rem; line-height: 1.5;">${htmlContent}</div>
            </div>
            `;
        });
        
        // Dashboard shows top 2
        if (notesList) notesList.innerHTML = allNotesHTML.slice(0, 2).join('');
        if (notesCount) notesCount.innerText = data.notes.length;
        
        // Tab shows all
        if (fullNotesList) fullNotesList.innerHTML = allNotesHTML.join('');
    } else {
        if (notesList) notesList.innerHTML = '<div class="empty-state">No recent notes found.</div>';
        if (notesCount) notesCount.innerText = "0";
        if (fullNotesList) fullNotesList.innerHTML = '<div class="empty-state">No past meetings found.</div>';
    }

    // Update processing state with pipeline stage breakdown
    const banner = document.getElementById('processing-banner');
    const errBanner = document.getElementById('error-banner');

    const STAGE_META = {
        RECORDING:       { title: 'Recording…',        detail: 'Capturing audio from microphone and system loopback.',        icon: 'fa-microphone' },
        TRANSCRIBING:    { title: 'Transcribing…',      detail: 'Whisper is converting the recorded audio to text.',           icon: 'fa-waveform-lines' },
        LLM_LOADING:     { title: 'Loading LLM…',       detail: 'Waiting for the local language model to become ready.',       icon: 'fa-server' },
        EXTRACTING:      { title: 'Extracting…',        detail: 'The AI agent is extracting action items from the transcript.', icon: 'fa-microchip' },
        AWAITING_REVIEW: { title: 'Ready for Review',   detail: 'Draft action items are waiting in the Needs Review queue.',   icon: 'fa-circle-check' },
        ERROR:           { title: 'Processing Failed',  detail: '',                                                            icon: 'fa-triangle-exclamation' },
    };
    const STEP_ORDER = ['TRANSCRIBING', 'LLM_LOADING', 'EXTRACTING', 'AWAITING_REVIEW'];

    if (data.processing || data.pipeline_stage === 'AWAITING_REVIEW') {
        banner.classList.remove('hidden');
        const stage = data.pipeline_stage || 'TRANSCRIBING';
        const meta  = STAGE_META[stage] || { title: 'Processing…', detail: 'The AI is generating your notes.', icon: 'fa-microchip' };

        const iconEl   = document.getElementById('processing-icon');
        const titleEl  = document.getElementById('processing-title');
        const detailEl = document.getElementById('processing-detail');
        if (iconEl)   { iconEl.className = `fa-solid ${meta.icon}${stage === 'AWAITING_REVIEW' ? '' : ' fa-spin'}`; }
        if (titleEl)  { titleEl.textContent = meta.title; }
        if (detailEl) { detailEl.textContent = meta.detail; }

        // Highlight active/done steps in the progress rail
        const currentIdx = STEP_ORDER.indexOf(stage);
        document.querySelectorAll('.pipeline-step').forEach(el => {
            const stepIdx = STEP_ORDER.indexOf(el.dataset.step);
            el.classList.remove('active', 'done');
            if (stepIdx === currentIdx)     el.classList.add('active');
            else if (stepIdx < currentIdx)  el.classList.add('done');
        });
    } else {
        banner.classList.add('hidden');
    }

    if (data.error) {
        errBanner.classList.remove('hidden');
        document.getElementById('error-text').innerText = data.error;
    } else {
        errBanner.classList.add('hidden');
    }
    
    // Sync recording state. active_recording.started_at (bugfix-02 Fix F) is
    // the source of truth for elapsed-time/segment-offset math -- setting it
    // here (not just once in updateRecordingBtn) means a browser refresh
    // mid-recording restores the *true* start time instead of resetting the
    // reference point to the moment of the refresh.
    isRecording = data.recording;
    if (data.active_recording && data.active_recording.started_at) {
        _recordingStartMs = new Date(data.active_recording.started_at).getTime();
    }
    updateRecordingBtn();
}

function _renderDueAlertCards(tasks) {
    const container = document.getElementById('due-alert-cards');
    if (!container) return;
    const overdue = (tasks && tasks.overdue) || [];
    const dueToday = (tasks && tasks.due_today) || [];

    if (!overdue.length && !dueToday.length) {
        container.innerHTML = '';
        return;
    }

    const renderList = (items) => items.slice(0, 3).map(t => `<li>${escHtml(t.description)}</li>`).join('');

    let html = '';
    if (overdue.length) {
        html += `
        <div class="alert-card overdue">
            <i class="fa-solid fa-triangle-exclamation" style="color: var(--danger); font-size: 1.4rem;"></i>
            <div style="flex:1;">
                <strong>${overdue.length} overdue task${overdue.length === 1 ? '' : 's'}</strong>
                <ul>${renderList(overdue)}</ul>
                <a href="#" onclick="switchTab('tasks'); return false;" style="color: var(--primary); font-size: 0.85rem;">View all</a>
            </div>
        </div>`;
    }
    if (dueToday.length) {
        html += `
        <div class="alert-card due-today">
            <i class="fa-solid fa-calendar-day" style="color: #b8862e; font-size: 1.4rem;"></i>
            <div style="flex:1;">
                <strong>${dueToday.length} task${dueToday.length === 1 ? '' : 's'} due today</strong>
                <ul>${renderList(dueToday)}</ul>
                <a href="#" onclick="switchTab('tasks'); return false;" style="color: var(--primary); font-size: 0.85rem;">View all</a>
            </div>
        </div>`;
    }
    container.innerHTML = html;
}

function showFetchError(message) {
    const errBanner = document.getElementById('error-banner');
    const errText = document.getElementById('error-text');
    if (errBanner && errText) {
        errText.innerText = message;
        errBanner.classList.remove('hidden');
    }
    console.error(message);
}

async function recordMeetingFromCalendar(btnElement) {
    if (isRecording) {
        alert("A recording is already in progress. Stop it first before starting a new one.");
        return;
    }

    const title = btnElement.getAttribute('data-title');
    const org = btnElement.getAttribute('data-org');
    const start = btnElement.getAttribute('data-start');
    const bodyText = btnElement.getAttribute('data-body') || '';
    const participants = btnElement.getAttribute('data-participants') || '';

    const generatedContext = `Meeting: ${title}\nOrganizer: ${org}\nStart Time: ${start}\nParticipants: ${participants}\n\nDescription/Agenda:\n${bodyText}`;

    // Set UI Context box
    document.getElementById('meeting-context').value = generatedContext;

    // Scroll up
    window.scrollTo({top: 0, behavior: 'smooth'});

    // Send Start request with Title for auto-tagging
    const btn = document.getElementById('btn-toggle-record');
    btn.disabled = true;

    try {
        const response = await fetch('/api/record/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ context: generatedContext, title: title })
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        _updateLoopbackBadge(data.loopback);
        isRecording = true;
        updateRecordingBtn();
    } catch (e) {
        showFetchError(`Could not start recording: ${e.message}`);
    } finally {
        btn.disabled = false;
    }
}

async function startIsCall() {
    // One-tap flow (architecture_v2.md §12.2): bypasses the title prompt and
    // the pre-meeting context modal entirely -- the IS call is the most
    // frequent action for this user, so it gets the fewest clicks.
    if (isRecording) {
        alert("A recording is already in progress. Stop it first before starting a new one.");
        return;
    }
    const btn = document.getElementById('btn-start-is-call');
    if (btn) btn.disabled = true;
    try {
        const response = await fetch('/api/record/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ meeting_type: 'is-call' }),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        _updateLoopbackBadge(data.loopback);
        isRecording = true;
        updateRecordingBtn();
        switchTab('dashboard');
    } catch (e) {
        showFetchError(`Could not start IS call: ${e.message}`);
    } finally {
        if (btn) btn.disabled = false;
    }
}

let _pmcMeetingType = 'general';
let _pmcSelectedFile = null;

const _PMC_TYPE_LABELS = {
    'general': 'General / Other',
    'project-meeting': 'Project Meeting',
    'seminar': 'Seminar',
};

function openPreMeetingModal(type) {
    _pmcMeetingType = type;
    _pmcSelectedFile = null;
    document.getElementById('pmc-selected-file').classList.add('hidden');
    document.getElementById('pmc-selected-file').textContent = '';
    document.getElementById('pmc-title-input').value = '';
    document.getElementById('pmc-agenda').value = '';
    document.getElementById('pmc-mail-status').textContent = '';
    document.getElementById('pmc-error').textContent = '';
    document.getElementById('pmc-title').textContent =
        'Pre-Meeting Context — ' + (_PMC_TYPE_LABELS[type] || 'Meeting');
    document.getElementById('pre-meeting-context-modal').classList.add('open');
    _validatePreMeetingForm();
    document.getElementById('pmc-title-input').focus();
}

function _validatePreMeetingForm() {
    const title = document.getElementById('pmc-title-input').value.trim();
    document.getElementById('pmc-start-btn').disabled = title.length === 0;
}

function closePreMeetingModal() {
    document.getElementById('pre-meeting-context-modal').classList.remove('open');
}

function _acceptPreMeetingFile(file) {
    if (!file) return;
    const name = file.name.toLowerCase();
    if (name.endsWith('.eml') || name.endsWith('.msg')) {
        // Emails are parsed immediately and folded into the agenda notes —
        // deterministic alternative to the fuzzy "Search Outlook" button.
        _importDroppedEmail(file);
        return;
    }
    const allowed = ['.pdf', '.pptx', '.docx', '.txt'];
    if (!allowed.some(ext => name.endsWith(ext))) {
        showFetchError('Unsupported file type. Allowed: PDF, PPTX, DOCX, TXT — or an email as .eml/.msg.');
        return;
    }
    if (file.size > 50 * 1024 * 1024) {
        showFetchError('File exceeds the 50MB limit.');
        return;
    }
    _pmcSelectedFile = file;
    const sizeKb = Math.round(file.size / 1024);
    const el = document.getElementById('pmc-selected-file');
    el.innerHTML = `<i class="fa-solid fa-file"></i> ${escHtml(file.name)} (${sizeKb} KB)`;
    el.classList.remove('hidden');
}

async function _importDroppedEmail(file) {
    const statusEl = document.getElementById('pmc-mail-status');
    const agendaEl = document.getElementById('pmc-agenda');
    statusEl.textContent = `Parsing ${file.name}…`;
    try {
        const fd = new FormData();
        fd.append('file', file);
        const resp = await fetch('/api/context/mail-file', { method: 'POST', body: fd });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        const marker = '\n\n--- Dropped email context ---\n';
        agendaEl.value = agendaEl.value + marker + data.body;
        statusEl.textContent = `Email "${data.subject || file.name}" added to notes below.`;
    } catch (e) {
        statusEl.textContent = 'Email import failed: ' + e.message;
    }
}

function handlePreMeetingFileSelect(files) {
    _acceptPreMeetingFile(files && files[0]);
}

function handlePreMeetingFileDrop(event) {
    event.preventDefault();
    document.getElementById('pmc-dropzone').classList.remove('dragover');
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    _acceptPreMeetingFile(file);
}

async function fetchPreMeetingMailContext() {
    const statusEl = document.getElementById('pmc-mail-status');
    const agendaEl = document.getElementById('pmc-agenda');
    const hint = agendaEl.value.trim() || _pmcMeetingType;
    statusEl.textContent = 'Searching local Outlook…';
    try {
        const resp = await fetch('/api/context/mail', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ subject_hint: hint }),
        });
        const data = await resp.json();
        if (data.status === 'found' && data.body) {
            const marker = '\n\n--- Matched email context ---\n';
            if (!agendaEl.value.includes(marker)) {
                agendaEl.value = agendaEl.value + marker + data.body;
            }
            statusEl.textContent = 'Found a matching email — added to notes below.';
        } else {
            statusEl.textContent = 'No matching email found.';
        }
    } catch (e) {
        statusEl.textContent = 'Mail search failed: ' + e.message;
    }
}

async function startRecordingFromPreMeetingModal() {
    // Rewritten to remove a blocking native prompt() dialog that previously
    // collected the title *after* this click: prompt() returns null (and this
    // function used to bail out silently) if it's dismissed, blocked by browser
    // dialog-suppression settings, or run in an automated/headless context --
    // indistinguishable from "the button does nothing." The title is now a
    // regular in-modal input, validated before the button is even clickable
    // (see _validatePreMeetingForm), and every failure path here shows a
    // visible message in #pmc-error instead of failing silently.
    const errorEl = document.getElementById('pmc-error');
    errorEl.textContent = '';

    const titleInput = document.getElementById('pmc-title-input');
    const title = titleInput ? titleInput.value.trim() : '';
    if (!title) {
        errorEl.textContent = 'Enter a meeting title to continue.';
        return;
    }

    const agenda = document.getElementById('pmc-agenda').value;
    const btn = document.getElementById('pmc-start-btn');
    btn.disabled = true;
    try {
        const response = await fetch('/api/record/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ context: agenda, title, meeting_type: _pmcMeetingType }),
        });
        if (!response.ok) {
            const errBody = await response.json().catch(() => ({}));
            throw new Error(errBody.error || `HTTP ${response.status}`);
        }
        const data = await response.json();
        _updateLoopbackBadge(data.loopback);
        isRecording = true;
        updateRecordingBtn();

        if (_pmcSelectedFile) {
            const fd = new FormData();
            fd.append('session_id', data.session_id);
            fd.append('file', _pmcSelectedFile);
            try {
                await fetch('/api/context/upload', { method: 'POST', body: fd });
            } catch (e) {
                console.warn('Doc context upload failed (non-fatal):', e);
            }
        }
        closePreMeetingModal();
    } catch (e) {
        console.error('startRecordingFromPreMeetingModal failed:', e);
        errorEl.textContent = `Could not start recording: ${e.message}`;
    } finally {
        btn.disabled = false;
    }
}

async function toggleRecording() {
    const btn = document.getElementById('btn-toggle-record');

    if (!isRecording) {
        const typeSelect = document.getElementById('meeting-type-select');
        const type = typeSelect ? typeSelect.value : 'general';
        if (type !== 'is-call') {
            // Pre-meeting context modal (architecture_v2.md §12.3): shown
            // BEFORE recording starts for Project Meetings and Seminars.
            // Recording is actually started from the modal's own button.
            openPreMeetingModal(type);
            return;
        }
    }

    btn.disabled = true;

    try {
        if (!isRecording) {
            // The only way to reach here with !isRecording is the 'is-call' type
            // selected from the dropdown (every other type returned early above,
            // via the pre-meeting modal). The backend ignores title for is-call
            // sessions and auto-generates the is-call-{timestamp} slug, so this
            // bypasses the title prompt too, same as the Hub's one-tap button.
            const context = document.getElementById('meeting-context').value;

            const response = await fetch('/api/record/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ context: context, meeting_type: 'is-call' })
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            _updateLoopbackBadge(data.loopback);
            isRecording = true;
        } else {
            // Stop
            const response = await fetch('/api/record/stop', { method: 'POST' });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            isRecording = false;
        }
        updateRecordingBtn();
        fetchBriefing(); // Trigger immediate update
    } catch (e) {
        showFetchError(`Recording action failed: ${e.message}`);
    } finally {
        btn.disabled = false;
    }
}

function _updateLoopbackBadge(loopbackAvailable) {
    const badge = document.getElementById('live-source-badge');
    if (!badge) return;
    if (loopbackAvailable === false) {
        badge.textContent = 'Mic only — no system audio capture';
        badge.style.color = '#b8862e';
    } else if (loopbackAvailable === true) {
        badge.textContent = 'Mic + System Audio';
        badge.style.color = '#33513e';
    } else {
        badge.textContent = '';
    }
}

let _recordingStartMs = null;

async function logHighlight() {
    const btn = document.getElementById('btn-highlight');
    btn.disabled = true;
    const oldHtml = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-check"></i> Highlighted';

    try {
        const response = await fetch('/api/record/highlight', { method: 'POST' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        _showHighlightNoteInput();
    } catch (e) {
        showFetchError(`Could not log highlight: ${e.message}`);
    } finally {
        setTimeout(() => {
            btn.innerHTML = oldHtml;
            btn.disabled = false;
        }, 2000);
    }
}

function _showHighlightNoteInput() {
    // Inline, auto-expiring note field (architecture_v2.md §12.4): appears for
    // 8s after a highlight is logged, so the user can optionally annotate the
    // moment without breaking the flow of a live meeting. Saves on Enter, on
    // blur, or when the 8s window elapses -- whichever comes first.
    const existing = document.getElementById('highlight-note-wrap');
    if (existing) existing.remove();

    const wrap = document.createElement('div');
    wrap.id = 'highlight-note-wrap';
    wrap.className = 'highlight-note-wrap';
    wrap.innerHTML = `
        <input type="text" id="highlight-note-input" class="highlight-note-input"
               maxlength="80" placeholder="Add a note… (optional)"
               aria-label="Optional note for this highlight">`;
    document.getElementById('btn-highlight').insertAdjacentElement('afterend', wrap);

    const input = document.getElementById('highlight-note-input');
    input.focus();

    let settled = false;
    const commit = () => {
        if (settled) return;
        settled = true;
        const note = input.value.trim();
        wrap.remove();
        if (note) _saveHighlightNote(note);
    };

    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') commit(); });
    input.addEventListener('blur', commit);
    setTimeout(commit, 8000);
}

async function _saveHighlightNote(note) {
    const segmentOffsetSeconds = _recordingStartMs ? (Date.now() - _recordingStartMs) / 1000 : null;
    try {
        await fetch('/api/record/highlight', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ note, segment_offset_seconds: segmentOffsetSeconds, update_last: true }),
        });
    } catch (e) {
        console.warn('Could not save highlight note:', e);
    }
}

async function syncCalendar() {
    const btn = document.getElementById('btn-sync-calendar');
    if (btn) btn.disabled = true;
    try {
        const response = await fetch('/api/calendar/sync', { method: 'POST' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        fetchBriefing();
    } catch (e) {
        showFetchError(`Calendar sync failed: ${e.message}`);
    } finally {
        if (btn) btn.disabled = false;
    }
}

let liveTranscriptEventSource = null;
let _elapsedTimerHandle = null;

function _formatElapsed(ms) {
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = totalSeconds % 60;
    const pad = n => String(n).padStart(2, '0');
    return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function _tickElapsed() {
    const el = document.getElementById('recording-elapsed');
    if (!el || _recordingStartMs === null) return;
    el.textContent = _formatElapsed(Date.now() - _recordingStartMs);
}

function updateRecordingBtn() {
    const btn = document.getElementById('btn-toggle-record');
    const status = document.getElementById('recording-status');
    const statusText = document.getElementById('recording-status-text');
    const preMeetingControls = document.getElementById('pre-meeting-controls');
    const btnHighlight = document.getElementById('btn-highlight');
    const liveTranscriptPanel = document.getElementById('live-transcript-panel');
    const liveTranscriptText = document.getElementById('live-transcript-text');
    const elapsedEl = document.getElementById('recording-elapsed');

    if (isRecording) {
        if (_recordingStartMs === null) _recordingStartMs = Date.now();
        btn.innerText = "Stop Meeting";
        btn.classList.add('recording');
        status.className = 'recording-active';
        statusText.innerText = 'Recording live...';
        preMeetingControls.classList.add('hidden');
        btnHighlight.classList.remove('hidden');
        if (elapsedEl) {
            elapsedEl.classList.remove('hidden');
            _tickElapsed();
            if (_elapsedTimerHandle === null) _elapsedTimerHandle = setInterval(_tickElapsed, 1000);
        }
        if (liveTranscriptPanel) liveTranscriptPanel.classList.remove('hidden');
        
        if (!liveTranscriptEventSource) {
            liveTranscriptEventSource = new EventSource('/api/record/live');
            liveTranscriptEventSource.onmessage = function(event) {
                const data = JSON.parse(event.data);
                if (data.text && data.text.trim().length > 0) {
                    liveTranscriptText.innerText = data.text;
                    liveTranscriptText.scrollTop = liveTranscriptText.scrollHeight;
                } else if (isRecording) {
                    // Empty text = model loaded and waiting for speech
                    liveTranscriptText.innerText = 'Listening...';
                }
            };
        }
    } else {
        _recordingStartMs = null;
        btn.innerText = "Start Meeting";
        btn.classList.remove('recording');
        status.className = 'recording-idle';
        statusText.innerText = 'Ready to Record';
        preMeetingControls.classList.remove('hidden');
        btnHighlight.classList.add('hidden');
        document.getElementById('meeting-context').value = ""; // clear context
        if (liveTranscriptPanel) liveTranscriptPanel.classList.add('hidden');
        if (elapsedEl) {
            elapsedEl.classList.add('hidden');
            elapsedEl.textContent = '';
        }
        if (_elapsedTimerHandle !== null) {
            clearInterval(_elapsedTimerHandle);
            _elapsedTimerHandle = null;
        }

        if (liveTranscriptEventSource) {
            liveTranscriptEventSource.close();
            liveTranscriptEventSource = null;
        }
    }
}

async function completeTask(taskId, el) {
    const checkbox = el.querySelector('.task-checkbox');
    const content = el.querySelector('.task-content');
    
    // Optimistic UI update
    checkbox.classList.add('checked');
    content.classList.add('completed');
    
    // API Call
    try {
        const response = await fetch('/api/todo/complete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_id: taskId })
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        // We'll let the next poll remove it from the list
        setTimeout(() => fetchBriefing(), 1000);
    } catch (e) {
        showFetchError(`Could not complete task: ${e.message}`);
        checkbox.classList.remove('checked');
        content.classList.remove('completed');
    }
}

function switchTab(tabId) {
    // Data-driven: iterates whatever [data-tab] nav items exist in the DOM
    // rather than a hardcoded tab-name array, so adding a new tab only means
    // adding markup, not a second edit site here.
    document.querySelectorAll('.nav-links li[data-tab]').forEach(li => {
        const active = li.dataset.tab === tabId;
        li.classList.toggle('active', active);
        li.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    document.querySelectorAll('main.content > [id^="view-"]').forEach(panel => {
        panel.classList.toggle('hidden', panel.id !== 'view-' + tabId);
    });

    clearInterval(_statusPollTimer);
    if (tabId === 'review') loadReviewQueue();
    if (tabId === 'system') {
        loadSystemStatus();
        _statusPollTimer = setInterval(loadSystemStatus, 5000);
    }
    if (tabId === 'is-call-hub') loadIsCallHub();
    if (tabId === 'project-meetings') loadTypeFilteredMeetings('project-meeting');
    if (tabId === 'seminars') loadTypeFilteredMeetings('seminar');
    if (tabId === 'settings') loadSettings();
}

// ── Review / Apply UI ─────────────────────────────────────────────────────────

async function loadReviewQueue() {
    const loading = document.getElementById('review-loading');
    if (loading) loading.style.display = '';
    try {
        const resp = await fetch('/api/review/pending');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (loading) loading.style.display = 'none';
        renderReviewAwaiting(data.awaiting_review || []);
        renderReviewApply(data.awaiting_apply || []);
        // Update badge
        const total = (data.awaiting_review || []).length + (data.awaiting_apply || []).length;
        const badge = document.getElementById('review-badge');
        if (badge) {
            badge.textContent = total;
            badge.style.display = total > 0 ? '' : 'none';
        }
    } catch (e) {
        if (loading) loading.textContent = 'Failed to load review queue: ' + e.message;
        console.error('loadReviewQueue:', e);
    }
}

function _qualityBadge(label, score, flags) {
    // Fix 3.3: colored quality indicator for the review UI.
    if (!label) return '';
    const colors = { HIGH: '#33513e', MEDIUM: '#b8862e', LOW: '#a6432d' };
    const color = colors[label] || 'var(--text-muted)';
    const scoreStr = score != null ? ` (${score})` : '';
    let html = `<span style="font-size:0.78rem;font-weight:600;color:${color};margin-right:0.5rem;">
        ● ${escHtml(label)}${escHtml(scoreStr)}</span>`;
    if (flags && flags.length) {
        html += `<span style="font-size:0.72rem;color:var(--text-muted);">${flags.map(f => escHtml(f)).join(' · ')}</span>`;
    }
    return html;
}

function renderReviewAwaiting(sessions) {
    const container = document.getElementById('review-awaiting-list');
    if (!container) return;
    if (!sessions.length) {
        container.innerHTML = '<p style="color:var(--text-muted);">No sessions awaiting review.</p>';
        return;
    }
    container.innerHTML = sessions.map(s => `
        <div class="glass-panel" style="margin-bottom:1.5rem;padding:1.25rem;" id="session-block-${escHtml(s.session_id)}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;">
                <strong style="color:var(--primary);">Session: ${escHtml(s.session_id)}</strong>
                <button class="btn-primary" onclick="submitReview('${escHtml(s.session_id)}')"
                        style="padding:6px 16px;font-size:0.85rem;">
                    <i class="fa-solid fa-paper-plane"></i> Submit Review
                </button>
            </div>
            ${(s.quality_label) ? `<div style="margin-bottom:0.75rem;">${_qualityBadge(s.quality_label, s.quality_score, s.quality_flags)}</div>` : ''}
            ${s.items.length === 0
                ? '<p style="color:var(--text-muted);">No items in draft.</p>'
                : s.items.map(item => renderReviewItem(s.session_id, item)).join('')
            }
        </div>
    `).join('');
}

function renderReviewItem(sessionId, item) {
    // Each item gets Accept/Reject radio + editable owner + due_date.
    // Default is Accept (the common case); the user only has to act on rejects/edits.
    const safeId = escHtml(item.id);
    const safeDesc = escHtml(item.description);
    const safeOwner = escHtml(item.owner || '');
    const safeDue = escHtml(item.due_date || '');
    return `
        <div class="task-card" id="item-${safeId}" style="margin-bottom:0.75rem;padding:0.75rem;border-left:3px solid var(--primary);">
            <div style="display:flex;align-items:flex-start;gap:0.75rem;">
                <div style="flex:1;">
                    <div style="font-weight:500;margin-bottom:0.4rem;">${safeDesc}</div>
                    <div style="display:flex;gap:0.75rem;flex-wrap:wrap;">
                        <label style="font-size:0.8rem;color:var(--text-muted);">Owner:
                            <input type="text" value="${safeOwner}"
                                   id="owner-${safeId}"
                                   style="background:var(--card-raised);border:1px solid var(--hairline-strong);
                                          border-radius:3px;color:var(--ink);padding:2px 6px;width:120px;font-size:0.8rem;">
                        </label>
                        <label style="font-size:0.8rem;color:var(--text-muted);">Due:
                            <input type="date" value="${safeDue}"
                                   id="due-${safeId}"
                                   style="background:var(--card-raised);border:1px solid var(--hairline-strong);
                                          border-radius:3px;color:var(--ink);padding:2px 6px;font-size:0.8rem;">
                        </label>
                    </div>
                </div>
                <div style="display:flex;gap:0.5rem;align-items:center;flex-shrink:0;">
                    <label style="font-size:0.8rem;cursor:pointer;">
                        <input type="radio" name="dec-${safeId}" value="accept" checked> Accept
                    </label>
                    <label style="font-size:0.8rem;cursor:pointer;color:#a6432d;">
                        <input type="radio" name="dec-${safeId}" value="reject"> Reject
                    </label>
                </div>
            </div>
        </div>`;
}

async function submitReview(sessionId) {
    const block = document.getElementById('session-block-' + sessionId);
    if (!block) return;
    const itemEls = block.querySelectorAll('[id^="item-"]');
    const decisions = [];
    for (const el of itemEls) {
        const rawId = el.id.replace('item-', '');
        const radios = block.querySelectorAll(`input[name="dec-${rawId}"]`);
        let decision = 'accept';
        radios.forEach(r => { if (r.checked) decision = r.value; });
        const ownerEl = block.querySelector(`#owner-${rawId}`);
        const dueEl   = block.querySelector(`#due-${rawId}`);
        // Recover description from the rendered text node
        const descEl  = el.querySelector('div[style*="font-weight"]');
        decisions.push({
            id: rawId,
            decision,
            description: descEl ? descEl.textContent.trim() : '',
            owner: ownerEl ? (ownerEl.value.trim() || null) : null,
            due_date: dueEl ? (dueEl.value.trim() || null) : null,
        });
    }
    try {
        const resp = await fetch('/api/review/decide', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session_id: sessionId, decisions }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        block.innerHTML = `<div style="padding:0.5rem;display:flex;align-items:center;gap:0.75rem;">
            <span class="stamp stamp-pine">Reviewed</span>
            <span>${data.accepted_count} accepted, ${data.rejected_count} rejected for <strong>${escHtml(sessionId)}</strong>. Reload to apply.</span>
        </div>`;
        setTimeout(loadReviewQueue, 800);
    } catch (e) {
        showFetchError('Review submission failed: ' + e.message);
    }
}

function renderReviewApply(sessionIds) {
    const container = document.getElementById('review-apply-list');
    if (!container) return;
    if (!sessionIds.length) {
        container.innerHTML = '<p style="color:var(--text-muted);">No sessions ready to apply.</p>';
        return;
    }
    container.innerHTML = sessionIds.map(sid => `
        <div class="glass-panel" style="display:flex;justify-content:space-between;align-items:center;
                                         margin-bottom:0.75rem;padding:0.75rem 1rem;"
             id="apply-block-${escHtml(sid)}">
            <span style="color:var(--primary);">${escHtml(sid)}</span>
            <button class="btn-primary" onclick="applySession('${escHtml(sid)}')"
                    style="padding:6px 16px;font-size:0.85rem;">
                <i class="fa-solid fa-circle-check"></i> Apply to todo.md
            </button>
        </div>
    `).join('');
}

async function applySession(sessionId) {
    const block = document.getElementById('apply-block-' + sessionId);
    try {
        const resp = await fetch('/api/review/apply', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session_id: sessionId }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        let html = `<div style="padding:0.5rem;">
            <span class="stamp stamp-pine">Applied</span>
            <span style="margin-left:0.5rem;"><strong>${data.applied_count}</strong> item(s) from
            <strong>${escHtml(sessionId)}</strong> written to todo.md.</span>`;
        if (data.conflicts && data.conflicts.length > 0) {
            // Amendment 3: show both versions of each conflict
            html += `<div style="margin-top:0.5rem;color:#a6432d;">
                ⚠ ${data.conflicts.length} conflict(s) skipped (id already in todo.md):`;
            data.conflicts.forEach(c => {
                html += `<div style="margin:0.4rem 0 0 1rem;font-size:0.8rem;font-family:monospace;">
                    <div>id: ${escHtml(c.id)}</div>
                    <div>existing: ${escHtml(c.existing.description)}</div>
                    <div>incoming: ${escHtml(c.incoming.description)}</div>
                    <div style="color:var(--text-muted);">Reconcile by hand in data/todo.md.</div>
                </div>`;
            });
            html += '</div>';
        }
        html += '</div>';
        if (block) block.innerHTML = html;
        setTimeout(() => { loadReviewQueue(); fetchBriefing(); }, 800);
    } catch (e) {
        showFetchError('Apply failed: ' + e.message);
    }
}

// ── Past Meetings search (BM25 via /api/search) ───────────────────────────────

let _searchTimer = null;

function debouncedSearch(q) {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => runSearch(q), 250);
}

async function runSearch(q) {
    const resultsEl = document.getElementById('search-results');
    const listEl    = document.getElementById('full-notes-list');
    if (!resultsEl) return;

    if (!q.trim()) {
        resultsEl.style.display = 'none';
        resultsEl.innerHTML = '';
        if (listEl) listEl.style.display = '';
        return;
    }

    try {
        const resp = await fetch('/api/search?q=' + encodeURIComponent(q.trim()));
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        if (listEl) listEl.style.display = 'none';
        resultsEl.style.display = '';

        if (!data.results || data.results.length === 0) {
            resultsEl.innerHTML = '<p style="color:var(--text-muted);padding:0.5rem 0;">No matching meetings found.</p>';
            return;
        }

        resultsEl.innerHTML = `
            <p style="color:var(--text-muted);font-size:0.85rem;margin-bottom:0.5rem;">
                ${data.results.length} result(s) for <em>${escHtml(q)}</em>
            </p>` +
            data.results.map(r => `
            <div class="note-card glass-panel" style="margin-bottom:0.75rem;padding:1rem;cursor:pointer;"
                 onclick="openMeetingDetail('${escHtml(r.session_id)}')">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.4rem;">
                    <h4 style="color:var(--primary);margin:0;">${escHtml(r.session_id)}</h4>
                    <span style="font-size:0.75rem;color:var(--text-muted);">
                        score ${r.score} · ${r.source}
                    </span>
                </div>
                <p style="font-size:0.85rem;color:var(--text-muted);margin:0;font-family:monospace;">${escHtml(r.snippet)}</p>
            </div>`).join('');
    } catch (e) {
        resultsEl.style.display = '';
        resultsEl.innerHTML = `<p style="color:#a6432d;">Search error: ${escHtml(e.message)}</p>`;
        console.error('search:', e);
    }
}

function escHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ── Per-meeting detail modal ───────────────────────────────────────────────────

async function openMeetingDetail(sessionId) {
    const modal = document.getElementById('meeting-detail-modal');
    if (!modal) return;

    document.getElementById('modal-session-title').textContent = sessionId;
    ['modal-calendar-link','modal-mom','modal-summary','modal-actions','modal-highlights','modal-transcript'].forEach(id => {
        document.getElementById(id).style.display = 'none';
    });
    modal.style.display = 'block';
    document.body.style.overflow = 'hidden';

    try {
        const resp = await fetch('/api/meetings/' + encodeURIComponent(sessionId));
        if (!resp.ok) {
            document.getElementById('modal-session-title').textContent =
                sessionId + ' — not found';
            return;
        }
        const d = await resp.json();

        if (d.calendar_subject) {
            const startText = d.calendar_start ? ` at ${escHtml(d.calendar_start)}` : '';
            document.getElementById('modal-calendar-link').innerHTML =
                `<i class="fa-solid fa-calendar-check"></i> Linked calendar event: <strong>${escHtml(d.calendar_subject)}</strong>${startText}`;
            document.getElementById('modal-calendar-link').style.display = '';
        }

        if (d.mom_content) {
            // textContent, not innerHTML -- the <pre> tag itself gives us the
            // formatting; this keeps MoM content immune to the same class of
            // XSS risk esc()/escHtml() guards against everywhere else in this file.
            document.getElementById('modal-mom-body').textContent = d.mom_content;
            document.getElementById('modal-mom').style.display = '';
        }

        if (d.summary) {
            document.getElementById('modal-summary-body').textContent = d.summary;
            document.getElementById('modal-summary').style.display = '';
        }

        if (d.actions && d.actions.length) {
            const ul = document.getElementById('modal-actions-list');
            ul.innerHTML = d.actions.map(a => `
                <li style="padding:0.4rem 0;border-bottom:1px solid rgba(43,42,40,0.10);">
                    <strong>${escHtml(a.description || a.action || '')}</strong>
                    ${a.owner ? `<span style="color:var(--text-muted);font-size:0.85rem;"> · ${escHtml(a.owner)}</span>` : ''}
                    ${a.due_date ? `<span style="color:var(--text-muted);font-size:0.85rem;"> · due ${escHtml(a.due_date)}</span>` : ''}
                </li>`).join('');
            document.getElementById('modal-actions').style.display = '';
        }

        if (d.highlights && d.highlights.length) {
            const ul = document.getElementById('modal-highlights-list');
            ul.innerHTML = d.highlights.map(h => `
                <li style="padding:0.35rem 0;border-bottom:1px solid rgba(43,42,40,0.10);
                           color:var(--text-muted);font-size:0.9rem;">
                    ${escHtml(typeof h === 'string' ? h : (h.text || JSON.stringify(h)))}
                </li>`).join('');
            document.getElementById('modal-highlights').style.display = '';
        }

        if (d.transcript) {
            document.getElementById('modal-transcript-body').textContent = d.transcript;
            document.getElementById('modal-transcript').style.display = '';
        }
    } catch (e) {
        document.getElementById('modal-session-title').textContent =
            sessionId + ' — error loading';
        console.error('meeting detail:', e);
    }
}

function closeMeetingDetail() {
    const modal = document.getElementById('meeting-detail-modal');
    if (modal) modal.style.display = 'none';
    document.body.style.overflow = '';
}

function toggleTranscript() {
    const body = document.getElementById('modal-transcript-body');
    const btn  = document.getElementById('btn-toggle-transcript');
    if (!body) return;
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? '' : 'none';
    if (btn) btn.textContent = hidden ? 'hide' : 'show';
}

// Close modal on backdrop click
document.addEventListener('click', function(e) {
    const modal = document.getElementById('meeting-detail-modal');
    if (modal && e.target === modal) closeMeetingDetail();
});

// Close modal on Escape key
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeMeetingDetail();
});

// ── System / Server Control ────────────────────────────────────────────────────

let _statusPollTimer = null;

async function loadSystemStatus() {
    try {
        const resp = await fetch('/api/server/status');
        if (!resp.ok) return;
        const d = await resp.json();

        const el = id => document.getElementById(id);
        if (el('stat-uptime')) el('stat-uptime').textContent = d.uptime || '--';
        if (el('stat-pid'))    el('stat-pid').textContent    = d.web_pid || '--';

        const llmEl = el('stat-llm');
        if (llmEl) {
            if (d.llm_running) {
                llmEl.textContent = 'Running';
                llmEl.style.color = '#33513e';
            } else {
                llmEl.textContent = 'Stopped';
                llmEl.style.color = '#6e6656';
            }
        }

        const sessEl = el('stat-session');
        if (sessEl) {
            if (d.processing) {
                sessEl.textContent = 'Processing…';
                sessEl.style.color = '#b8862e';
            } else if (d.recording) {
                sessEl.textContent = 'Recording';
                sessEl.style.color = '#a6432d';
            } else if (d.active_session) {
                sessEl.textContent = d.active_session;
                sessEl.style.color = 'var(--primary)';
            } else {
                sessEl.textContent = 'Idle';
                sessEl.style.color = 'var(--text-muted)';
            }
        }
    } catch (e) {
        // server might be mid-restart — silently ignore
    }
}

async function restartWebServer() {
    const statusEl = document.getElementById('restart-status');
    if (statusEl) statusEl.textContent = 'Sending restart signal…';

    try {
        await fetch('/api/server/restart', { method: 'POST' });
    } catch (_) { /* expected: connection drops when server dies */ }

    if (statusEl) statusEl.textContent = 'Restarting — page will reload in a few seconds…';

    // Poll until the server responds, then hard-reload
    let attempts = 0;
    const poll = setInterval(async () => {
        attempts++;
        try {
            const r = await fetch('/api/server/status');
            if (r.ok) {
                clearInterval(poll);
                if (statusEl) statusEl.textContent = 'Back online — reloading…';
                setTimeout(() => location.reload(), 500);
            }
        } catch (_) {
            if (attempts > 30) {  // 15 s timeout
                clearInterval(poll);
                if (statusEl) statusEl.textContent = 'Server did not come back — check the terminal.';
            }
        }
    }, 500);
}

async function llmStart() {
    const msg = document.getElementById('llm-control-msg');
    if (msg) msg.textContent = 'Starting…';
    try {
        const resp = await fetch('/api/server/llm/start', { method: 'POST' });
        const d = await resp.json();
        if (msg) msg.textContent = d.status === 'already_running'
            ? `Already running (PID ${d.pid})`
            : `Started (PID ${d.pid})`;
        loadSystemStatus();
    } catch (e) {
        if (msg) msg.textContent = 'Error: ' + e.message;
    }
}

async function llmStop() {
    const msg = document.getElementById('llm-control-msg');
    if (msg) msg.textContent = 'Stopping…';
    try {
        const resp = await fetch('/api/server/llm/stop', { method: 'POST' });
        const d = await resp.json();
        if (msg) msg.textContent = d.status === 'not_running' ? 'LLM server was not running.' : 'Stopped.';
        loadSystemStatus();
    } catch (e) {
        if (msg) msg.textContent = 'Error: ' + e.message;
    }
}

async function confirmResetData() {
    const statusEl = document.getElementById('reset-status');
    if (!confirm('Delete ALL meeting records, session state, pending reviews, and clear todo.md?\n\nThis cannot be undone.')) {
        return;
    }
    if (statusEl) statusEl.textContent = 'Resetting…';
    try {
        const resp = await fetch('/api/data/reset', { method: 'POST' });
        const d = await resp.json();
        if (statusEl) statusEl.textContent =
            `Done — cleared meetings:${d.cleared.meetings}, state:${d.cleared.state}, pending:${d.cleared.pending_review}`;
        // Refresh the dashboard data
        await fetchBriefing();
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    }
}

// ── IS Call Hub ──────────────────────────────────────────────────────────────

function _sessionDateFromId(sessionId) {
    const m = sessionId.match(/-(\d{8})-(\d{6})$/);
    if (!m) return null;
    const y = m[1].slice(0, 4), mo = m[1].slice(4, 6), d = m[1].slice(6, 8);
    const hh = m[2].slice(0, 2), mm = m[2].slice(2, 4);
    return { dateStr: `${y}-${mo}-${d}`, display: `${y}-${mo}-${d} ${hh}:${mm}` };
}

function _renderActionList(actions) {
    if (!actions || !actions.length) return '<div class="empty-state">No action items.</div>';
    return '<ul style="list-style:none;padding:0;margin:0;">' + actions.map(a => `
        <li style="padding:0.5rem 0;border-bottom:1px solid rgba(43,42,40,0.10);">
            <div>${escHtml(a.description)}</div>
            <div class="task-meta">
                <span><i class="fa-solid fa-user"></i> ${escHtml(a.owner) || 'Unassigned'}</span>
                <span><i class="fa-solid fa-calendar"></i> ${escHtml(a.due_date) || 'No date'}</span>
                ${a.priority ? `<span><i class="fa-solid fa-flag"></i> ${escHtml(a.priority)}</span>` : ''}
            </div>
        </li>`).join('') + '</ul>';
}

async function loadIsCallHub() {
    const yesterdayEl = document.getElementById('is-call-yesterday-targets');
    const todayEl = document.getElementById('is-call-today-progress');
    const historyEl = document.getElementById('is-call-history-list');

    _loadRecurringBlockers();

    try {
        const resp = await fetch('/api/briefing');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const isCallNotes = (data.notes || []).filter(n => n.session_id.startsWith('is-call-'));

        if (!isCallNotes.length) {
            historyEl.innerHTML = '<p style="color:var(--text-muted);">No IS calls recorded yet. Click "Start IS Call" above to begin.</p>';
            return;
        }

        const todayStr = new Date().toISOString().split('T')[0];
        const todaysCall = isCallNotes.find(n => {
            const d = _sessionDateFromId(n.session_id);
            return d && d.dateStr === todayStr;
        });
        const priorCall = isCallNotes.find(n => n !== todaysCall);

        if (priorCall) {
            const detail = await fetch('/api/meetings/' + encodeURIComponent(priorCall.session_id)).then(r => r.json());
            yesterdayEl.innerHTML = _renderActionList(detail.actions);
        }
        if (todaysCall) {
            const detail = await fetch('/api/meetings/' + encodeURIComponent(todaysCall.session_id)).then(r => r.json());
            todayEl.innerHTML = _renderActionList(detail.actions);
        }

        // History: newest first, capped at 15 rows, with lazily-fetched action counts.
        const historyRows = isCallNotes.slice(0, 15);
        const details = await Promise.all(
            historyRows.map(n => fetch('/api/meetings/' + encodeURIComponent(n.session_id)).then(r => r.json()).catch(() => null))
        );
        historyEl.innerHTML = historyRows.map((n, i) => {
            const d = _sessionDateFromId(n.session_id);
            const actions = (details[i] && details[i].actions) || [];
            return `
            <div class="meeting-card hub-history-row" onclick="openMeetingDetail('${escHtml(n.session_id)}')">
                <div>
                    <div class="meeting-time">${d ? escHtml(d.display) : escHtml(n.session_id)}</div>
                </div>
                <span class="badge">${actions.length} action item${actions.length === 1 ? '' : 's'}</span>
            </div>`;
        }).join('');
    } catch (e) {
        historyEl.innerHTML = `<p style="color:#a6432d;">Failed to load IS call history: ${escHtml(e.message)}</p>`;
    }
}

// ── Weekly Digest (user-initiated, never auto-run) ─────────────────────────

function closeWeeklyDigest() {
    document.getElementById('weekly-digest-modal').classList.remove('open');
}

async function openWeeklyDigest() {
    const modal = document.getElementById('weekly-digest-modal');
    const body = document.getElementById('weekly-digest-body');
    modal.classList.add('open');
    body.innerHTML = '<p style="color:var(--text-muted);"><i class="fa-solid fa-spinner fa-spin"></i> Generating your weekly digest…</p>';

    try {
        const resp = await fetch('/api/summary/weekly');
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        const s = data.summary || {};
        body.innerHTML = `
            <p style="color:var(--text-muted);margin-bottom:1rem;">${escHtml(s.insight || '')}</p>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem;">
                <div class="glass-panel" style="padding:0.8rem;text-align:center;">
                    <div style="font-size:1.4rem;font-weight:700;color:var(--primary);">${s.open_action_count ?? '—'}</div>
                    <div style="font-size:0.75rem;color:var(--text-muted);">Open actions</div>
                </div>
                <div class="glass-panel" style="padding:0.8rem;text-align:center;">
                    <div style="font-size:1.4rem;font-weight:700;color:var(--success);">${s.completed_count ?? '—'}</div>
                    <div style="font-size:0.75rem;color:var(--text-muted);">Completed this week</div>
                </div>
            </div>
            <h3 style="color:var(--accent);font-size:0.95rem;margin-bottom:0.4rem;">Key Decisions</h3>
            <ul style="margin:0 0 1rem 1.2rem;color:var(--text-muted);">${(s.key_decisions || []).map(d => `<li>${escHtml(d)}</li>`).join('') || '<li>None recorded.</li>'}</ul>
            <h3 style="color:var(--accent);font-size:0.95rem;margin-bottom:0.4rem;">Recurring Topics</h3>
            <ul style="margin:0 0 1rem 1.2rem;color:var(--text-muted);">${(s.recurring_topics || []).map(t => `<li>${escHtml(t)}</li>`).join('') || '<li>None recorded.</li>'}</ul>
            <h3 style="color:#b8862e;font-size:0.95rem;margin-bottom:0.4rem;">High Priority Open</h3>
            <ul style="margin:0 0 0 1.2rem;color:var(--text-muted);">${(s.high_priority_open || []).map(t => `<li>${escHtml(t)}</li>`).join('') || '<li>None.</li>'}</ul>
        `;
    } catch (e) {
        body.innerHTML = `<p style="color:#a6432d;">Could not generate digest: ${escHtml(e.message)}</p>`;
    }
}

async function _loadRecurringBlockers() {
    const card = document.getElementById('recurring-blockers-card');
    const list = document.getElementById('recurring-blockers-list');
    if (!card) return;
    try {
        const resp = await fetch('/api/blockers/recurring');
        const data = await resp.json();
        const blockers = data.blockers || [];
        if (!blockers.length) {
            card.style.display = 'none';
            return;
        }
        card.style.display = '';
        list.innerHTML = blockers.map(b => `
            <div class="alert-card due-today" style="margin-bottom:0.75rem;">
                <i class="fa-solid fa-triangle-exclamation" style="color:#b8862e;font-size:1.2rem;"></i>
                <div>
                    <strong>${escHtml(b.theme)}</strong>
                    <div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.25rem;">
                        Seen in ${(b.occurrences || []).length} session(s) since ${escHtml(b.first_seen || '')}
                    </div>
                    <div style="font-size:0.85rem;margin-top:0.35rem;"><i class="fa-solid fa-arrow-right"></i> ${escHtml(b.suggested_action)}</div>
                </div>
            </div>`).join('');
    } catch (e) {
        card.style.display = 'none';
    }
}

// ── Type-filtered meetings (Project Meetings / Seminars tabs) ─────────────────

function _detectMeetingTypeFromSlug(sessionId) {
    // Mirrors mcp_server.meeting_type.detect_meeting_type's slug-prefix rule
    // client-side, to avoid an N+1 fetch of every session's .type file just
    // to populate these two list tabs. Default is 'general', matching the
    // backend's default (not every unrecognised slug is a project meeting).
    if (sessionId.startsWith('is-call-')) return 'is-call';
    if (sessionId.startsWith('seminar-')) return 'seminar';
    if (sessionId.startsWith('project-')) return 'project-meeting';
    return 'general';
}

async function loadTypeFilteredMeetings(type) {
    const containerId = type === 'seminar' ? 'seminars-list' : 'project-meetings-list';
    const container = document.getElementById(containerId);
    try {
        const resp = await fetch('/api/briefing');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const filtered = (data.notes || []).filter(n => _detectMeetingTypeFromSlug(n.session_id) === type);

        if (!filtered.length) {
            container.innerHTML = `<p style="color:var(--text-muted);">No ${type === 'seminar' ? 'seminars' : 'project meetings'} recorded yet.</p>`;
            return;
        }

        container.innerHTML = filtered.map(n => {
            const d = _sessionDateFromId(n.session_id);
            const htmlContent = escHtml(n.content).replace(/^- (.*$)/gim, '<li>$1</li>')
                                         .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
                                         .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
            return `
            <div class="note-card glass-panel" style="padding:1rem;cursor:pointer;"
                 onclick="openMeetingDetail('${escHtml(n.session_id)}')">
                <h4 style="margin-bottom:0.4rem;color:var(--primary);">${d ? escHtml(d.display) : ''} — ${escHtml(n.session_id)}</h4>
                <div style="font-size:0.9rem;line-height:1.5;">${htmlContent}</div>
            </div>`;
        }).join('');
    } catch (e) {
        container.innerHTML = `<p style="color:#a6432d;">Failed to load: ${escHtml(e.message)}</p>`;
    }
}

// ── Settings panel ─────────────────────────────────────────────────────────

async function loadSettings() {
    const dateInput = document.getElementById('import-cal-date');
    if (dateInput && !dateInput.value) dateInput.value = new Date().toISOString().split('T')[0];

    try {
        const resp = await fetch('/api/settings');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const radios = document.querySelectorAll('#whisper-model-radios input[type="radio"]');
        radios.forEach(r => { r.checked = (r.value === data.whisper_model); });
    } catch (e) {
        console.error('loadSettings:', e);
    }
}

async function saveSettings() {
    const statusEl = document.getElementById('settings-save-status');
    const checked = document.querySelector('#whisper-model-radios input[type="radio"]:checked');
    if (!checked) return;
    statusEl.textContent = 'Saving…';
    try {
        const resp = await fetch('/api/settings', {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ whisper_model: checked.value }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        statusEl.textContent = 'Saved.';
    } catch (e) {
        statusEl.textContent = 'Error: ' + e.message;
    }
}

// ── Manual task entry / status tracking (Tasks tab) ────────────────────────

let _taskFilter = 'all';
let _lastTasksData = null;

function toggleAddTaskForm(show) {
    const form = document.getElementById('add-task-form');
    const shouldShow = show === undefined ? form.classList.contains('hidden') : show;
    form.classList.toggle('hidden', !shouldShow);
    if (shouldShow) {
        document.getElementById('new-task-description').value = '';
        document.getElementById('new-task-due').value = '';
        document.getElementById('new-task-tag').value = '';
        document.getElementById('new-task-note').value = '';
        document.getElementById('new-task-priority').value = 'MEDIUM';
        document.getElementById('add-task-error').textContent = '';
        _validateAddTaskForm();
        document.getElementById('new-task-description').focus();
    }
}

function _validateAddTaskForm() {
    const desc = document.getElementById('new-task-description').value.trim();
    document.getElementById('btn-save-task').disabled = desc.length === 0;
}

async function saveManualTask() {
    const errorEl = document.getElementById('add-task-error');
    const description = document.getElementById('new-task-description').value.trim();
    if (!description) return;

    const payload = {
        description,
        due_date: document.getElementById('new-task-due').value || null,
        priority: document.getElementById('new-task-priority').value,
        tag: document.getElementById('new-task-tag').value.trim() || null,
        progress_note: document.getElementById('new-task-note').value.trim() || null,
    };

    const btn = document.getElementById('btn-save-task');
    btn.disabled = true;
    try {
        const resp = await fetch('/api/tasks/manual', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        toggleAddTaskForm(false);
        fetchBriefing();
    } catch (e) {
        errorEl.textContent = e.message;
        btn.disabled = false;
    }
}

function setTaskFilter(filter) {
    _taskFilter = filter;
    document.querySelectorAll('.filter-btn[data-filter]').forEach(b => {
        const active = b.dataset.filter === filter;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    if (_lastTasksData) renderFullTaskList(_lastTasksData);
}

function _filterTasks(tasks) {
    if (_taskFilter === 'all') return tasks;
    if (_taskFilter === 'active') return tasks.filter(t => (t.status || 'todo') === 'todo' || t.status === 'in_progress');
    if (_taskFilter === 'blocked') return tasks.filter(t => t.status === 'blocked');
    if (_taskFilter === 'done') return tasks.filter(t => t.status === 'done');
    return tasks;
}

function renderFullTaskList(allTasks) {
    _lastTasksData = allTasks;
    const fullTaskList = document.getElementById('full-task-list');
    const fullTaskCount = document.getElementById('full-task-count');
    if (!fullTaskList) return;

    const visible = _filterTasks(allTasks);
    if (!visible.length) {
        fullTaskList.innerHTML = '<div class="empty-state">No tasks match this filter.</div>';
        if (fullTaskCount) fullTaskCount.innerText = allTasks.length;
        return;
    }

    fullTaskList.innerHTML = visible.map(t => {
        const status = t.status || 'todo';
        const isDone = status === 'done';
        const isBlocked = status === 'blocked';
        return `
        <div class="task-card" style="${isBlocked ? 'opacity:0.6;' : ''}" id="task-row-${escHtml(t.id)}">
            <div class="task-checkbox ${isDone ? 'checked' : ''}" onclick="event.stopPropagation(); completeTask('${escHtml(t.id)}', this.closest('.task-card'))">
                <i class="fa-solid fa-check"></i>
            </div>
            <div class="task-content ${isDone ? 'completed' : ''}" style="flex:1;">
                <h4>${escHtml(t.description)}</h4>
                <div class="task-meta">
                    <span><i class="fa-solid fa-user"></i> ${escHtml(t.owner) || 'Unassigned'}</span>
                    <span><i class="fa-solid fa-calendar"></i> ${escHtml(t.due_date) || 'No date'}</span>
                    ${t.tag ? `<span><i class="fa-solid fa-tag"></i> ${escHtml(t.tag)}</span>` : ''}
                    <button class="btn-icon" style="width:22px;height:22px;" title="Edit progress note"
                            onclick="event.stopPropagation(); _toggleNoteEditor('${escHtml(t.id)}')">
                        <i class="fa-solid fa-pencil" style="font-size:0.7rem;"></i>
                    </button>
                </div>
                <div id="note-editor-${escHtml(t.id)}" class="hidden" style="margin-top:0.5rem;">
                    <input type="text" class="text-input" value="${escHtml(t.progress_note || '')}"
                           placeholder="Progress note…" onclick="event.stopPropagation();"
                           onkeydown="if(event.key==='Enter'){this.blur();}"
                           onblur="_saveProgressNote('${escHtml(t.id)}', this.value)">
                </div>
                ${t.progress_note ? `<div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.3rem;"><i class="fa-solid fa-note-sticky"></i> ${escHtml(t.progress_note)}</div>` : ''}
            </div>
            <select class="status-select" onclick="event.stopPropagation();" onchange="event.stopPropagation(); _updateTaskStatus('${escHtml(t.id)}', this.value)">
                <option value="todo" ${status === 'todo' ? 'selected' : ''}>To Do</option>
                <option value="in_progress" ${status === 'in_progress' ? 'selected' : ''}>In Progress</option>
                <option value="done" ${status === 'done' ? 'selected' : ''}>Done</option>
                <option value="blocked" ${status === 'blocked' ? 'selected' : ''}>Blocked</option>
            </select>
        </div>`;
    }).join('');
    if (fullTaskCount) fullTaskCount.innerText = allTasks.length;
}

function _toggleNoteEditor(taskId) {
    const el = document.getElementById('note-editor-' + taskId);
    if (!el) return;
    el.classList.toggle('hidden');
    if (!el.classList.contains('hidden')) el.querySelector('input').focus();
}

async function _saveProgressNote(taskId, note) {
    try {
        await fetch('/api/tasks/' + encodeURIComponent(taskId), {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ progress_note: note || null }),
        });
        fetchBriefing();
    } catch (e) {
        showFetchError('Could not save progress note: ' + e.message);
    }
}

async function _updateTaskStatus(taskId, status) {
    try {
        const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId), {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ status }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        fetchBriefing();
    } catch (e) {
        showFetchError('Could not update task status: ' + e.message);
    }
}

// ── Transcript import (Settings tab) ────────────────────────────────────────
// bugfix-01 Fix 5: this UI did not exist at all before -- the backend endpoint
// (POST /api/upload/transcript) had no corresponding frontend entry point.

let _importSelectedCalendarEvent = null;

async function searchImportCalendarEvents() {
    const dateInput = document.getElementById('import-cal-date');
    const resultsEl = document.getElementById('import-cal-results');
    const date = dateInput.value || new Date().toISOString().split('T')[0];
    dateInput.value = date;
    resultsEl.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem;">Searching…</span>';

    try {
        const resp = await fetch('/api/calendar/events?date=' + encodeURIComponent(date));
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        const events = data.events || [];
        if (!events.length) {
            resultsEl.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem;">No meetings found for this date.</span>';
            return;
        }
        resultsEl.innerHTML = events.map(e => `
            <div class="task-card" style="padding:0.6rem 0.8rem;margin-bottom:0.4rem;cursor:pointer;"
                 onclick='_selectImportCalendarEvent(${JSON.stringify(e).replace(/'/g, "&apos;")})'>
                <div class="task-content">
                    <h4 style="font-size:0.85rem;">${escHtml(e.subject || 'Untitled')}</h4>
                    <div class="task-meta"><span>${escHtml(e.date)} ${escHtml(e.start)}–${escHtml(e.end)}</span></div>
                </div>
            </div>`).join('');
    } catch (e) {
        resultsEl.innerHTML = `<span style="color:#a6432d;font-size:0.85rem;">Search failed: ${escHtml(e.message)}</span>`;
    }
}

function _selectImportCalendarEvent(event) {
    _importSelectedCalendarEvent = event;
    document.getElementById('import-cal-selected').innerHTML =
        `<i class="fa-solid fa-check"></i> Linked to: ${escHtml(event.subject || 'Untitled')} at ${escHtml(event.date)} ${escHtml(event.start)}`;
}

async function submitTranscriptImport() {
    const errorEl = document.getElementById('import-error');
    const statusEl = document.getElementById('import-status');
    errorEl.textContent = '';
    statusEl.textContent = '';

    const fileInput = document.getElementById('import-file-input');
    const file = fileInput.files && fileInput.files[0];
    if (!file) {
        errorEl.textContent = 'Choose a transcript file first.';
        return;
    }

    const btn = document.getElementById('import-transcript-btn');
    btn.disabled = true;
    statusEl.textContent = 'Importing…';
    try {
        const fd = new FormData();
        fd.append('file', file);
        fd.append('meeting_type', document.getElementById('import-type-select').value);
        const sessionId = document.getElementById('import-session-id').value.trim();
        if (sessionId) fd.append('session_id', sessionId);
        if (_importSelectedCalendarEvent) fd.append('calendar_event_id', _importSelectedCalendarEvent.id);

        const resp = await fetch('/api/upload/transcript', { method: 'POST', body: fd });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        statusEl.textContent = `Imported as ${data.session_id} (${data.segments_count} segment(s)) — processing…`;
        fileInput.value = '';
        document.getElementById('import-session-id').value = '';
        _importSelectedCalendarEvent = null;
        document.getElementById('import-cal-selected').textContent = '';
        document.getElementById('import-cal-results').innerHTML = '';
        fetchBriefing();
    } catch (e) {
        console.error('submitTranscriptImport failed:', e);
        errorEl.textContent = `Import failed: ${e.message}`;
    } finally {
        btn.disabled = false;
    }
}
