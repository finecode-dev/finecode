class ActionRunFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class StartingEnvironmentsFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
