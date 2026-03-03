#!/usr/bin/env python3
"""Backward-compatible shim — delegates to frame.server.main()."""
from frame.server import main

if __name__ == '__main__':
    main()
