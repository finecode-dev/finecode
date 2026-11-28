try:
    from .simple import SimpleAction as TryAction
except ImportError:
    from .base import BaseAction as TryAction
