from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "services.miniprogram_gateway.app:app",
        host="0.0.0.0",
        port=8010,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
