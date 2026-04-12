"""
Main FastAPI application.
Production-grade configuration with proper lifecycle management.
"""
import time
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.core.config import settings
from app.core.database import init_db, close_db
from app.core.redis import redis_client
from app.core.logging import logger
from app.api import supabase_auth, sessions, websocket, coding_events, speech, evaluations, vision

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager - handles startup and shutdown."""
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    
    # Non-fatal: app starts in degraded mode if DB/Redis are unreachable
    await init_db()
    await redis_client.connect()

    # BUG-04 FIX: Start the transactional outbox sweeper as a background task.
    # Without this the outbox table is written but never drained — agent
    # completion events are never published to Redis WebSocket subscribers.
    from app.core.outbox import run_outbox_sweeper
    outbox_task = asyncio.create_task(run_outbox_sweeper(), name="outbox_sweeper")
    logger.info("Outbox sweeper started")
    
    logger.info("Application startup complete")
    
    yield
    
    logger.info("Shutting down application...")

    # Cancel and await the outbox task for a clean shutdown
    outbox_task.cancel()
    try:
        await outbox_task
    except asyncio.CancelledError:
        logger.info("Outbox sweeper stopped cleanly")
    
    await redis_client.disconnect()
    await close_db()
    
    logger.info("Application shutdown complete")

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="नीति · AI-Powered Technical Interview Platform",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    lifespan=lifespan
)

# ── CORS: Use explicit origins when credentials=True ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(GZipMiddleware, minimum_size=1000)


# ── Global exception handler — ensures errors return JSON, not bare 500 ──
from fastapi.responses import JSONResponse
import traceback

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions and return a proper JSON response with CORS headers."""
    logger.error(f"Unhandled exception on {request.method} {request.url.path}: {exc}\n{traceback.format_exc()}")
    
    # BUG-16 FIX: Never expose internal exception messages (SQL fragments,
    # file paths, PII) to API clients in production.
    if settings.DEBUG:
        detail = f"Internal server error: {str(exc)}"
    else:
        detail = "An unexpected error occurred. Please try again."

    response = JSONResponse(
        status_code=500,
        content={"detail": detail},
    )
    
    # Manually add CORS headers — middleware may not run for some exception types
    origin = request.headers.get("origin")
    if origin in settings.cors_origins_list or "*" in settings.cors_origins_list:
        response.headers["Access-Control-Allow-Origin"] = origin or "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        
    return response


# ── FIX #4: Rate limiting middleware using Redis ──
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """
    Sliding-window rate limiter using a Redis sorted set.
    Limits per IP to RATE_LIMIT_PER_MINUTE requests/minute.
    Skips rate limiting if Redis is unavailable.

    BUG-13 FIX: the previous fixed-window counter allowed 2× burst at the
    window boundary (N requests at :59 + N at :00 with no penalty).
    A sliding window using a Redis sorted set is immune to this.
    """
    # Skip rate limiting for health/root endpoints and WebSocket upgrades
    if request.url.path in ("/", "/health", "/api/info") or request.url.path.startswith("/ws"):
        return await call_next(request)

    try:
        if redis_client.client:
            client_ip = request.client.host if request.client else "unknown"
            now_ms = int(time.time() * 1000)
            window_ms = 60_000
            key = f"rlsw:{client_ip}"
            limit = settings.RATE_LIMIT_PER_MINUTE

            # Atomic sliding window: remove expired entries, add current,
            # count, set expiry — all in a single pipeline round-trip.
            pipe = redis_client.client.pipeline(transaction=False)
            pipe.zremrangebyscore(key, 0, now_ms - window_ms)
            pipe.zadd(key, {str(now_ms): now_ms})
            pipe.zcard(key)
            pipe.expire(key, 60)
            results = await pipe.execute()
            current = results[2]  # zcard result

            if current > limit:
                logger.warning(f"Rate limit exceeded for {client_ip}: {current}/{limit}")
                return Response(
                    content='{"detail": "Too many requests. Please slow down."}',
                    status_code=429,
                    media_type="application/json",
                    headers={"Retry-After": "60"},
                )
    except Exception:
        # Redis unavailable — skip rate limiting gracefully
        pass

    return await call_next(request)


# ── Request logging middleware ──
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Log request timing in development/staging."""
    start = time.time()
    response = await call_next(request)
    duration_ms = (time.time() - start) * 1000

    if settings.DEBUG or settings.ENVIRONMENT != "production":
        logger.info(
            f"{request.method} {request.url.path} → {response.status_code} ({duration_ms:.1f}ms)"
        )

    return response


app.include_router(supabase_auth.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(websocket.router, prefix="/api")
app.include_router(coding_events.router, prefix="/api")
app.include_router(speech.router, prefix="/api")
app.include_router(vision.router, prefix="/api")
app.include_router(evaluations.router, prefix="/api")

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "operational"
    }

@app.get("/health")
async def health_check():
    """Health check endpoint with real service connectivity checks."""
    db_status = "connected"
    redis_status = "connected"
    
    try:
        # A6 FIX: use AsyncSessionLocal directly — iterating get_db() with
        # async for / break does not reliably trigger the generator's finally
        # cleanup, potentially leaking the session back into the pool.
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        db_status = "disconnected"
    
    try:
        if redis_client.client:
            await redis_client.client.ping()
        else:
            redis_status = "disconnected"
    except Exception:
        redis_status = "disconnected"
    
    overall = "healthy" if db_status == "connected" and redis_status == "connected" else "degraded"
    
    return {
        "status": overall,
        "environment": settings.ENVIRONMENT,
        "database": db_status,
        "redis": redis_status
    }

@app.get("/api/info")
async def api_info():
    """API information endpoint."""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "endpoints": {
            "auth": "/api/auth",
            "sessions": "/api/sessions",
            "websocket": "/api/ws",
            "docs": "/docs" if settings.DEBUG else None
        }
    }

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
        workers=settings.WORKERS if not settings.DEBUG else 1
    )
