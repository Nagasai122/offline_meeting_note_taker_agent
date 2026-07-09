document.addEventListener('DOMContentLoaded', () => {
    setGreeting();
    fetchBriefing();
    setInterval(fetchBriefing, 5000); // Poll every 5s for updates

    loadStalledSessions();
    setInterval(loadStalledSessions, 5000); // Same cadence as fetchBriefing, kept separate on purpose

    document.getElementById('btn-toggle-record').addEventListener('click', toggleRecording);
    document.getElementById('btn-highlight').addEventListener('click', logHighlight);
    document.getElementById('btn-add-context').addEventListener('click', toggleAddContextPanel);
});

function setGreeting() {
    const el = document.getElementById('greeting-text');
    if (!el) return;
    const hour = new Date().getHours();
    el.textContent = hour < 12 ? 'Good morning.'
                   : hour < 18 ? 'Good afternoon.'
                   : 'Good evening.';
}

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

    // Nav "Needs Review" badge: previously only updated inside
    // loadReviewQueue() (tab-switch or 800ms after submitReview()/
    // applySession()), so a session that newly reached PROPOSED while the
    // user was on a different tab left the badge stale until they happened
    // to visit Review. Piggyback on this poll (already running every 5s)
    // instead -- backend computes the count from the same pipeline_status()
    // data loadReviewQueue's full fetch uses, so the two never disagree.
    const reviewBadge = document.getElementById('review-badge');
    if (reviewBadge && typeof data.review_pending_count === 'number') {
        reviewBadge.textContent = data.review_pending_count;
        reviewBadge.style.display = data.review_pending_count > 0 ? '' : 'none';
    }

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

// ── Stalled sessions ─────────────────────────────────────────────────────────
// A session parked at STOPPED/TRANSCRIBED/EXTRACTED (most often because the
// local LLM server was unreachable) will never advance on its own. This polls
// GET /api/sessions/stalled and renders a "Stalled" badge + Resume button for
// each one -- see cli/web.py's resume_session for what each state does.

async function loadStalledSessions() {
    try {
        const resp = await fetch('/api/sessions/stalled');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderStalledSessions(data.stalled || []);
    } catch (e) {
        // Best-effort widget: a failure here must not raise the global error
        // banner (that's reserved for /api/briefing per bugfix precedent) --
        // just leave the banner in whatever state it was last in.
        console.error('Failed to load stalled sessions:', e);
    }
}

const STALLED_STATE_LABEL = {
    STOPPED: 'Stopped — not yet transcribed',
    TRANSCRIBED: 'Transcribed — extraction did not run',
    EXTRACTED: 'Extracted — proposal was not written',
};

function renderStalledSessions(sessions) {
    const banner = document.getElementById('stalled-sessions-banner');
    const list = document.getElementById('stalled-sessions-list');
    if (!banner || !list) return;

    if (!sessions.length) {
        banner.classList.add('hidden');
        list.innerHTML = '';
        return;
    }

    banner.classList.remove('hidden');
    list.innerHTML = sessions.map(s => `
        <div class="glass-panel" style="display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:0.75rem 1rem;background:var(--card-raised);">
            <div style="flex:1;min-width:0;">
                <span class="badge" style="background:#b8862e;margin-right:0.5rem;">Stalled</span>
                <strong>${escHtml(s.session_id)}</strong>
                <div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.2rem;">
                    ${escHtml(STALLED_STATE_LABEL[s.state] || s.state)}
                </div>
            </div>
            <button class="btn-primary" style="padding:6px 14px;font-size:0.85rem;flex-shrink:0;"
                    onclick="resumeStalledSession('${escHtml(s.session_id)}', this)">
                <i class="fa-solid fa-play"></i> Resume
            </button>
        </div>
    `).join('');
}

