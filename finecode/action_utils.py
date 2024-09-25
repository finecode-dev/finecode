import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def permanent_or_tmp_file_path(file_path: Path | None, file_content: str):
    if file_path is not None:
        yield file_path
    else:
        with tempfile.NamedTemporaryFile() as tmp_file:
            tmp_file.write(file_content.encode('utf-8'))
            yield Path(tmp_file.name)


@contextmanager
def tmp_file_copy_path(file_path: Path | None, file_content: str):
    # the same extension is important, because some tools like black check file extension as well
    with tempfile.NamedTemporaryFile(suffix=file_path.suffix if file_path is not None else None) as tmp_file:
        if file_content != '':
            tmp_file.write(file_content.encode('utf-8'))
            tmp_file.flush()
        elif file_path is not None:
            with open(file_path, 'rb') as original_file:
                tmp_file.write(original_file.read())
                tmp_file.flush()
        else:
            raise ValueError('Invalid arguments')
        
        yield Path(tmp_file.name)
