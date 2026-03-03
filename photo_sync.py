#!/usr/bin/env python3
"""Backward-compatible shim — delegates to frame.sync.main()."""
from frame.sync import main

if __name__ == '__main__':
    main()
