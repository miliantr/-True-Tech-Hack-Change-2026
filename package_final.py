"""Build the final review archive from an explicit list, excluding inputs and runtime data."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from package_submission import FILES

ROOT = Path(__file__).resolve().parent
FINAL_FILES = FILES + (
    'app.py', 'storage.py', 'static/index.html', 'static/style.css', 'static/app.js',
    'Dockerfile', 'compose.yaml', '.dockerignore', '.gitignore', '.env.example', 'README.md',
    'package_submission.py', 'package_final.py', 'evaluate.py', 'tests/test_core.py', 'tests/test_http.py',
    'tests/test_submission.py', 'tests/scenarios.json', 'REVIEW.md',
)


if __name__ == '__main__':
    dist = ROOT/'dist'
    presentation = dist/'EnergyTool.pdf'
    if not presentation.is_file():
        raise SystemExit('Missing dist/EnergyTool.pdf')
    destination = dist/'EnergyTool-final.zip'
    with ZipFile(destination, 'w', ZIP_DEFLATED) as z:
        for name in FINAL_FILES:
            z.write(ROOT/name, name)
        z.write(presentation, 'EnergyTool.pdf')
    with ZipFile(destination) as z:
        assert set(z.namelist()) == {*FINAL_FILES, 'EnergyTool.pdf'} and z.testzip() is None
    print(destination)
