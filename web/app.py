"""FastAPI web dashboard for content review"""
import json
import secrets
import hmac
import hashlib
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from src.queue import QueueManager, ContentStatus
from src.utils import get_env, logger

app = FastAPI(title="Content Machine", docs_url=None, redoc_url=None)

DASHBOARD_SECRET = get_env("DASHBOARD_SECRET", "")
SESSION_COOKIE = "cm_session"
CSRF_FIELD = "csrf_token"


def _sign_session(value: str) -> str:
    return hmac.new(DASHBOARD_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def _verify_session(token: str, signature: str) -> bool:
    if not DASHBOARD_SECRET:
        return False
    expected = _sign_session(token)
    return hmac.compare_digest(expected, signature)


def _get_session_token(request: Request) -> Optional[str]:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie or ":" not in cookie:
        return None
    token, signature = cookie.rsplit(":", 1)
    if _verify_session(token, signature):
        return token
    return None


def _is_authenticated(request: Request) -> bool:
    if not DASHBOARD_SECRET:
        return True
    return _get_session_token(request) is not None


def _generate_csrf(session_token: str) -> str:
    return hmac.new(
        DASHBOARD_SECRET.encode(),
        f"csrf:{session_token}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]


def _verify_csrf(request: Request, token: str) -> bool:
    if not DASHBOARD_SECRET:
        return True
    session_token = _get_session_token(request)
    if not session_token:
        return False
    expected = _generate_csrf(session_token)
    return hmac.compare_digest(expected, token)


class _AuthRedirect(Exception):
    pass


def _require_auth(request: Request):
    if not _is_authenticated(request):
        raise _AuthRedirect()


@app.exception_handler(_AuthRedirect)
async def auth_redirect_handler(request: Request, exc: _AuthRedirect):
    return RedirectResponse(url="/login", status_code=303)


def _require_csrf(request: Request, csrf_token: str):
    if not _verify_csrf(request, csrf_token or ""):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error on {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


web_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=web_dir / "static"), name="static")
templates = Jinja2Templates(directory=web_dir / "templates")

queue = QueueManager()


def _template_context(request: Request, **kwargs) -> dict:
    ctx = {"request": request, **kwargs}
    session_token = _get_session_token(request)
    if session_token:
        ctx["csrf_token"] = _generate_csrf(session_token)
    elif not DASHBOARD_SECRET:
        ctx["csrf_token"] = ""
    return ctx


def parse_json_field(value: Optional[str]) -> list:
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if not DASHBOARD_SECRET:
        return RedirectResponse(url="/", status_code=303)
    if _is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "page": "login",
    })


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if not DASHBOARD_SECRET:
        return RedirectResponse(url="/", status_code=303)
    if not hmac.compare_digest(password, DASHBOARD_SECRET):
        logger.warning(f"Failed login attempt from {request.client.host}")
        return RedirectResponse(url="/login?error=Invalid+password", status_code=303)

    session_token = secrets.token_hex(32)
    signature = _sign_session(session_token)
    cookie_value = f"{session_token}:{signature}"

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        httponly=True,
        samesite="strict",
        max_age=86400 * 7,
    )
    logger.info(f"Login from {request.client.host}")
    return response


@app.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    _require_auth(request)
    stats = queue.get_stats()
    pending = queue.get_pending(limit=5)

    return templates.TemplateResponse("dashboard.html", _template_context(
        request, stats=stats, pending=pending, page="dashboard"
    ))


@app.get("/queue", response_class=HTMLResponse)
async def queue_list(request: Request, status: str = "pending"):
    _require_auth(request)
    valid_statuses = [s.value for s in ContentStatus]
    if status not in valid_statuses:
        status = "pending"

    with queue.get_session() as session:
        from src.queue import ContentItem
        items = session.query(ContentItem)\
            .filter_by(status=status)\
            .order_by(ContentItem.created_at.desc())\
            .limit(50).all()
        session.expunge_all()

    return templates.TemplateResponse("queue.html", _template_context(
        request, items=items, current_status=status, statuses=valid_statuses, page="queue"
    ))


@app.get("/review/{item_id}", response_class=HTMLResponse)
async def review_item(request: Request, item_id: int):
    _require_auth(request)
    item = queue.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    return templates.TemplateResponse("review.html", _template_context(
        request,
        item=item,
        pro_hashtags=parse_json_field(item.pro_hashtags),
        pro_thread=parse_json_field(item.pro_thread_parts),
        work_cashtags=parse_json_field(item.work_cashtags),
        work_thread=parse_json_field(item.work_thread_parts),
        degen_thread=parse_json_field(item.degen_thread_parts),
        page="review",
    ))


@app.post("/approve/{item_id}")
async def approve_item(request: Request, item_id: int, csrf_token: str = Form("")):
    _require_auth(request)
    _require_csrf(request, csrf_token)
    if queue.update_status(item_id, ContentStatus.APPROVED.value):
        logger.info(f"Approved item {item_id} via web")
        return RedirectResponse(url="/queue?status=pending", status_code=303)
    raise HTTPException(status_code=404, detail="Item not found")


@app.post("/reject/{item_id}")
async def reject_item(request: Request, item_id: int, csrf_token: str = Form("")):
    _require_auth(request)
    _require_csrf(request, csrf_token)
    if queue.update_status(item_id, ContentStatus.REJECTED.value):
        logger.info(f"Rejected item {item_id} via web")
        return RedirectResponse(url="/queue?status=pending", status_code=303)
    raise HTTPException(status_code=404, detail="Item not found")


@app.post("/mark-posted/{item_id}")
async def mark_posted(request: Request, item_id: int, csrf_token: str = Form("")):
    _require_auth(request)
    _require_csrf(request, csrf_token)
    if queue.update_status(item_id, ContentStatus.POSTED.value):
        logger.info(f"Marked item {item_id} as posted via web")
        return RedirectResponse(url="/queue?status=approved", status_code=303)
    raise HTTPException(status_code=404, detail="Item not found")


@app.post("/edit/{item_id}")
async def edit_item(
    request: Request,
    item_id: int,
    csrf_token: str = Form(""),
    pro_content: str = Form(None),
    work_content: str = Form(None),
    degen_content: str = Form(None),
):
    _require_auth(request)
    _require_csrf(request, csrf_token)
    updates = {}
    if pro_content is not None:
        updates["pro_content"] = pro_content
    if work_content is not None:
        updates["work_content"] = work_content
    if degen_content is not None:
        updates["degen_content"] = degen_content

    if updates and queue.update_content(item_id, updates):
        logger.info(f"Updated item {item_id} via web")
        return RedirectResponse(url=f"/review/{item_id}", status_code=303)
    raise HTTPException(status_code=404, detail="Item not found")


@app.get("/health")
async def health():
    db_ok = queue.ping()
    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
