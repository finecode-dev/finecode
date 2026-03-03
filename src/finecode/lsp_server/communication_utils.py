from enum import Enum, auto


class CommunicationType(Enum):
    TCP = auto()
    WS = auto()
    STDIO = auto()
