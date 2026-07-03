# UI Test Plan — Manual & Browser-Based
**Run after:** claude_cli_bugfix_01.md and claude_cli_bugfix_02.md are applied  
**Prerequisites:** `meeting-agent serve` running at http://localhost:8765  
**Browser:** Chrome with DevTools open (F12 → Console tab visible throughout)

Zero red errors in Console is a PASS condition for every test below.

---

## T-01 — Page Load & Navigation

1. Open http://localhost:8765
2. Confirm: page loads without a white screen or 500 error
3. Confirm: sidebar shows all nav items:
   Dashboard | IS Call Hub | Project Meetings | Seminars | Calendar | Past Meetings | Tasks | Needs Review | Settings | System
4. Click each nav item in sequence
5. PASS: each panel renders without console errors, no blank white panels
6. FAIL indicators: red console error, blank panel, JS exception alert

---

## T-02 — Meeting Type Dropdown

1. Click "Start Meeting" or the recording controls area
2. Confirm: meeting type dropdown is visible with 4 options:
   General / Other (default) | Project Meeting | IS Call (ad-hoc) | Seminar
3. Select each option in turn
4. PASS: dropdown selection registers without errors
5. For "General / Other": clicking Start Meeting opens the pre-meeting context modal.
   Confirm: document upload and agenda fields are present but optional.
6. For "Project Meeting": clicking Start Meeting opens the pre-meeting context modal.
   The modal's Start Recording button is disabled until a title is typed (title is the
   only required field — document upload is entirely optional).
7. For "IS Call (ad-hoc)": clicking Start Meeting bypasses the modal entirely and
   starts recording directly (same as the IS Call Hub one-tap button — no title prompt).
8. For the IS Call Hub: the "Start IS Call" button routes directly without requiring
   the dropdown selection (these are independent entry points to the same flow).

---

## T-03 — Start Recording Without Context (Project Meeting)

