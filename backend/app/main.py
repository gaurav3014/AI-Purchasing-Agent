import logging
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.db import Base, engine
from app.api.routes import router

# Makes the "agent" and "scheduler" loggers (api/orchestrator.py,
# scheduler.py) actually print -- without this, logger.info() calls are
# silently dropped since the root logger defaults to WARNING. This is what
# lets you trace what the agent gathered/decided/did in the server console,
# in addition to querying decision_log via the API.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

app = FastAPI(title="AI Purchasing Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
def root():
    # This backend only serves the frontend's API calls (see the React app in
    # frontend/); it has no page of its own. If you hit http://localhost:8000
    # directly in a browser you'll only ever get this, or a 404 on any path
    # that isn't under /api/... -- the actual UI is at http://localhost:5173.
    return {
        "service": "AI Purchasing Agent API",
        "docs": "/docs",
        "health": "/api/health",
        "note": "The UI is a separate app -- run the frontend (npm run dev) and open it, usually on http://localhost:5173.",
    }


@app.on_event("startup")
def on_startup():
    # Auto-seed on first run only (never wipes an existing DB with decision history).
    db_path = "purchasing_agent.db"
    if not os.path.exists(db_path):
        from app.seed import seed
        seed()
    else:
        Base.metadata.create_all(bind=engine)

    from app import scheduler
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    from app import scheduler
    scheduler.stop()
