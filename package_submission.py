"""Create the allowed root-only competition archive; never include source data."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

FILES = ("solution.py", "config.py", "api_client.py", "retrieval.py", "consultant.py", "requirements.txt")


def package(destination):
    root = Path(__file__).resolve().parent
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(root/name, name)
    with ZipFile(destination) as archive:
        assert set(archive.namelist()) == set(FILES)
        assert archive.testzip() is None
    assert destination.stat().st_size < 128*1024*1024
    return destination


if __name__ == "__main__":
    print(package(Path(__file__).resolve().parent/"dist"/"submission.zip"))
