import enum


class Color(enum.Enum):
    BLACK = "black"
    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    BLUE = "blue"
    MAGENTA = "magenta"
    CYAN = "cyan"
    WHITE = "white"
    BRIGHT_BLACK = "bright_black"
    BRIGHT_RED = "bright_red"
    BRIGHT_GREEN = "bright_green"
    BRIGHT_YELLOW = "bright_yellow"
    BRIGHT_BLUE = "bright_blue"
    BRIGHT_MAGENTA = "bright_magenta"
    BRIGHT_CYAN = "bright_cyan"
    BRIGHT_WHITE = "bright_white"


class StyledText:
    # currently we use builder for styled string
    # from python 3.14 we could use template string from PEP 750
    def __init__(self) -> None:
        self.text_parts: list[str | dict[str, str | bool]] = []

    def append(self, text: str) -> None:
        self.text_parts.append(text)

    def append_styled(
        self,
        text: str,
        foreground: Color | None = None,
        background: Color | None = None,
        bold: bool | None = None,
        underline: bool | None = None,
        overline: bool | None = None,
        italic: bool | None = None,
        blink: bool | None = None,
        strikethrough: bool | None = None,
        reset: bool = True,
    ) -> None:
        # parameters inspired by
        # https://click.palletsprojects.com/en/stable/api/#click.style
        params = [
            # name, default_value, provided_value
            ("foreground", None, foreground),
            ("background", None, background),
            ("bold", None, bold),
            ("underline", None, underline),
            ("overline", None, overline),
            ("italic", None, italic),
            ("blink", None, blink),
            ("strikethrough", None, strikethrough),
            ("reset", True, reset),
        ]

        changed_params: dict[str, str | bool] = {}
        for param_name, param_default, param_value in params:
            if param_value != param_default:
                changed_params[param_name] = (
                    param_value
                    if not isinstance(param_value, enum.Enum)
                    else param_value.value
                )

        self.text_parts.append({"text": text, **changed_params})

    def to_json(self) -> dict[str, list[str | dict[str, str | bool]]]:
        return {"parts": self.text_parts}
