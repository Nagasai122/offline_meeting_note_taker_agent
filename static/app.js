document.addEventListener('DOMContentLoaded', () => {
    fetchBriefing();
    setInterval(fetchBriefing, 5000); // Poll every 5s for updates
    
    document.getElementById('btn-toggle-record').addEventListener('click', toggleRecording);
    document.getElementById('btn-highlight').addEventListener('click', logHighlight);
});

let isRecording = false;

async function fetchBriefing() {
    try {
        const response = await fetch('/api/briefing');
        const data = await response.json();
        
        updateUI(data);
    } catch (e) {
        console.error("Failed to fetch briefing:", e);
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
            <div class="meeting-card" style="display: flex; justify-content: space-between; align-items: center; ${(new Date().toTimeString().slice(0,5) >= m.start && new Date().toTimeString().slice(0,5) <= m.end) ? 'border: 2px solid var(--accent); background: rgba(0, 243, 255, 0.1);' : ''}">
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
    const fullTaskList = document.getElementById('full-task-list');
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
        const tasksHTML = allTasks.map(t => {
            // Check if it's already completed in our UI state (we do optimistic updates)
            return `
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
        `}).join('');
        
        if (taskList) taskList.innerHTML = tasksHTML; // We could slice this to top 3 if we wanted
        if (fullTaskList) fullTaskList.innerHTML = tasksHTML;
        if (fullTaskCount) fullTaskCount.innerText = allTasks.length;
    } else {
        const emptyState = '<p style="color: var(--text-muted); text-align: center; margin-top: 2rem;">All caught up! No pending tasks.</p>';
        if (taskList) taskList.innerHTML = emptyState;
        if (fullTaskList) fullTaskList.innerHTML = emptyState;
        if (fullTaskCount) fullTaskCount.innerText = "0";
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

    // Update processing state
    const banner = document.getElementById('processing-banner');
    const errBanner = document.getElementById('error-banner');
    
    if (data.processing) {
        banner.classList.remove('hidden');
    } else {
        banner.classList.add('hidden');
    }
    
    if (data.error) {
        errBanner.classList.remove('hidden');
        document.getElementById('error-text').innerText = data.error;
    } else {
        errBanner.classList.add('hidden');
    }
    
    // Sync recording state
    isRecording = data.recording;
    updateRecordingBtn();
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

async function toggleRecording() {
    const btn = document.getElementById('btn-toggle-record');
    btn.disabled = true;

    try {
        if (!isRecording) {
            // Start
            let title = "Ad-hoc Meeting";
            const context = document.getElementById('meeting-context').value;

            // If the context doesn't start with "Meeting:", it's an ad-hoc meeting and we should ask for a title
            if (!context.startsWith("Meeting:")) {
                const userTitle = prompt("Enter a title for this ad-hoc meeting:", "Quick Sync");
                if (userTitle === null) {
                    // User cancelled
                    return;
                }
                title = userTitle || "Ad-hoc Meeting";
            } else {
                // It's from the calendar, extract the title from the context line "Meeting: [Title]"
                const lines = context.split('\n');
                const titleLine = lines.find(l => l.startsWith('Meeting: '));
                if (titleLine) {
                    title = titleLine.replace('Meeting: ', '').trim();
                }
            }

            const response = await fetch('/api/record/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ context: context, title: title })
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
        badge.style.color = '#f59e0b';
    } else if (loopbackAvailable === true) {
        badge.textContent = 'Mic + System Audio';
        badge.style.color = '#10b981';
    } else {
        badge.textContent = '';
    }
}

async function logHighlight() {
    const btn = document.getElementById('btn-highlight');
    btn.disabled = true;
    const oldHtml = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-check"></i> Highlighted';

    try {
        const response = await fetch('/api/record/highlight', { method: 'POST' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch (e) {
        showFetchError(`Could not log highlight: ${e.message}`);
    } finally {
        setTimeout(() => {
            btn.innerHTML = oldHtml;
            btn.disabled = false;
        }, 2000);
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

function updateRecordingBtn() {
    const btn = document.getElementById('btn-toggle-record');
    const status = document.getElementById('recording-status');
    const statusText = document.getElementById('recording-status-text');
    const preMeetingControls = document.getElementById('pre-meeting-controls');
    const btnHighlight = document.getElementById('btn-highlight');
    const liveTranscriptPanel = document.getElementById('live-transcript-panel');
    const liveTranscriptText = document.getElementById('live-transcript-text');
    
    if (isRecording) {
        btn.innerText = "Stop Meeting";
        btn.classList.add('recording');
        status.className = 'recording-active';
        statusText.innerText = 'Recording live...';
        preMeetingControls.classList.add('hidden');
        btnHighlight.classList.remove('hidden');
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
        btn.innerText = "Start Meeting";
        btn.classList.remove('recording');
        status.className = 'recording-idle';
        statusText.innerText = 'Ready to Record';
        preMeetingControls.classList.remove('hidden');
        btnHighlight.classList.add('hidden');
        document.getElementById('meeting-context').value = ""; // clear context
        if (liveTranscriptPanel) liveTranscriptPanel.classList.add('hidden');
        
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
    const tabs = ['dashboard', 'calendar', 'meetings', 'tasks', 'review', 'system'];
    tabs.forEach(t => {
        document.getElementById('view-' + t).classList.add('hidden');
        document.getElementById('nav-' + t).classList.remove('active');
        document.getElementById('nav-' + t).setAttribute('aria-selected', 'false');
    });
    document.getElementById('view-' + tabId).classList.remove('hidden');
    document.getElementById('nav-' + tabId).classList.add('active');
    document.getElementById('nav-' + tabId).setAttribute('aria-selected', 'true');

    clearInterval(_statusPollTimer);
    if (tabId === 'review') loadReviewQueue();
    if (tabId === 'system') {
        loadSystemStatus();
        _statusPollTimer = setInterval(loadSystemStatus, 5000);
    }
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

function renderReviewAwaiting(sessions) {
    const container = document.getElementById('review-awaiting-list');
    if (!container) return;
    if (!sessions.length) {
        container.innerHTML = '<p style="color:var(--text-muted);">No sessions awaiting review.</p>';
        return;
    }
    container.innerHTML = sessions.map(s => `
        <div class="glass-panel" style="margin-bottom:1.5rem;padding:1.25rem;" id="session-block-${s.session_id}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
                <strong style="color:var(--primary);">Session: ${escHtml(s.session_id)}</strong>
                <button class="btn-primary" onclick="submitReview('${escHtml(s.session_id)}')"
                        style="padding:6px 16px;font-size:0.85rem;">
                    <i class="fa-solid fa-paper-plane"></i> Submit Review
                </button>
            </div>
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
                                   style="background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.15);
                                          border-radius:4px;color:white;padding:2px 6px;width:120px;font-size:0.8rem;">
                        </label>
                        <label style="font-size:0.8rem;color:var(--text-muted);">Due:
                            <input type="date" value="${safeDue}"
                                   id="due-${safeId}"
                                   style="background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.15);
                                          border-radius:4px;color:white;padding:2px 6px;font-size:0.8rem;">
                        </label>
                    </div>
                </div>
                <div style="display:flex;gap:0.5rem;align-items:center;flex-shrink:0;">
                    <label style="font-size:0.8rem;cursor:pointer;">
                        <input type="radio" name="dec-${safeId}" value="accept" checked> Accept
                    </label>
                    <label style="font-size:0.8rem;cursor:pointer;color:#f87171;">
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
        block.innerHTML = `<div style="color:#10b981;padding:0.5rem;">
            ✓ Review submitted for <strong>${escHtml(sessionId)}</strong> —
            ${data.accepted_count} accepted, ${data.rejected_count} rejected.
            Reload to apply.
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
                    style="padding:6px 16px;font-size:0.85rem;background:linear-gradient(135deg,#10b981,#059669);">
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
        let html = `<div style="color:#10b981;padding:0.5rem;">
            ✓ Applied <strong>${data.applied_count}</strong> item(s) from
            <strong>${escHtml(sessionId)}</strong> to todo.md.`;
        if (data.conflicts && data.conflicts.length > 0) {
            // Amendment 3: show both versions of each conflict
            html += `<div style="margin-top:0.5rem;color:#f87171;">
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
        resultsEl.innerHTML = `<p style="color:#f87171;">Search error: ${escHtml(e.message)}</p>`;
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
    ['modal-summary','modal-actions','modal-highlights','modal-transcript'].forEach(id => {
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

        if (d.summary) {
            document.getElementById('modal-summary-body').textContent = d.summary;
            document.getElementById('modal-summary').style.display = '';
        }

        if (d.actions && d.actions.length) {
            const ul = document.getElementById('modal-actions-list');
            ul.innerHTML = d.actions.map(a => `
                <li style="padding:0.4rem 0;border-bottom:1px solid rgba(255,255,255,0.07);">
                    <strong>${escHtml(a.description || a.action || '')}</strong>
                    ${a.owner ? `<span style="color:var(--text-muted);font-size:0.85rem;"> · ${escHtml(a.owner)}</span>` : ''}
                    ${a.due_date ? `<span style="color:var(--text-muted);font-size:0.85rem;"> · due ${escHtml(a.due_date)}</span>` : ''}
                </li>`).join('');
            document.getElementById('modal-actions').style.display = '';
        }

        if (d.highlights && d.highlights.length) {
            const ul = document.getElementById('modal-highlights-list');
            ul.innerHTML = d.highlights.map(h => `
                <li style="padding:0.35rem 0;border-bottom:1px solid rgba(255,255,255,0.07);
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
                llmEl.style.color = '#10b981';
            } else {
                llmEl.textContent = 'Stopped';
                llmEl.style.color = '#6b7280';
            }
        }

        const sessEl = el('stat-session');
        if (sessEl) {
            if (d.processing) {
                sessEl.textContent = 'Processing…';
                sessEl.style.color = '#f59e0b';
            } else if (d.recording) {
                sessEl.textContent = 'Recording';
                sessEl.style.color = '#ef4444';
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
        await loadDashboardData();
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    }
}
