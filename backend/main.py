import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from backend.routers.download import router as download_router, dm


async def cleanup_task():
    while True:
        await asyncio.sleep(30)
        dm.purge_old_states(max_age=60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    dm.purge_old_states(max_age=0)
    task = asyncio.create_task(cleanup_task())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(download_router)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
