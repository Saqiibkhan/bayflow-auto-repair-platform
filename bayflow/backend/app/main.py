import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import r as api_router
from .config import AUTO_CREATE_TABLES
from .db import Base, engine

app = FastAPI(title="BayFlow API", version="1.0.0")
app.include_router(api_router)

if AUTO_CREATE_TABLES:
    Base.metadata.create_all(engine)

STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
