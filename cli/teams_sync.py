import datetime
import json
import logging
from pathlib import Path

from concurrency.atomic import atomic_write_text

logger = logging.getLogger(__name__)

def fetch_outlook_calendar(output_path: Path) -> int:
    """Fetch today's meetings from local Outlook Desktop and save to JSON.

    Bug fix: this is now called via `asyncio.to_thread` from the dashboard
    (cli/web.py's sync_calendar_endpoint), which runs it on a plain
    concurrent.futures.ThreadPoolExecutor worker thread. COM requires
    CoInitialize()/CoInitializeEx() on any thread before it can use
    win32com.client.Dispatch(...) -- a never-initialized thread makes
    Dispatch raise pywintypes.com_error ("CoInitialize has not been
    called"), which the broad `except Exception` below would silently catch
    and report as "synced, 0 events" instead of a real failure. Explicitly
    initialize COM for this thread and uninitialize it when done (safe to
    call from the CLI's main thread too, which already may or may not have
    COM initialized -- CoInitialize is idempotent-safe per MSDN, returning
    S_FALSE rather than raising on a second call from the same thread)."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        logger.error("pywin32 is not installed or not supported on this platform.")
        return 0

    pythoncom.CoInitialize()
    try:
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            namespace = outlook.GetNamespace("MAPI")
            calendar = namespace.GetDefaultFolder(9)  # 9 is olFolderCalendar
        except Exception as e:
            logger.error("Failed to connect to local Outlook instance: %s", e)
            return 0

        # Date range for today
        today = datetime.date.today()

        # Format dates for Outlook Restrict method
        start_str = (today - datetime.timedelta(days=1)).strftime("%m/%d/%Y 00:00")
        end_str = (today + datetime.timedelta(days=7)).strftime("%m/%d/%Y 00:00")

        restriction = f"[Start] >= '{start_str}' AND [Start] < '{end_str}'"
        items = calendar.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        restricted_items = items.Restrict(restriction)

        events = []
        try:
            for item in restricted_items:
                if item.AllDayEvent:
                    continue

                try:
                    start_dt = item.Start.strftime("%H:%M")
                    end_dt = item.End.strftime("%H:%M")

                    # Extract and truncate body
                    body_text = ""
                    if hasattr(item, 'Body') and item.Body:
                        body_text = str(item.Body)[:1000]

                    # Extract participants
                    participants = []
                    try:
                        if hasattr(item, 'Recipients'):
                            for r in item.Recipients:
                                participants.append(r.Name)
                    except Exception:
                        pass

                    events.append({
                        "subject": item.Subject,
                        "date": item.Start.strftime("%Y-%m-%d"),
                        "start": start_dt,
                        "end": end_dt,
                        "organizer": item.Organizer if hasattr(item, 'Organizer') else "Unknown",
                        "location": item.Location if hasattr(item, 'Location') else "",
                        "body": body_text,
                        "participants": participants
                    })
                except Exception as e:
                    logger.debug("Error parsing item %s: %s", getattr(item, 'Subject', 'Unknown'), e)
        except Exception as e:
            logger.error("Error iterating over Outlook items: %s", e)
            return 0

        # atomic_write_text (tmp+fsync+os.replace) rather than a plain open/write:
        # a crash mid-write here previously risked leaving calendar.json truncated
        # or empty, which build_daily_briefing (cli/briefing.py) reads on every
        # dashboard poll -- self-healing on the next sync either way, but no
        # reason to leave a truncated-JSON window open when every other sidecar
        # writer in this project already closed it.
        atomic_write_text(output_path, json.dumps(events, indent=2))

        return len(events)
    finally:
        pythoncom.CoUninitialize()
