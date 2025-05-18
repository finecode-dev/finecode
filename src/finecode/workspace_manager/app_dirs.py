from platformdirs import PlatformDirs


def get_app_dirs():
    # ensure best practice: use versioned path
    return PlatformDirs(
        appname="FineCode_Workspace_Manager", appauthor="FineCode", version="1.0"
    )
