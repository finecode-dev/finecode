from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class LoggingEventHandler(FileSystemEventHandler):
    """Logs all the events captured."""

    def __init__(self):
        super().__init__()

    def on_moved(self, event):
        super().on_moved(event)

        what = "directory" if event.is_directory else "file"
        print(f"Moved {what}: from {event.src_path} to {event.dest_path}")

    def on_created(self, event):
        super().on_created(event)

        what = "directory" if event.is_directory else "file"
        print(f"Created {what}: {event.src_path}")

    def on_deleted(self, event):
        super().on_deleted(event)

        what = "directory" if event.is_directory else "file"
        print(f"Deleted {what}: {event.src_path}")

    def on_modified(self, event):
        super().on_modified(event)

        what = "directory" if event.is_directory else "file"
        print(f"Modified {what}: {event.src_path}")


@contextmanager
def watch_workspace_dir(dir_path: Path) -> Generator[Observer, None, None]:
    # NOTE: watcher is not in all possible cases reliable, especially when there are a lot of
    # changes on Windows. Always provide possibility to refresh information manually if possible.
    observer = Observer()
    event_handler = LoggingEventHandler()
    observer.schedule(event_handler, dir_path, recursive=True)
    try:
        observer.start()
        yield observer
    finally:
        observer.stop()
        observer.join()
