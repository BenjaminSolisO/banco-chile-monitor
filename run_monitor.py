#!/usr/bin/env python
import os
from pathlib import Path

# Load .env file
env_file = Path(__file__).parent / '.env'
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()

# Run monitor
import subprocess
subprocess.run(['python', str(Path(__file__).parent / 'gmail_banco_chile_monitor.py')])
