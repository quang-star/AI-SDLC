from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Starter App")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def add_numbers(a: int, b: int) -> int:
    return a + b
