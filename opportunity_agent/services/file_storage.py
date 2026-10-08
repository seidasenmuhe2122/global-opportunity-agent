from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile


@contextmanager
def local_file_path(stored_file):
    try:
        path = stored_file.path
    except NotImplementedError:
        descriptor, path = tempfile.mkstemp(suffix=Path(stored_file.name).suffix)
        try:
            with os.fdopen(descriptor, 'wb') as destination:
                with stored_file.open('rb') as source:
                    shutil.copyfileobj(source, destination)
            yield path
        finally:
            if os.path.exists(path):
                os.unlink(path)
    else:
        yield path
