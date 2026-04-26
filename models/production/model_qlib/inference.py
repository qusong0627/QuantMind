#!/usr/bin/env python3
"""
Compatibility shim for the checked-in parquet inference entrypoint.
"""

from backend.services.engine.inference.templates.inference_parquet import main


if __name__ == "__main__":
    main()
