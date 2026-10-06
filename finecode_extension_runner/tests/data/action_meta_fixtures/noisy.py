import os

from finecode_extension_api.code_action import Action

print("noisy stdout at import")
os.write(1, b"noisy fd1 at import\n")


class NoisyAction(Action):
    LANGUAGE = "python"
