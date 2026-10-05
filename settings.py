"""Where the prepared data lives: data/ next to the code, or DATA_DIR (e.g. a Cloud Storage bucket
mounted into the Cloud Run service). Built by scripts/prep_data.py."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
