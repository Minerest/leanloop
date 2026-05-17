import sys
from pathlib import Path

# Make the top-level leanloop module importable when pytest is run from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
