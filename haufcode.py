#!/usr/bin/env python3
"""HaufCode — point d'entrée principal."""
import sys
import os

# Permet d'invoquer le package même sans installation
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from haufcode.__main__ import main

if __name__ == "__main__":
    main()
