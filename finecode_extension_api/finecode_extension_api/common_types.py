from finecode_extension_api.code_action import BaseModel


class Position(BaseModel):
    line: int
    character: int


class Range(BaseModel):
    start: Position
    end: Position


class TextDocumentIdentifier(BaseModel):
    uri: str


class TextDocumentItem(BaseModel):
    uri: str
    language_id: str
    version: int
    text: str
