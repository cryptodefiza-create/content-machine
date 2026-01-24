"""FastAPI web dashboard for content review"""
import json
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from src.queue import QueueManager, ContentStatus
from src.utils import logger

app = FastAPI(title="Content Machine", docs_url=None, redoc_url=None)

# Setup paths
web_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=web_dir / "static"), name="static")
templates = Jinja2Templates(directory=web_dir / "templates")

# Database
queue = QueueManager()


def parse_json_field(value: Optional[str]) -> list:
    """Safely parse JSON list fields"""
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard with stats"""
    stats = queue.get_stats()
    pending = queue.get_pending(limit=5)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "pending": pending,
        "page": "dashboard"
    })


@app.get("/queue", response_class=HTMLResponse)
async def queue_list(request: Request, status: str = "pending"):
    """List items by status"""
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

    return templates.TemplateResponse("queue.html", {
        "request": request,
        "items": items,
        "current_status": status,
        "statuses": valid_statuses,
        "page": "queue"
    })


@app.get("/review/{item_id}", response_class=HTMLResponse)
async def review_item(request: Request, item_id: int):
    """Review a single content item with all personas"""
    item = queue.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    # Parse JSON fields for display
    pro_hashtags = parse_json_field(item.pro_hashtags)
    pro_thread = parse_json_field(item.pro_thread_parts)
    work_cashtags = parse_json_field(item.work_cashtags)
    work_thread = parse_json_field(item.work_thread_parts)
    degen_thread = parse_json_field(item.degen_thread_parts)

    return templates.TemplateResponse("review.html", {
        "request": request,
        "item": item,
        "pro_hashtags": pro_hashtags,
        "pro_thread": pro_thread,
        "work_cashtags": work_cashtags,
        "work_thread": work_thread,
        "degen_thread": degen_thread,
        "page": "review"
    })


@app.post("/approve/{item_id}")
async def approve_item(item_id: int):
    """Approve content"""
    if queue.update_status(item_id, ContentStatus.APPROVED.value):
        logger.info(f"Approved item {item_id} via web")
        return RedirectResponse(url="/queue?status=pending", status_code=303)
    raise HTTPException(status_code=404, detail="Item not found")


@app.post("/reject/{item_id}")
async def reject_item(item_id: int):
    """Reject content"""
    if queue.update_status(item_id, ContentStatus.REJECTED.value):
        logger.info(f"Rejected item {item_id} via web")
        return RedirectResponse(url="/queue?status=pending", status_code=303)
    raise HTTPException(status_code=404, detail="Item not found")


@app.post("/mark-posted/{item_id}")
async def mark_posted(item_id: int):
    """Mark as posted"""
    if queue.update_status(item_id, ContentStatus.POSTED.value):
        logger.info(f"Marked item {item_id} as posted via web")
        return RedirectResponse(url="/queue?status=approved", status_code=303)
    raise HTTPException(status_code=404, detail="Item not found")


@app.post("/edit/{item_id}")
async def edit_item(
    item_id: int,
    pro_content: str = Form(None),
    work_content: str = Form(None),
    degen_content: str = Form(None)
):
    """Edit content fields"""
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
    """Health check endpoint"""
    db_ok = queue.ping()
    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
