$ErrorActionPreference = "Stop"

python -m compileall -q .
python -m unittest discover -s tests -v
