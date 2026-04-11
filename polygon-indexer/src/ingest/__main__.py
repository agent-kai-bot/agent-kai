from __future__ import annotations

import asyncio

from src.ingest.service import run_service


def main() -> None:
    asyncio.run(run_service())


if __name__ == "__main__":
    main()