1. Select "Project Meeting" from the type dropdown
2. Click "Start Meeting" — the pre-meeting context modal opens
3. Type "Weekly team sync" in the Meeting Title field
4. Confirm: the modal's "Start Recording" button becomes enabled after typing the title
5. Click Start Recording once (the modal's button, not the header button)
6. PASS: modal closes, recording starts (indicator appears, elapsed timer begins at 0)
7. Wait 5 seconds, then click "Stop Meeting" in the header
8. PASS: pipeline begins (processing banner appears with "Transcribing…")

FAIL conditions:
- Modal's Start Recording button never enables after typing title
- Multiple rapid clicks on the modal's Start Recording button produce duplicate POSTs
  (check Network tab — the button is disabled during the request, so this should not occur)
- Console shows "Cannot read properties of undefined"

---

## T-04 — Double-Click Protection on Start Recording

1. Select any meeting type, type a description
2. Double-click the Start Recording button rapidly
3. PASS: only one recording starts (check Network tab — only one POST /api/record/start)
4. PASS: second click returns a visible "already recording" message or is silently ignored
5. FAIL: two recording processes start (visible as two session IDs in briefing response)

---

## T-05 — IS Call Hub One-Tap Start

1. Click "IS Call Hub" in the sidebar
2. Confirm: "Start IS Call" button is prominent and clearly labelled
3. Click it once
4. PASS: recording starts immediately with session_id beginning "is-call-"
5. No description field should be required
6. Confirm: elapsed timer starts correctly
7. Stop recording after 5 seconds

---

## T-06 — Browser Refresh Mid-Recording (Fix F verification)

1. Start a General recording
2. Wait 30 seconds (note the elapsed time shown)
3. Press F5 to refresh the browser
4. PASS: after refresh, the dashboard shows "Recording in progress"
5. PASS: the elapsed timer resumes from approximately the correct time (not reset to 0:00)
6. FAIL: timer resets to 0:00 after refresh (Fix F not applied)
7. Stop the recording after confirming timer behaviour

---

## T-07 — Transcript Upload With Calendar Link

1. Navigate to **Settings** tab → "Import Transcript" section
2. Set the date picker to today
3. Click "Search" for calendar events
4. PASS: either a list of events appears, or "No meetings found" message appears
5. No 500 error in console
6. Create a test .txt file with content: "Meeting notes. Action: Fix the bug by Friday."
7. Upload the .txt file
8. If calendar events were found: select one before uploading
9. PASS: session appears in Past Meetings after a short delay
10. If calendar was linked: open the session detail and confirm calendar event name appears

---

## T-08 — Manual Task Entry

1. Navigate to the Tasks tab
2. Confirm: "Add Task" button or section is visible
3. Click to expand the form
4. Fill in:
   - Description: "Test task with pipe | character and <script>test</script>"
   - Due date: today
   - Priority: HIGH
   - Tag: test-tag
5. Click Save Task
6. PASS: task appears in the list
7. PASS: the description renders as literal text — `<script>` is NOT executed
   (no alert box or JS execution — XSS Fix C2 working)
8. PASS: the pipe character `|` appears as a literal `|` in the task text
9. Change status to "In Progress" via the dropdown
10. PASS: task shows "In Progress" state
11. Delete the task
12. PASS: task disappears from the active list

---

## T-09 — XSS Verification (C2 Fix)

1. Ensure a session exists in Past Meetings (from prior tests)
2. Manually inject a test action item via the API:

   Open browser console and run:
   ```javascript
   fetch('/api/tasks/manual', {
     method: 'POST',
     headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({
       description: '<img src=x onerror=alert("XSS")>',
       priority: 'LOW'
     })
   }).then(r => r.json()).then(console.log)
   ```

3. Navigate to Tasks tab and refresh
4. PASS: no alert box appears; the text `<img src=x onerror=alert("XSS")>` renders
   as literal text (HTML entities visible in the DOM)
5. FAIL: an alert box appears (XSS vulnerability still present)
6. Clean up: delete the test task via the UI

---

## T-10 — Context Upload (Optional, No Block)

1. Start a Project Meeting recording
2. While the recording is active (or use the pre-meeting modal):
   attempt to upload a small PDF or PPTX if available
3. PASS: upload succeeds and shows a confirmation message
4. PASS: if no file is uploaded, recording can still proceed and complete normally
5. FAIL: recording is blocked waiting for file upload

---

## T-11 — Needs Review Tab

1. Allow any prior recording to reach PROPOSED state (may require waiting for pipeline)
2. Navigate to "Needs Review" tab
3. PASS: session appears with its extracted action items listed
4. Accept all items
5. Click Apply
6. PASS: session moves to APPLIED state
7. PASS: items appear in the Tasks tab
8. FAIL: apply button produces a 500 or shows no feedback

---

## T-12 — Settings Panel

1. Navigate to Settings tab
2. Confirm: Whisper model selection is present (Fast/Balanced/Accurate)
3. Change the selection
4. PASS: selection persists after navigating away and back
5. Confirm: privacy notice about mail context is visible
6. No console errors

---

## T-13 — Past Meetings Sort Order

1. Navigate to Past Meetings
2. Confirm: meetings appear newest first (mtime-sort, not alphabetical)
3. Verify: the most recently completed session appears at the top
4. FAIL: meetings appear in alphabetical order or random order

---

## T-14 — Search

1. Navigate to Past Meetings or use the search box
2. Type a keyword that appears in a known past meeting (e.g. "action", "progress")
3. PASS: results appear with matching snippets
4. Type a nonsense string ("zzzzxxxxxxxxxnotaword")
5. PASS: "No results" message appears (no crash, no empty white area)

---

## Console Error Baseline

After completing all tests, the browser console must show:
- Zero red errors
- Zero uncaught TypeError or ReferenceError
- Warnings (yellow) are acceptable if they are browser-generated (e.g. deprecated API notices)

Record any remaining console errors with their exact message and line number.
These are outstanding bugs requiring a further fix pass.

---

## Reporting Format

For each test, record:
| Test | PASS/FAIL | Console errors (yes/no) | Notes |
|------|-----------|------------------------|-------|
| T-01 | | | |
| T-02 | | | |
...

Submit the completed table alongside any screenshot of console errors for investigation.