async function resumeStalledSession(sessionId, btnElement) {
    if (btnElement) { btnElement.disabled = true; btnElement.innerText = 'Resuming…'; }
    try {
        const resp = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/resume`, { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            showFetchError(data.error || `Could not resume session '${sessionId}' (HTTP ${resp.status}).`);
            if (btnElement) { btnElement.disabled = false; btnElement.innerHTML = '<i class="fa-solid fa-play"></i> Resume'; }
            return;
        }
        // The resumed session is now "processing" -- both the pipeline banner
        // (via fetchBriefing) and the stalled list need to reflect that.
        await Promise.all([fetchBriefing(), loadStalledSessions()]);
    } catch (e) {
        showFetchError(`Could not resume session '${sessionId}': ${e.message}`);
        if (btnElement) { btnElement.disabled = false; btnElement.innerHTML = '<i class="fa-solid fa-play"></i> Resume'; }
    }
}

// ── Semantic search reindex (Settings tab) ──────────────────────────────────

async function rebuildSemanticIndex() {
    const btn = document.getElementById('btn-reindex');
    const status = document.getElementById('reindex-status');
    if (!btn || !status) return;

    btn.disabled = true;
    status.style.color = 'var(--text-muted)';
    status.innerText = 'Indexing…';
    try {
        const resp = await fetch('/api/search/reindex', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            status.style.color = '#a6432d';
            status.innerText = data.error || `HTTP ${resp.status}`;
        } else {
            const stats = Object.entries(data)
                .filter(([k]) => k !== 'status')
                .map(([k, v]) => `${k}: ${v}`)
                .join(', ');
            status.style.color = '#33513e';
            status.innerText = stats ? `Indexed (${stats}).` : 'Indexed.';
        }
    } catch (e) {
        status.style.color = '#a6432d';
        status.innerText = `Could not reach backend: ${e.message}`;
    } finally {
        btn.disabled = false;
    }
}

async function recordMeetingFromCalendar(btnElement) {
    if (isRecording) {
        showFetchError("A recording is already in progress. Stop it first before starting a new one.");
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
        showFetchError("A recording is already in progress. Stop it first before starting a new one.");
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
    if (!file) {
        // Dragging straight from Outlook (or another mail client) often hands
        // the browser a virtual attachment with no real file behind it --
        // dataTransfer.files is empty and silently "nothing happens" without this.
        document.getElementById('pmc-mail-status').textContent =
            'No file received from the drag — save the email to disk first, then drop the file.';
        return;
    }
    const name = file.name.toLowerCase();
    const docExts = ['.pdf', '.pptx', '.docx', '.txt'];
    if (docExts.some(ext => name.endsWith(ext))) {
        if (file.size > 50 * 1024 * 1024) {
            showFetchError('File exceeds the 50MB limit.');
            return;
        }
        _pmcSelectedFile = file;
        const sizeKb = Math.round(file.size / 1024);
        const el = document.getElementById('pmc-selected-file');
        el.innerHTML = `<i class="fa-solid fa-file"></i> ${escHtml(file.name)} (${sizeKb} KB)`;
        el.classList.remove('hidden');
        return;
    }
    // .eml/.msg, and files with NO extension at all (mail clients often save
    // messages with no suffix), go to the email importer — the backend
    // sniffs the content and, if it can't parse the file, returns an error
    // listing the accepted formats (shown in #pmc-mail-status). A file with
    // some OTHER recognised-but-unsupported extension (.jpg, .doc, ...) is
    // rejected immediately here instead: routing it to the mail endpoint
    // would only produce a confusing "not a recognisable email file" error
    // that omits the fact that documents are also accepted.
    const hasExtension = /\.[a-z0-9]{1,5}$/i.test(name);
    if (hasExtension && !name.endsWith('.eml') && !name.endsWith('.msg')) {
        showFetchError('Unsupported file type. Allowed: PDF, PPTX, DOCX, TXT — or an email as .eml/.msg.');
        return;
    }
    _importDroppedEmail(file);
}

async function _importDroppedEmail(file) {
    const statusEl = document.getElementById('pmc-mail-status');
    const agendaEl = document.getElementById('pmc-agenda');
    if (file.size > 50 * 1024 * 1024) {
        statusEl.textContent = 'File exceeds the 50MB limit.';
        return;
    }
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
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
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

// ── Mid-recording "Add Context" (P3.7) ──────────────────────────────────────
// Neither POST /api/context/upload nor POST /api/context/text is called with
// a session_id here -- both endpoints fall back to the server's own
// active_session_id when it's omitted (cli/web.py), the same convention
// POST /api/record/highlight already uses. That means this panel needs no
// client-side session tracking at all, matching the Highlight button right
// next to it.

const _ADD_CONTEXT_ACCEPT = '.pdf,.pptx,.docx,.xlsx,.txt,.png,.jpg,.jpeg';

function toggleAddContextPanel() {
    const existing = document.getElementById('add-context-panel');
    if (existing) {
        _closeAddContextPanel();
        return;
    }
    const btn = document.getElementById('btn-add-context');
    const wrap = document.createElement('div');
    wrap.id = 'add-context-panel';
    wrap.className = 'glass-panel';
    // Appended to <body> and positioned fixed, rather than inserted inline
    // next to the button, deliberately: #btn-add-context lives inside the
    // header's narrow recording-controls card, and a panel constrained to
    // that flex item's width (~150px) squeezed the textarea/buttons down to
    // the point of being unusable. A floating popover anchored under the
    // button isn't fighting that parent's width at all.
    wrap.style.cssText = 'position:fixed;z-index:900;width:320px;max-width:calc(100vw - 2rem);'
        + 'padding:0.9rem;display:flex;flex-direction:column;gap:0.6rem;box-shadow:0 8px 24px rgba(0,0,0,0.25);';
    wrap.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <strong style="font-size:0.85rem;">Add context to this recording</strong>
            <button class="btn-icon" style="width:24px;height:24px;" aria-label="Close" onclick="_closeAddContextPanel()">
                <i class="fa-solid fa-xmark"></i>
            </button>
        </div>
        <textarea id="add-context-text-input" class="textarea-input" rows="2" maxlength="20000"
                  placeholder="Paste text (e.g. a Teams chat snippet)…"></textarea>
        <div style="display:flex;gap:0.5rem;flex-wrap:wrap;align-items:center;">
            <button id="btn-add-context-text-submit" class="btn-secondary" style="padding:6px 12px;font-size:0.8rem;"
                    onclick="_submitAddContextText()">
                <i class="fa-solid fa-paper-plane"></i> Add pasted text
            </button>
            <label class="btn-secondary" style="padding:6px 12px;font-size:0.8rem;cursor:pointer;margin:0;">
                <i class="fa-solid fa-upload"></i> Upload file
                <input type="file" id="add-context-file-input" accept="${_ADD_CONTEXT_ACCEPT}" style="display:none;"
                       onchange="_submitAddContextFile(this)">
            </label>
        </div>
        <div id="add-context-status" style="font-size:0.8rem;color:var(--text-muted);"></div>
    `;
    document.body.appendChild(wrap);

    // Position under the button, flipped to the left edge if it would
    // otherwise overflow the right side of the viewport.
    const rect = btn.getBoundingClientRect();
    const panelWidth = wrap.offsetWidth;
    let left = rect.left;
    if (left + panelWidth > window.innerWidth - 16) left = window.innerWidth - panelWidth - 16;
    wrap.style.top = `${rect.bottom + 8}px`;
    wrap.style.left = `${Math.max(16, left)}px`;

    document.getElementById('add-context-text-input').focus();
    // Close on an outside click, but not the click that just opened it.
    setTimeout(() => document.addEventListener('click', _onAddContextOutsideClick), 0);
}

function _onAddContextOutsideClick(e) {
    const wrap = document.getElementById('add-context-panel');
    const btn = document.getElementById('btn-add-context');
    if (!wrap) return;
    if (wrap.contains(e.target) || (btn && btn.contains(e.target))) return;
    _closeAddContextPanel();
}

function _closeAddContextPanel() {
    const existing = document.getElementById('add-context-panel');
    if (existing) existing.remove();
    document.removeEventListener('click', _onAddContextOutsideClick);
}

function _setAddContextStatus(message, isError) {
    const el = document.getElementById('add-context-status');
    if (!el) return;
    el.textContent = message;
    el.style.color = isError ? '#a6432d' : '#33513e';
}

async function _submitAddContextText() {
    const input = document.getElementById('add-context-text-input');
    const text = input ? input.value.trim() : '';
    if (!text) return;
    const btn = document.getElementById('btn-add-context-text-submit');
    if (btn) btn.disabled = true;
    _setAddContextStatus('Adding…', false);
    try {
        const resp = await fetch('/api/context/text', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ text }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        input.value = '';
        _setAddContextStatus('Added.', false);
    } catch (e) {
        _setAddContextStatus(`Could not add context: ${e.message}`, true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function _submitAddContextFile(fileInput) {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    _setAddContextStatus(`Uploading ${file.name}…`, false);
    try {
        const fd = new FormData();
        fd.append('file', file);
        const resp = await fetch('/api/context/upload', { method: 'POST', body: fd });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        _setAddContextStatus(`Added ${file.name}.`, false);
    } catch (e) {
        _setAddContextStatus(`Could not add ${file.name}: ${e.message}`, true);
    } finally {
        fileInput.value = '';
    }
}

async function syncCalendar(buttonId, statusId) {
    // Generalized to take an explicit button/status pair so both the
    // Dashboard tab's and the Calendar tab's Sync buttons can share one
    // implementation (previously the Calendar tab had no Sync button at all
    // -- see the #btn-sync-calendar-full markup in index.html). Defaults
    // keep any other/legacy caller working unchanged.
    const btn = document.getElementById(buttonId || 'btn-sync-calendar');
    const statusEl = document.getElementById(statusId || 'sync-status-dashboard');
    if (btn) btn.disabled = true;
    if (statusEl) statusEl.textContent = 'Syncing…';
    try {
        const response = await fetch('/api/calendar/sync', { method: 'POST' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        // fetchBriefing() repopulates both #calendar-list (Dashboard) and
        // #full-calendar-list (Calendar tab) regardless of which button
        // triggered the sync, so either entry point refreshes both views.
        await fetchBriefing();
        if (statusEl) {
            statusEl.textContent = `Synced ${data.count ?? 0} event${data.count === 1 ? '' : 's'}`;
            setTimeout(() => { if (statusEl.textContent.startsWith('Synced')) statusEl.textContent = ''; }, 4000);
        }
    } catch (e) {
        showFetchError(`Calendar sync failed: ${e.message}`);
        if (statusEl) statusEl.textContent = 'Sync failed';
    } finally {
        if (btn) btn.disabled = false;
    }
}

let liveTranscriptEventSource = null;
let _liveFrameParseWarned = false;
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
    const btnAddContext = document.getElementById('btn-add-context');
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
        if (btnAddContext) btnAddContext.classList.remove('hidden');
        if (elapsedEl) {
            elapsedEl.classList.remove('hidden');
            _tickElapsed();
            if (_elapsedTimerHandle === null) _elapsedTimerHandle = setInterval(_tickElapsed, 1000);
        }
        if (liveTranscriptPanel) liveTranscriptPanel.classList.remove('hidden');
        
        if (!liveTranscriptEventSource) {
            liveTranscriptEventSource = new EventSource('/api/record/live');
            _liveFrameParseWarned = false;
            liveTranscriptEventSource.onmessage = function(event) {
                let data;
                try {
                    data = JSON.parse(event.data);
                } catch (e) {
                    // A single malformed SSE frame must not kill the whole
                    // stream handler — skip it (and warn only once per stream
                    // to avoid flooding the console).
                    if (!_liveFrameParseWarned) {
                        _liveFrameParseWarned = true;
                        console.warn('Skipping malformed live-transcript frame:', event.data);
                    }
                    return;
                }
                if (data.text && data.text.trim().length > 0) {
                    liveTranscriptText.innerText = data.text;
                    liveTranscriptText.scrollTop = liveTranscriptText.scrollHeight;
                } else if (isRecording) {
                    // Empty text = model loaded and waiting for speech
                    liveTranscriptText.innerText = 'Listening...';
                }
            };
            liveTranscriptEventSource.onerror = function() {
                // The browser reconnects EventSource automatically — do NOT
                // close the source here, just tell the user what's happening.
                if (liveTranscriptText) {
                    liveTranscriptText.innerText = 'Live transcript stream interrupted — reconnecting…';
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
        if (btnAddContext) btnAddContext.classList.add('hidden');
        _closeAddContextPanel();
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
    if (tabId === 'projects') loadProjectsTab();
}

// ── Review / Apply UI ─────────────────────────────────────────────────────────

// session_id → accepted_count from the last submitReview in this page session;
// used by applySession's confirmation dialog.
const _acceptedCountBySession = {};

// P2.5: the same 8-value owner_type vocabulary the extraction pipeline
// classifies against (mcp_server/tools/extraction.py's _VALID_OWNER_TYPES) --
// kept in sync by hand since the frontend has no import from the Python side.
const _OWNER_TYPES = ['unknown', 'self', 'institution', 'partner', 'organisation', 'consortium', 'all_partners', 'external'];
const _OWNER_TYPE_LABELS = {
    unknown: 'Unknown', self: 'Me', institution: 'My Institution', partner: 'Partner Org',
    organisation: 'My Org (entity)', consortium: 'Consortium', all_partners: 'All Partners', external: 'External',
};

// Cached across calls within one page load -- the active project list rarely
// changes mid-review-session, and re-fetching on every loadReviewQueue() poll
// would be wasted work for a picker that's just a few dozen entries at most.
let _activeProjectsCache = null;
async function _getActiveProjects() {
    if (_activeProjectsCache) return _activeProjectsCache;
    try {
        const resp = await fetch('/api/projects');
        if (!resp.ok) return [];
        const data = await resp.json();
        _activeProjectsCache = (data.projects || []).filter(p => p.status === 'active');
        return _activeProjectsCache;
    } catch (e) {
        console.error('_getActiveProjects:', e);
        return [];
    }
}

async function loadReviewQueue() {
    const loading = document.getElementById('review-loading');
    if (loading) loading.style.display = '';
    try {
        const [resp, activeProjects] = await Promise.all([fetch('/api/review/pending'), _getActiveProjects()]);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (loading) loading.style.display = 'none';
        renderReviewAwaiting(data.awaiting_review || [], activeProjects);
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

function renderReviewAwaiting(sessions, activeProjects) {
    const container = document.getElementById('review-awaiting-list');
    if (!container) return;
    if (!sessions.length) {
        container.innerHTML = '<p style="color:var(--text-muted);">No sessions awaiting review.</p>';
        return;
    }
    container.innerHTML = sessions.map(s => `
        <div class="glass-panel" style="margin-bottom:1.5rem;padding:1.25rem;" id="session-block-${escHtml(s.session_id)}">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:0.5rem;flex-wrap:wrap;margin-bottom:0.5rem;">
                <strong style="color:var(--primary);">Session: ${escHtml(s.session_id)}</strong>
                <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;">
                    <button class="btn-secondary" onclick="setAllReviewDecisions('${escHtml(s.session_id)}', 'accept')"
                            style="padding:6px 12px;font-size:0.8rem;">
                        <i class="fa-solid fa-check-double"></i> Accept all
                    </button>
                    <button class="btn-secondary" onclick="setAllReviewDecisions('${escHtml(s.session_id)}', 'reject')"
                            style="padding:6px 12px;font-size:0.8rem;color:#a6432d;">
                        <i class="fa-solid fa-xmark"></i> Reject all
                    </button>
                    <button class="btn-primary" onclick="submitReview('${escHtml(s.session_id)}')"
                            style="padding:6px 16px;font-size:0.85rem;">
                        <i class="fa-solid fa-paper-plane"></i> Submit Review
                    </button>
                </div>
            </div>
            ${(s.quality_label) ? `<div style="margin-bottom:0.75rem;">${_qualityBadge(s.quality_label, s.quality_score, s.quality_flags)}</div>` : ''}
            ${s.items.length === 0
                ? '<p style="color:var(--text-muted);">No items in draft.</p>'
                : s.items.map(item => renderReviewItem(s.session_id, item, activeProjects || [])).join('')
            }
        </div>
    `).join('');
}

function renderReviewItem(sessionId, item, activeProjects) {
    // Each item gets Accept/Reject radio + editable owner + due_date, plus
    // (P2.5) the extraction pipeline's ownership classification -- shown as
    // a confidence badge and editable owner_type/project select controls,
    // so a wrong or low-confidence classification can be corrected before
    // accepting rather than only after, via a separate task-edit pass.
    const safeId = escHtml(item.id);
    const safeDesc = escHtml(item.description);
    const safeOwner = escHtml(item.owner || '');
    const safeDue = escHtml(item.due_date || '');
    const ownerType = _OWNER_TYPES.includes(item.owner_type) ? item.owner_type : 'unknown';
    const ownerTypeOptions = _OWNER_TYPES.map(
        t => `<option value="${t}" ${t === ownerType ? 'selected' : ''}>${escHtml(_OWNER_TYPE_LABELS[t])}</option>`
    ).join('');
    const projectOptions = ['<option value="">No project</option>'].concat(
        (activeProjects || []).map(p => `<option value="${escHtml(p.id)}" ${p.id === item.project_id ? 'selected' : ''}>${escHtml(p.name)}</option>`)
    ).join('');
    const confidenceBadge = item.confidence != null
        ? `<span style="font-size:0.72rem;color:var(--text-muted);margin-left:0.4rem;" title="Extraction confidence">${Math.round(item.confidence * 100)}%</span>`
        : '';
    const origOwnerType = escHtml(ownerType);
    const origConfidence = item.confidence != null ? item.confidence : '';
    const origInstitution = escHtml(item.institution || '');
    return `
        <div class="task-card" id="item-${safeId}" style="margin-bottom:0.75rem;padding:0.75rem;border-left:3px solid var(--primary);"
             data-orig-owner-type="${origOwnerType}" data-orig-confidence="${origConfidence}" data-orig-institution="${origInstitution}">
            <div style="display:flex;align-items:flex-start;gap:0.75rem;">
                <div style="flex:1;">
                    <div style="font-weight:500;margin-bottom:0.4rem;">${safeDesc}</div>
                    ${item.evidence ? `<div class="evidence-quote">“${escHtml(item.evidence)}”</div>` : ''}
                    <div style="display:flex;gap:0.75rem;flex-wrap:wrap;align-items:center;">
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
                        <label style="font-size:0.8rem;color:var(--text-muted);">Ownership:
                            <select id="ownertype-${safeId}"
                                    style="background:var(--card-raised);border:1px solid var(--hairline-strong);
                                           border-radius:3px;color:var(--ink);padding:2px 6px;font-size:0.8rem;">
                                ${ownerTypeOptions}
                            </select>${confidenceBadge}
                        </label>
                        <label style="font-size:0.8rem;color:var(--text-muted);">Project:
                            <select id="projectid-${safeId}"
                                    style="background:var(--card-raised);border:1px solid var(--hairline-strong);
                                           border-radius:3px;color:var(--ink);padding:2px 6px;font-size:0.8rem;">
                                ${projectOptions}
                            </select>
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

function _setReviewItemDecision(itemId, decision) {
    // Single source of truth for flipping one item's Accept/Reject state —
    // checking the radio is exactly what an individual click does, so the
    // bulk buttons below produce the same visual + submit state per item.
    const radio = document.querySelector(`input[name="dec-${itemId}"][value="${decision}"]`);
    if (radio) radio.checked = true;
}

function setAllReviewDecisions(sessionId, decision) {
    const block = document.getElementById('session-block-' + sessionId);
    if (!block) return;
    block.querySelectorAll('[id^="item-"]').forEach(el => {
        _setReviewItemDecision(el.id.replace('item-', ''), decision);
    });
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
        const ownerTypeEl = block.querySelector(`#ownertype-${rawId}`);
        const projectIdEl = block.querySelector(`#projectid-${rawId}`);
        // Recover description from the rendered text node
        const descEl  = el.querySelector('div[style*="font-weight"]');
        const ownerType = ownerTypeEl ? ownerTypeEl.value : null;
        // P2.5: if the reviewer changed owner_type from what extraction
        // classified, treat that as a human-confirmed correction (confidence
        // 1.0) rather than silently keeping the model's original confidence
        // score attached to a value the model didn't actually produce.
        const origOwnerType = el.dataset.origOwnerType || 'unknown';
        const origConfidence = el.dataset.origConfidence !== '' ? parseFloat(el.dataset.origConfidence) : null;
        const confidence = (ownerType && ownerType !== origOwnerType) ? 1.0 : origConfidence;
        decisions.push({
            id: rawId,
            decision,
            description: descEl ? descEl.textContent.trim() : '',
            owner: ownerEl ? (ownerEl.value.trim() || null) : null,
            due_date: dueEl ? (dueEl.value.trim() || null) : null,
            owner_type: ownerType,
            project_id: projectIdEl ? (projectIdEl.value || null) : null,
            institution: el.dataset.origInstitution || null,
            confidence,
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
        // Remember how many items were accepted so the Apply confirmation can
        // say "Apply N accepted item(s)" (the awaiting-apply endpoint only
        // returns session ids, not per-item decisions).
        _acceptedCountBySession[sessionId] = data.accepted_count;
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
    // Confirm before writing to todo.md. The accepted count is only known if
    // the review was submitted in this page session; otherwise fall back to a
    // generic phrasing rather than guessing a number.
    const acceptedCount = _acceptedCountBySession[sessionId];
    const confirmed = await showConfirmModal({
        title: 'Apply to todo.md',
        body: acceptedCount != null
            ? `Apply ${acceptedCount} accepted item(s) to todo.md?`
            : 'Apply the accepted item(s) of this session to todo.md?',
        confirmLabel: 'Apply',
    });
    if (!confirmed) return;
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
    window._currentDetailSession = sessionId;
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
    const confirmModal = document.getElementById('confirm-modal');
    if (confirmModal && e.target === confirmModal) _settleConfirmModal(false);
});

// Close modal on Escape key
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        closeMeetingDetail();
        _settleConfirmModal(false);
    }
});

// ── Reusable confirm modal (replaces native confirm()) ────────────────────────

let _confirmModalResolve = null;

// showConfirmModal({title, body, confirmLabel, danger}) → Promise<boolean>
// Resolves true on confirm; false on Cancel, backdrop click, or Escape.
function showConfirmModal({ title, body, confirmLabel, danger } = {}) {
    const modal = document.getElementById('confirm-modal');
    if (!modal) return Promise.resolve(false);
    // Settle any confirm already on screen so its promise never dangles.
    _settleConfirmModal(false);
    document.getElementById('confirm-modal-title').textContent = title || 'Are you sure?';
    document.getElementById('confirm-modal-body').textContent = body || '';
    const okBtn = document.getElementById('confirm-modal-ok');
    okBtn.textContent = confirmLabel || 'Confirm';
    okBtn.style.background = danger ? '#a6432d' : '';
    modal.classList.add('open');
    okBtn.focus();
    return new Promise(resolve => { _confirmModalResolve = resolve; });
}

function _settleConfirmModal(result) {
    const modal = document.getElementById('confirm-modal');
    if (!modal || !modal.classList.contains('open')) return;
    modal.classList.remove('open');
    if (_confirmModalResolve) {
        const resolve = _confirmModalResolve;
        _confirmModalResolve = null;
        resolve(result);
    }
}

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

        // P3.6: OCR health-check -- lets a missing Tesseract install be
        // noticed here, ahead of time, rather than only the first time
        // someone tries to add a screenshot as context mid-recording.
        const ocrEl = el('stat-ocr');
        if (ocrEl) {
            if (d.ocr_available) {
                ocrEl.textContent = 'Available';
                ocrEl.style.color = '#33513e';
            } else {
                ocrEl.textContent = 'Not installed';
                ocrEl.style.color = '#a6432d';
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
        const d = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(d.error || `HTTP ${resp.status}`);
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
        const d = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(d.error || `HTTP ${resp.status}`);
        if (msg) msg.textContent = d.status === 'not_running' ? 'LLM server was not running.' : 'Stopped.';
        loadSystemStatus();
    } catch (e) {
        if (msg) msg.textContent = 'Error: ' + e.message;
    }
}

async function confirmResetData() {
    const statusEl = document.getElementById('reset-status');
    const confirmed = await showConfirmModal({
        title: 'Reset All Data',
        body: 'Delete ALL meeting records, session state, pending reviews, and clear todo.md? This cannot be undone.',
        confirmLabel: 'Delete everything',
        danger: true,
    });
    if (!confirmed) return;
    if (statusEl) statusEl.textContent = 'Resetting…';
    try {
        const resp = await fetch('/api/data/reset', { method: 'POST' });
        const d = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(d.error || `HTTP ${resp.status}`);
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
        const diar = document.getElementById('diarisation-toggle');
        if (diar) diar.checked = !!data.diarisation_enabled;
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
            body: JSON.stringify({
                whisper_model: checked.value,
                diarisation_enabled: !!(document.getElementById('diarisation-toggle') || {}).checked,
            }),
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
        document.getElementById('new-task-title').value = '';
        document.getElementById('new-task-description').value = '';
        document.getElementById('new-task-owner').value = '';
        document.getElementById('new-task-project').value = '';
        document.getElementById('new-task-due').value = '';
        document.getElementById('new-task-reminder').value = '';
        document.getElementById('new-task-tag').value = '';
        document.getElementById('new-task-note').value = '';
        document.getElementById('new-task-priority').value = 'MEDIUM';
        document.getElementById('new-task-status').value = 'todo';
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
        title: document.getElementById('new-task-title').value.trim() || null,
        owner: document.getElementById('new-task-owner').value.trim() || null,
        project_id: document.getElementById('new-task-project').value.trim() || null,
        due_date: document.getElementById('new-task-due').value || null,
        reminder_date: document.getElementById('new-task-reminder').value || null,
        priority: document.getElementById('new-task-priority').value,
        status: document.getElementById('new-task-status').value,
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

// P2.6: default mapping from the extraction pipeline's 8-value owner_type
// vocabulary (mcp_server/tools/extraction.py's _VALID_OWNER_TYPES) onto the
// 3 primary cross-project views the roadmap asked for. Not user-configurable
// today -- a deliberate, documented default rather than an open question left
// unresolved: "self" is unambiguously mine; "institution" and "organisation"
// both describe the user's own institution (as a named colleague vs. as an
// entity) so both bucket under Institution; "partner"/"consortium"/
// "all_partners" all describe ownership outside the user's own institution
// so all bucket under Partner. "external" and "unknown" deliberately map to
// no bucket -- surfacing a low-confidence or out-of-project classification
// as if it were confidently "mine" or affiliated would be misleading; those
// items are still visible under the "All" filter.
function _ownerBucket(ownerType) {
    if (ownerType === 'self') return 'mine';
    if (ownerType === 'institution' || ownerType === 'organisation') return 'institution';
    if (ownerType === 'partner' || ownerType === 'consortium' || ownerType === 'all_partners') return 'partner';
    return null;
}

function _todayIso() {
    return new Date().toISOString().slice(0, 10);
}

function _isOverdue(t) {
    return !!t.due_date && t.status !== 'done' && t.due_date < _todayIso();
}

function _isUpcoming(t) {
    if (!t.due_date || t.status === 'done') return false;
    const in7 = new Date();
    in7.setDate(in7.getDate() + 7);
    return t.due_date >= _todayIso() && t.due_date <= in7.toISOString().slice(0, 10);
}

function _filterTasks(tasks) {
    if (_taskFilter === 'all') return tasks;
    if (_taskFilter === 'active') return tasks.filter(t => (t.status || 'todo') === 'todo' || t.status === 'in_progress');
    if (_taskFilter === 'blocked') return tasks.filter(t => t.status === 'blocked');
    if (_taskFilter === 'done') return tasks.filter(t => t.status === 'done');
    if (_taskFilter === 'mine' || _taskFilter === 'institution' || _taskFilter === 'partner') {
        return tasks.filter(t => _ownerBucket(t.owner_type) === _taskFilter);
    }
    if (_taskFilter === 'overdue') return tasks.filter(_isOverdue);
    if (_taskFilter === 'upcoming') return tasks.filter(_isUpcoming);
    return tasks;
}

function _taskCardHtml(t) {
    const status = t.status || 'todo';
    const isDone = status === 'done';
    const isBlocked = status === 'blocked';
    return `
    <div class="task-card" style="${isBlocked ? 'opacity:0.6;' : ''}" id="task-row-${escHtml(t.id)}">
        <div class="task-checkbox ${isDone ? 'checked' : ''}" onclick="event.stopPropagation(); completeTask('${escHtml(t.id)}', this.closest('.task-card'))">
            <i class="fa-solid fa-check"></i>
        </div>
        <div class="task-content ${isDone ? 'completed' : ''}" style="flex:1;cursor:pointer;" onclick="openTaskDetail('${escHtml(t.id)}')" title="Click to view/edit full details">
            <h4>${escHtml(t.title || t.description)}</h4>
            ${t.title ? `<div style="font-size:0.82rem;color:var(--text-muted);margin-top:-0.2rem;margin-bottom:0.3rem;">${escHtml(t.description)}</div>` : ''}
            <div class="task-meta">
                <span><i class="fa-solid fa-user"></i> ${escHtml(t.owner) || 'Unassigned'}</span>
                <span><i class="fa-solid fa-calendar"></i> ${escHtml(t.due_date) || 'No date'}</span>
                ${t.tag ? `<span><i class="fa-solid fa-tag"></i> ${escHtml(t.tag)}</span>` : ''}
                ${t.session_id ? `<a class="session-link" title="Open the meeting this task came from"
                        onclick="event.stopPropagation(); openMeetingDetail('${escHtml(t.session_id)}')">
                        <i class="fa-solid fa-link"></i> ${escHtml(t.session_id)}</a>` : ''}
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
            ${t.evidence ? `<div class="evidence-quote">“${escHtml(t.evidence)}”</div>` : ''}
        </div>
        <select class="status-select" onclick="event.stopPropagation();" onchange="event.stopPropagation(); _updateTaskStatus('${escHtml(t.id)}', this.value)">
            <option value="todo" ${status === 'todo' ? 'selected' : ''}>To Do</option>
            <option value="in_progress" ${status === 'in_progress' ? 'selected' : ''}>In Progress</option>
            <option value="done" ${status === 'done' ? 'selected' : ''}>Done</option>
            <option value="blocked" ${status === 'blocked' ? 'selected' : ''}>Blocked</option>
        </select>
    </div>`;
}

// Priority grouping (roadmap item 5): High/Medium/Low sections, with a final
// "No Priority" bucket for items that have never had one set (extraction
// always assigns HIGH/MEDIUM/LOW, but older/manual items may predate that).
const _PRIORITY_ORDER = ['HIGH', 'MEDIUM', 'LOW', null];
const _PRIORITY_LABELS = { HIGH: 'High Priority', MEDIUM: 'Medium Priority', LOW: 'Low Priority', null: 'No Priority' };

function renderFullTaskList(allTasks) {
    _lastTasksData = allTasks;
    const fullTaskList = document.getElementById('full-task-list');
    const fullTaskCount = document.getElementById('full-task-count');
    if (!fullTaskList) return;

    const visible = _filterTasks(allTasks);
    if (!allTasks.length) {
        fullTaskList.innerHTML = '<div class="empty-state">No tasks yet. Click "Add Task" to create one.</div>';
        if (fullTaskCount) fullTaskCount.innerText = '0';
        return;
    }
    if (!visible.length) {
        fullTaskList.innerHTML = '<div class="empty-state">No tasks match this filter.</div>';
        if (fullTaskCount) fullTaskCount.innerText = allTasks.length;
        return;
    }

    const groups = new Map(_PRIORITY_ORDER.map(p => [p, []]));
    visible.forEach(t => {
        const key = _PRIORITY_ORDER.includes(t.priority) ? t.priority : null;
        groups.get(key).push(t);
    });

    fullTaskList.innerHTML = _PRIORITY_ORDER
        .filter(p => groups.get(p).length > 0)
        .map(p => `
            <div class="task-priority-group">
                <h4 class="task-priority-group-heading priority-${(p || 'none').toLowerCase()}">${_PRIORITY_LABELS[p]}
                    <span class="badge">${groups.get(p).length}</span>
                </h4>
                ${groups.get(p).map(_taskCardHtml).join('')}
            </div>
        `).join('');
    if (fullTaskCount) fullTaskCount.innerText = allTasks.length;
}

// ── Projects tab (P2.6) ──────────────────────────────────────────────────────

function toggleAddProjectForm(show) {
    const form = document.getElementById('add-project-form');
    const shouldShow = show === undefined ? form.classList.contains('hidden') : show;
    form.classList.toggle('hidden', !shouldShow);
    if (shouldShow) {
        document.getElementById('new-project-name').value = '';
        document.getElementById('new-project-institutions').value = '';
        document.getElementById('new-project-partners').value = '';
        document.getElementById('new-project-description').value = '';
        document.getElementById('add-project-error').textContent = '';
        _validateAddProjectForm();
        document.getElementById('new-project-name').focus();
    }
}

function _validateAddProjectForm() {
    const name = document.getElementById('new-project-name').value.trim();
    document.getElementById('btn-save-project').disabled = name.length === 0;
}

function _splitCommaList(value) {
    return value.split(',').map(s => s.trim()).filter(Boolean);
}

async function saveNewProject() {
    const errorEl = document.getElementById('add-project-error');
    const name = document.getElementById('new-project-name').value.trim();
    if (!name) return;

    const payload = {
        name,
        institutions: _splitCommaList(document.getElementById('new-project-institutions').value),
        partners: _splitCommaList(document.getElementById('new-project-partners').value),
        description: document.getElementById('new-project-description').value.trim() || null,
    };

    const btn = document.getElementById('btn-save-project');
    btn.disabled = true;
    errorEl.textContent = '';
    try {
        const resp = await fetch('/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        toggleAddProjectForm(false);
        // A newly created project must show up in the review UI's project
        // picker too, not just here -- drop the cache so the next
        // loadReviewQueue() re-fetches instead of serving the stale list.
        _activeProjectsCache = null;
        loadProjectsTab();
    } catch (e) {
        errorEl.textContent = e.message;
        btn.disabled = false;
    }
}

let _lastProjectsData = [];

async function loadProjectsTab() {
    const list = document.getElementById('projects-list');
    const countEl = document.getElementById('projects-count');
    if (!list) return;
    try {
        const resp = await fetch('/api/projects');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        _lastProjectsData = data.projects || [];
        if (countEl) countEl.innerText = _lastProjectsData.filter(p => p.status === 'active').length;
        renderProjectsList(_lastProjectsData);
    } catch (e) {
        list.innerHTML = `<div class="empty-state">Failed to load projects: ${escHtml(e.message)}</div>`;
        console.error('loadProjectsTab:', e);
    }
}

function renderProjectsList(projects) {
    const list = document.getElementById('projects-list');
    if (!list) return;
    if (!projects.length) {
        list.innerHTML = '<div class="empty-state">No projects yet. Click "New Project" to create one.</div>';
        return;
    }
    // Active projects first, then archived -- archived ones stay visible
    // (never deleted, per the soft-delete convention) but shouldn't compete
    // for attention at the top of the list.
    const ordered = [...projects].sort((a, b) => (a.status === b.status) ? 0 : (a.status === 'active' ? -1 : 1));
    list.innerHTML = ordered.map(p => renderProjectCard(p)).join('');
}

function renderProjectCard(p) {
    const safeId = escHtml(p.id);
    const isArchived = p.status === 'archived';
    return `
        <div class="glass-panel" id="project-card-${safeId}" style="margin-bottom:1rem;padding:1rem;${isArchived ? 'opacity:0.6;' : ''}">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:0.5rem;flex-wrap:wrap;">
                <div>
                    <div style="font-weight:600;font-size:1.02rem;">${escHtml(p.name)}${isArchived ? ' <span class="stamp" style="font-size:0.7rem;">Archived</span>' : ''}</div>
                    ${p.institutions.length ? `<div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.25rem;"><i class="fa-solid fa-building"></i> ${p.institutions.map(escHtml).join(', ')}</div>` : ''}
                    ${p.partners.length ? `<div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.15rem;"><i class="fa-solid fa-handshake"></i> ${p.partners.map(escHtml).join(', ')}</div>` : ''}
                    ${p.description ? `<div style="font-size:0.85rem;margin-top:0.4rem;">${escHtml(p.description)}</div>` : ''}
                </div>
                <div style="display:flex;gap:0.5rem;flex-shrink:0;">
                    <button class="btn-secondary" style="padding:6px 12px;font-size:0.8rem;" onclick="toggleProjectTasks('${safeId}')">
                        <i class="fa-solid fa-list-check"></i> Tasks
                    </button>
                    ${!isArchived ? `
                        <button class="btn-secondary" style="padding:6px 12px;font-size:0.8rem;" onclick="archiveProject('${safeId}', '${escHtml(p.name).replace(/'/g, "\\'")}')">
                            <i class="fa-solid fa-box-archive"></i> Archive
                        </button>` : ''}
                </div>
            </div>
            <div id="project-tasks-${safeId}" class="hidden" style="margin-top:0.75rem;border-top:1px solid var(--hairline-strong);padding-top:0.75rem;"></div>
        </div>`;
}

async function toggleProjectTasks(projectId) {
    const panel = document.getElementById('project-tasks-' + projectId);
    if (!panel) return;
    const willShow = panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !willShow);
    if (!willShow || panel.dataset.loaded === '1') return;
    panel.innerHTML = '<div class="loading-shimmer"></div>';
    try {
        const resp = await fetch(`/api/projects/${projectId}/tasks`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const tasks = data.tasks || [];
        panel.innerHTML = tasks.length
            ? tasks.map(_taskCardHtml).join('')
            : '<div class="empty-state">No tasks linked to this project yet.</div>';
        panel.dataset.loaded = '1';
    } catch (e) {
        panel.innerHTML = `<div class="empty-state">Failed to load tasks: ${escHtml(e.message)}</div>`;
    }
}

async function archiveProject(projectId, projectName) {
    const confirmed = await showConfirmModal({
        title: 'Archive project',
        body: `Archive "${projectName}"? Existing tasks will keep their link to it, but it will no longer appear as an option for new tasks.`,
        confirmLabel: 'Archive',
    });
    if (!confirmed) return;
    try {
        const resp = await fetch(`/api/projects/${projectId}`, { method: 'DELETE' });
        if (!resp.ok) {
            const data = await resp.json();
            throw new Error(data.error || `HTTP ${resp.status}`);
        }
        _activeProjectsCache = null;
        loadProjectsTab();
    } catch (e) {
        showFetchError('Could not archive project: ' + e.message);
    }
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

// ── Task detail side panel (P1.5) ───────────────────────────────────────────

let _taskDetailId = null;

async function openTaskDetail(taskId) {
    _taskDetailId = taskId;
    document.getElementById('task-detail-error').textContent = '';
    try {
        const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId));
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const t = await resp.json();

        document.getElementById('task-detail-title').textContent = t.title || t.description;
        document.getElementById('td-title').value = t.title || '';
        document.getElementById('td-description').value = t.description || '';
        document.getElementById('td-owner').value = t.owner || '';
        document.getElementById('td-project').value = t.project_id || '';
        document.getElementById('td-due').value = t.due_date || '';
        document.getElementById('td-reminder').value = t.reminder_date || '';
        document.getElementById('td-priority').value = t.priority || 'MEDIUM';
        document.getElementById('td-status').value = t.status || 'todo';
        document.getElementById('td-tag').value = t.tag || '';
        document.getElementById('td-institution').value = t.institution || '';
        document.getElementById('td-notes').value = t.progress_note || '';

        const sourceEl = document.getElementById('task-detail-source-meeting');
        if (t.session_id) {
            sourceEl.style.display = '';
            sourceEl.innerHTML = `<a class="session-link" onclick="openMeetingDetail('${escHtml(t.session_id)}')">
                <i class="fa-solid fa-link"></i> View source meeting (${escHtml(t.session_id)})</a>`;
        } else {
            sourceEl.style.display = 'none';
        }

        _renderTaskAttachments(t.attachments || []);
        _renderTaskComments(t.comments || []);

        document.getElementById('task-detail-overlay').classList.remove('hidden');
        document.getElementById('task-detail-panel').classList.remove('hidden');
    } catch (e) {
        showFetchError('Could not load task: ' + e.message);
    }
}

function closeTaskDetail() {
    _taskDetailId = null;
    document.getElementById('task-detail-overlay').classList.add('hidden');
    document.getElementById('task-detail-panel').classList.add('hidden');
}

function _renderTaskAttachments(attachments) {
    const el = document.getElementById('task-detail-attachments');
    if (!attachments.length) {
        el.innerHTML = '<div style="font-size:0.82rem;color:var(--text-muted);">No attachments yet.</div>';
        return;
    }
    el.innerHTML = attachments.map(a => `
        <div style="display:flex;align-items:center;gap:0.5rem;font-size:0.85rem;">
            <i class="fa-solid fa-file"></i>
            <a href="/${escHtml(a.path)}" target="_blank" rel="noopener">${escHtml(a.filename)}</a>
        </div>`).join('');
}

function _renderTaskComments(comments) {
    const el = document.getElementById('task-detail-comments');
    if (!comments.length) {
        el.innerHTML = '<div style="font-size:0.82rem;color:var(--text-muted);">No comments yet.</div>';
        return;
    }
    el.innerHTML = comments.map(c => `
        <div style="font-size:0.85rem;background:var(--panel-inset);border-radius:4px;padding:0.5rem 0.7rem;">
            <div style="font-weight:600;">${escHtml(c.author || 'Anonymous')}</div>
            <div>${escHtml(c.text)}</div>
        </div>`).join('');
}

async function saveTaskDetail() {
    if (!_taskDetailId) return;
    const errorEl = document.getElementById('task-detail-error');
    const payload = {
        title: document.getElementById('td-title').value.trim() || null,
        description: document.getElementById('td-description').value.trim() || null,
        owner: document.getElementById('td-owner').value.trim() || null,
        project_id: document.getElementById('td-project').value.trim() || null,
        due_date: document.getElementById('td-due').value || null,
        reminder_date: document.getElementById('td-reminder').value || null,
        priority: document.getElementById('td-priority').value,
        status: document.getElementById('td-status').value,
        tag: document.getElementById('td-tag').value.trim() || null,
        institution: document.getElementById('td-institution').value.trim() || null,
        progress_note: document.getElementById('td-notes').value.trim() || null,
    };
    try {
        const resp = await fetch('/api/tasks/' + encodeURIComponent(_taskDetailId), {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        closeTaskDetail();
        fetchBriefing();
    } catch (e) {
        errorEl.textContent = e.message;
    }
}

async function duplicateCurrentTaskDetail() {
    if (!_taskDetailId) return;
    try {
        const resp = await fetch(`/api/tasks/${encodeURIComponent(_taskDetailId)}/duplicate`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        closeTaskDetail();
        fetchBriefing();
    } catch (e) {
        document.getElementById('task-detail-error').textContent = e.message;
    }
}

async function deleteCurrentTaskDetail() {
    if (!_taskDetailId) return;
    const confirmed = await showConfirmModal({
        title: 'Delete this task?',
        body: 'This soft-deletes the task -- it stays in the record for history/audit, but disappears from your task list.',
        confirmLabel: 'Delete',
        danger: true,
    });
    if (!confirmed) return;
    try {
        const resp = await fetch('/api/tasks/' + encodeURIComponent(_taskDetailId), { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        closeTaskDetail();
        fetchBriefing();
    } catch (e) {
        document.getElementById('task-detail-error').textContent = 'Could not delete task: ' + e.message;
    }
}

async function uploadTaskAttachment() {
    if (!_taskDetailId) return;
    const input = document.getElementById('td-attachment-input');
    const file = input.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    try {
        const resp = await fetch(`/api/tasks/${encodeURIComponent(_taskDetailId)}/attachments`, {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        _renderTaskAttachments(data.attachments);
    } catch (e) {
        document.getElementById('task-detail-error').textContent = 'Could not add attachment: ' + e.message;
    } finally {
        input.value = '';
    }
}

async function addTaskComment() {
    if (!_taskDetailId) return;
    const input = document.getElementById('td-new-comment');
    const text = input.value.trim();
    if (!text) return;
    try {
        const resp = await fetch(`/api/tasks/${encodeURIComponent(_taskDetailId)}/comments`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ text }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        _renderTaskComments(data.comments);
        input.value = '';
    } catch (e) {
        document.getElementById('task-detail-error').textContent = 'Could not add comment: ' + e.message;
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


async function exportCurrentSessionToVault() {
    const statusEl = document.getElementById('modal-export-status');
    const sessionId = window._currentDetailSession;
    if (!sessionId) return;
    statusEl.textContent = 'Exporting…';
    try {
        const resp = await fetch('/api/export/vault', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session_id: sessionId }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        statusEl.textContent = 'Exported to ' + data.paths[0];
    } catch (e) {
        statusEl.textContent = 'Export failed: ' + e.message;
    }
}

async function exportCurrentSessionToDocx() {
    // Unlike the vault export above (which writes into a configured folder
    // and reports the path back as JSON), this endpoint streams the .docx
    // file itself -- download it as a blob and trigger the browser's normal
    // save-file flow, rather than parsing a JSON response.
    const statusEl = document.getElementById('modal-export-status');
    const sessionId = window._currentDetailSession;
    if (!sessionId) return;
    statusEl.textContent = 'Exporting…';
    try {
        const resp = await fetch('/api/export/docx', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session_id: sessionId }),
        });
        if (!resp.ok) {
            let message = `HTTP ${resp.status}`;
            try {
                const data = await resp.json();
                message = data.error || message;
            } catch (e) { /* non-JSON error body -- keep the HTTP status message */ }
            throw new Error(message);
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${sessionId}.docx`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        statusEl.textContent = 'Downloaded ' + sessionId + '.docx';
    } catch (e) {
        statusEl.textContent = 'Export failed: ' + e.message;
    }
}
