from __future__ import annotations

import uvicorn

from src.analytics.app import create_app
from src.shared.config import load_settings


def main() -> None:
    settings = load_settings("analytics")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
