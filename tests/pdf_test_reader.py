"""PDF assertions use the declared test dependency, optionally installed in tests/.pdf-deps."""
try:
    from pypdf import PdfReader
except ModuleNotFoundError:
    import sys
    from pathlib import Path
    local = Path(__file__).with_name('.pdf-deps')
    if not (local / 'pypdf').is_dir():
        raise
    sys.path.insert(0, str(local))
    from pypdf import PdfReader
