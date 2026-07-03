# Research: systematic todo management with linked context (2026-07)

Requested by the user: "any better way to maintain todo list in a systematic
way with proper context linked to them — deep search on current tools and
issues people are facing."

## 1. What people are actually struggling with

**Follow-through, not capture.** Studies cited across the meeting-tools
space: ~44% of meeting action items are never completed; ~71% of meetings
fail their objectives due to poor follow-through; workers forget 50% of
meeting content within an hour. The 2026 AI-notetaker market reviews land on
one sentence that matters most here: *"Most tools are good at transcription,
and almost none are good at what happens after transcription. Action items
get captured but don't move anywhere."*

**Context fragmentation.** Knowledge workers toggle apps ~1,200 times/day;
~60% of time goes to coordination (finding context, chasing status) rather
than the work itself. The task lives in one app, the meeting it came from in
another, the document that explains it in a third. A bare "negotiate SLA"
line with no path back to *why* is the canonical failure.

**Notetaker-specific complaints (2026):** bots joining calls creep
participants out (an Otter lawsuit over non-consenting participants made
teams bot-averse — device-audio capture like Granola's, and like this
project's loopback approach, is the trend); Fireflies spams summaries to
external participants; Granola had a shared-links-public-by-default trust
incident. Privacy/local-first is now a differentiator, not a niche.

**What the PKM world converged on** (org-mode/GTD, Obsidian/Logseq
communities): tasks and knowledge should live in one linked graph — a task
carries a backlink to the note/meeting that spawned it; a weekly review
ritual (GTD) keeps the list honest; plain text wins for longevity.

## 2. Where meeting-agent already stands vs. this landscape

Genuinely ahead of the market on the hard part:

| Market pain | meeting-agent today |
|---|---|
| Action items don't move anywhere | review→apply pipeline lands them in todo.md with status tracking |
| Bot-in-call creepiness / consent | device-side loopback capture, no bot, nothing leaves the machine |
| Summaries in an app nobody reopens | morning `briefing` + dashboard; local Markdown |
| No follow-through check | IS-call loop closure (did last session's targets get addressed?) + recurring-blocker escalation — rare even in paid tools |
| Vendor lock-in / privacy | plain-text todo.md, zero egress, git history |

The provenance link half-exists: every applied item carries `session_id` in
its meta — but it is **write-only context**: nothing in the UI or CLI lets
you *follow* it back to the meeting, and the item stores no evidence of the
moment it was extracted from.

## 3. Recommendations (concrete, in value order)

1. **Make `session_id` navigable (the single biggest win, tiny effort).**
   Task cards link to the source meeting's detail modal (transcript, MoM,
   summary); the briefing prints the session id it already stores.
   → Implemented in this pass (Tasks UI click-through).
2. **Store extraction evidence per action item.** Extend the extraction
   prompt to return a short verbatim `evidence` quote per item; keep it in
   the meta JSON and show it as "why this task exists" in the task detail.
   One prompt-field + one meta field; degrades gracefully when absent.
   → Implemented in this pass (prompt + meta + UI tooltip).
3. **Weekly review ritual = digest trends.** GTD's weekly review is the
   habit that fixes the 44%-never-done statistic; the digest now keeps
   per-week history and shows week-over-week open/completed deltas.
   → Implemented in this pass (digest extensions).
4. **Obsidian export** for users whose knowledge graph lives in a vault —
   tasks/MoMs join the backlink graph there. → Implemented in this pass.
5. **Not recommended:** syncing to an external task app (Todoist etc.) —
   re-introduces egress and the two-sources-of-truth problem the research
   shows people hate; and building a full projects/kanban layer — todo.md's
   flatness plus tags/status is the right weight for a single user.

## Sources

- [SpeakWise: Context Switching Statistics 2026](https://speakwiseapp.com/blog/context-switching-statistics)
- [Reclaim: Context Switching Guide 2026](https://reclaim.ai/blog/context-switching)
- [Atlassian: Context Switching](https://www.atlassian.com/work-management/project-management/context-switching)
- [Fellow: How to Manage Meeting Action Items (2026)](https://fellow.ai/blog/how-to-manage-meeting-tasks-and-action-items/)
- [Fellow: How to Track Action Items](https://fellow.ai/blog/how-to-track-action-items-steps-to-ensure-follow-through/)
- [ActionLog: Track Meeting Action Items](https://www.actionlog.app/blog/how-to-track-meeting-action-items)
- [Luminix: Granola vs Otter vs Fireflies vs Fathom 2026](https://www.useluminix.com/reports/industry-analysis/ai-meeting-notes-comparison-granola-vs-otter-vs-fireflies-vs-fathom-2026)
- [alfred_: Best AI Meeting Notetakers 2026 (bot-free)](https://get-alfred.ai/blog/best-ai-meeting-notetakers)
- [tooldirectory.ai: Otter vs Granola vs Fireflies 2026](https://tooldirectory.ai/blog/ai-notetakers-2026-otter-fireflies-granola-fathom-read)
- [GTD Forums: Combining knowledge and task management](https://forum.gettingthingsdone.com/threads/combining-knowledge-management-and-task-management.19401/)
- [Jethro Kuan: Org-mode Workflow](https://blog.jethro.dev/posts/org_mode_workflow_preview/)
- [Worg: Org for GTD](https://orgmode.org/worg/org-gtd-etc.html)
- [Obsidian Stats: Todoist Sync plugin](https://www.obsidianstats.com/plugins/todoist-sync-plugin)
