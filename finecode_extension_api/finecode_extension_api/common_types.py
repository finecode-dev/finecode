import dataclasses


@dataclasses.dataclass
class Position:
    line: int
    character: int


@dataclasses.dataclass
class Range:
    start: Position
    end: Position


@dataclasses.dataclass
class TextDocumentIdentifier:
    uri: str


@dataclasses.dataclass
class TextDocumentItem:
    uri: str
    language_id: str
    version: int
    text: str
