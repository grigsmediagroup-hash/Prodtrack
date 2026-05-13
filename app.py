from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import date, datetime, timedelta
from typing import Optional, List
import os

from database import get_db, init_db
from models import WorkDay, Project, EditSession, EngagementLog, AuditLog, DEADLINE_DAYS

app = FastAPI(title="Production Tracker")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

SET_NAME_PATHS = {"/set-name", "/set-name-post"}


# ── Middleware: require identity ───────────────────────────────────────────────

@app.middleware("http")
async def require_name_middleware(request: Request, call_next):
    path = request.url.path
    if path not in SET_NAME_PATHS:
        user_name = request.cookies.get("user_name", "").strip()
        if not user_name:
            return RedirectResponse("/set-name", status_code=302)
    return await call_next(request)


# ── Helpers ────────────────────────────────────────────────────────────────────

def current_user(request: Request) -> str:
    return request.cookies.get("user_name", "Unknown").strip()


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "")


def audit(
    db: Session,
    request: Request,
    action: str,
    entity_type: str,
    entity_id: int,
    entity_title: str,
    details: str = "",
):
    db.add(AuditLog(
        performed_by=current_user(request),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_title=entity_title,
        details=details,
        ip_address=client_ip(request),
    ))


SCORE_THRESHOLDS = {
    ("reel",  7):  [500,  2_000,  10_000,  50_000],
    ("reel",  30): [1_000, 5_000, 25_000, 100_000],
    ("short", 7):  [500,  2_000,  10_000,  50_000],
    ("short", 30): [1_000, 5_000, 25_000, 100_000],
    ("video", 7):  [200,  1_000,   3_000,  10_000],
    ("video", 30): [500,  2_000,   7_000,  25_000],
}


def calculate_score(project_type: str, check_period: int, views: int) -> str:
    t = SCORE_THRESHOLDS.get((project_type, check_period), [500, 2000, 10000, 50000])
    if views < t[0]: return "flop"
    if views < t[1]: return "below_average"
    if views < t[2]: return "average"
    if views < t[3]: return "strong"
    return "viral"


@app.on_event("startup")
def startup():
    init_db()


# ── Identity ───────────────────────────────────────────────────────────────────

@app.get("/set-name")
def set_name_page(request: Request):
    return templates.TemplateResponse("set_name.html", {"request": request, "today": date.today()})


@app.post("/set-name-post")
def set_name_post(name: str = Form(...)):
    name = name.strip()[:80]
    if not name:
        return RedirectResponse("/set-name", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("user_name", name, max_age=60 * 60 * 24 * 365, httponly=False, samesite="lax")
    return response


@app.post("/change-name")
def change_name(name: str = Form(...)):
    name = name.strip()[:80]
    if not name:
        return RedirectResponse("/", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("user_name", name, max_age=60 * 60 * 24 * 365, httponly=False, samesite="lax")
    return response


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    today_workday = db.query(WorkDay).filter(WorkDay.date == today).first()

    active = db.query(Project).filter(Project.status != "complete").all()
    overdue = [p for p in active if p.is_overdue]
    due_soon = [p for p in active if not p.is_overdue
                and p.days_until_deadline is not None
                and 0 <= p.days_until_deadline <= 2]
    on_track = [p for p in active if p not in overdue and p not in due_soon]

    recent = (db.query(Project)
              .filter(Project.status == "complete")
              .order_by(Project.completed_at.desc())
              .limit(6).all())

    total = db.query(Project).count()
    completed_all = db.query(Project).filter(Project.status == "complete").all()
    on_time_count = sum(1 for p in completed_all if p.met_deadline)
    on_time_pct = round(on_time_count / len(completed_all) * 100) if completed_all else 0

    recent_audit = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(5).all()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "today": today,
        "user": current_user(request),
        "today_workday": today_workday,
        "overdue": overdue,
        "due_soon": due_soon,
        "on_track": on_track,
        "recent": recent,
        "recent_audit": recent_audit,
        "stats": {
            "total": total,
            "active": len(active),
            "completed": len(completed_all),
            "on_time_pct": on_time_pct,
        },
    })


# ── Work Days ──────────────────────────────────────────────────────────────────

@app.get("/workdays")
def workdays_list(request: Request, db: Session = Depends(get_db)):
    workdays = db.query(WorkDay).order_by(WorkDay.date.desc()).all()
    today = date.today()
    today_logged = db.query(WorkDay).filter(WorkDay.date == today).first()
    all_projects = db.query(Project).order_by(Project.created_at.desc()).all()
    return templates.TemplateResponse("workdays.html", {
        "request": request,
        "today": today,
        "user": current_user(request),
        "workdays": workdays,
        "today_logged": today_logged,
        "all_projects": all_projects,
    })


@app.post("/workdays/log")
def log_workday(
    request: Request,
    workday_date: str = Form(...),
    notes: str = Form(""),
    project_ids: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    d = date.fromisoformat(workday_date)
    wd = db.query(WorkDay).filter(WorkDay.date == d).first()
    is_new = wd is None
    if not wd:
        wd = WorkDay(date=d, notes=notes or None)
        db.add(wd)
        db.flush()
    elif notes:
        wd.notes = notes

    linked_titles = []
    if project_ids:
        for p in db.query(Project).filter(Project.id.in_(project_ids)).all():
            p.work_day_id = wd.id
            if not p.filmed_date:
                p.filmed_date = d
            if not p.deadline:
                p.deadline = d + timedelta(days=DEADLINE_DAYS.get(p.type, 3))
            linked_titles.append(p.title)

    action = "created" if is_new else "updated"
    details = f"Date: {d.strftime('%d %b %Y')}"
    if linked_titles:
        details += f" | Projects: {', '.join(linked_titles)}"
    audit(db, request, action, "workday", wd.id, d.strftime("%d %b %Y"), details)
    db.commit()
    return RedirectResponse("/workdays?msg=Work+day+logged", status_code=303)


@app.post("/workdays/{workday_id}/delete")
def delete_workday(workday_id: int, request: Request, db: Session = Depends(get_db)):
    wd = db.query(WorkDay).filter(WorkDay.id == workday_id).first()
    if wd:
        label = wd.date.strftime("%d %b %Y")
        for p in wd.projects:
            p.work_day_id = None
        audit(db, request, "deleted", "workday", workday_id, label, f"Removed work day log for {label}")
        db.delete(wd)
        db.commit()
    return RedirectResponse("/workdays", status_code=303)


# ── Projects ───────────────────────────────────────────────────────────────────

@app.get("/projects")
def projects_list(
    request: Request,
    status: Optional[str] = None,
    type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Project)
    if status == "overdue":
        all_p = q.filter(Project.status != "complete").all()
        projects = [p for p in all_p if p.is_overdue]
    elif status:
        projects = q.filter(Project.status == status).order_by(Project.created_at.desc()).all()
    else:
        projects = q.order_by(Project.created_at.desc()).all()

    if type:
        projects = [p for p in projects if p.type == type]

    return templates.TemplateResponse("projects.html", {
        "request": request,
        "today": date.today(),
        "user": current_user(request),
        "projects": projects,
        "filter_status": status,
        "filter_type": type,
    })


@app.post("/projects")
def create_project(
    request: Request,
    title: str = Form(...),
    type: str = Form(...),
    filmed_date: str = Form(""),
    deadline: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    fd = date.fromisoformat(filmed_date) if filmed_date else None
    if deadline:
        dl = date.fromisoformat(deadline)
    elif fd:
        dl = fd + timedelta(days=DEADLINE_DAYS.get(type, 3))
    else:
        dl = None

    p = Project(title=title, type=type, filmed_date=fd, deadline=dl, notes=notes or None)
    db.add(p)
    db.flush()
    details = f"Type: {type}"
    if fd:
        details += f" | Filmed: {fd.strftime('%d %b %Y')}"
    if dl:
        details += f" | Deadline: {dl.strftime('%d %b %Y')}"
    audit(db, request, "created", "project", p.id, title, details)
    db.commit()
    db.refresh(p)
    return RedirectResponse(f"/projects/{p.id}?msg=Project+created", status_code=303)


@app.get("/projects/{project_id}")
def project_detail(project_id: int, request: Request, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return templates.TemplateResponse("project_detail.html", {
        "request": request,
        "today": date.today(),
        "user": current_user(request),
        "project": p,
    })


@app.post("/projects/{project_id}/update")
def update_project(
    project_id: int,
    request: Request,
    title: str = Form(""),
    notes: str = Form(""),
    deadline: str = Form(""),
    filmed_date: str = Form(""),
    db: Session = Depends(get_db),
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404)
    changes = []
    if title and title != p.title:
        changes.append(f"title: '{p.title}' → '{title}'")
        p.title = title
    if filmed_date:
        new_fd = date.fromisoformat(filmed_date)
        if new_fd != p.filmed_date:
            changes.append(f"filmed date changed")
            p.filmed_date = new_fd
    if deadline:
        new_dl = date.fromisoformat(deadline)
        if new_dl != p.deadline:
            changes.append(f"deadline: {new_dl.strftime('%d %b %Y')}")
            p.deadline = new_dl
    p.notes = notes or None
    audit(db, request, "updated", "project", project_id, p.title,
          " | ".join(changes) if changes else "Details updated")
    db.commit()
    return RedirectResponse(f"/projects/{project_id}?msg=Updated", status_code=303)


@app.post("/projects/{project_id}/start-editing")
def start_editing(project_id: int, request: Request, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if p and p.status == "filmed":
        p.status = "editing"
        audit(db, request, "started_editing", "project", project_id, p.title, "Status changed: filmed → editing")
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/complete")
def complete_project(
    project_id: int,
    request: Request,
    youtube_url: str = Form(""),
    instagram_url: str = Form(""),
    db: Session = Depends(get_db),
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404)
    if youtube_url:
        p.youtube_url = youtube_url
    if instagram_url:
        p.instagram_url = instagram_url
    p.status = "complete"
    p.completed_at = datetime.utcnow()
    links = []
    if youtube_url:
        links.append("YouTube ✓")
    if instagram_url:
        links.append("Instagram ✓")
    on_time = p.met_deadline
    details = " | ".join(links)
    if on_time is not None:
        details += f" | {'On time' if on_time else 'Late'}"
    audit(db, request, "completed", "project", project_id, p.title, details)
    db.commit()
    return RedirectResponse(f"/projects/{project_id}?msg=Project+marked+complete", status_code=303)


@app.post("/projects/{project_id}/log-delay")
def log_delay(
    project_id: int,
    request: Request,
    delay_reason: str = Form(...),
    db: Session = Depends(get_db),
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if p:
        stamp = datetime.now().strftime("%d %b %Y")
        existing = p.delay_reason or ""
        p.delay_reason = f"[{stamp}] {delay_reason}\n{existing}".strip()
        audit(db, request, "delay_logged", "project", project_id, p.title, delay_reason[:200])
        db.commit()
    return RedirectResponse(f"/projects/{project_id}?msg=Delay+logged", status_code=303)


@app.post("/projects/{project_id}/delete")
def delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if p:
        audit(db, request, "deleted", "project", project_id, p.title,
              f"Type: {p.type} | Status was: {p.status}")
        db.delete(p)
        db.commit()
    return RedirectResponse("/projects", status_code=303)


# ── Edit Sessions ──────────────────────────────────────────────────────────────

@app.post("/projects/{project_id}/edit-sessions")
def log_edit_session(
    project_id: int,
    request: Request,
    session_date: str = Form(...),
    duration_minutes: int = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    p = db.query(Project).filter(Project.id == project_id).first()
    s = EditSession(
        project_id=project_id,
        date=date.fromisoformat(session_date),
        duration_minutes=duration_minutes,
        notes=notes or None,
    )
    db.add(s)
    db.flush()
    h = duration_minutes // 60
    m = duration_minutes % 60
    dur = f"{h}h {m}m" if h else f"{m}m"
    title = p.title if p else f"project #{project_id}"
    details = f"{dur} on {session_date}"
    if notes:
        details += f" — {notes}"
    audit(db, request, "edit_session_logged", "edit_session", s.id, title, details)
    db.commit()
    return RedirectResponse(f"/projects/{project_id}?msg=Edit+session+logged", status_code=303)


@app.post("/edit-sessions/{session_id}/delete")
def delete_edit_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    s = db.query(EditSession).filter(EditSession.id == session_id).first()
    project_id = s.project_id if s else 0
    if s:
        p = s.project
        title = p.title if p else f"project #{project_id}"
        audit(db, request, "deleted", "edit_session", session_id, title,
              f"Removed {s.duration_display} session from {s.date.strftime('%d %b %Y')}")
        db.delete(s)
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ── Engagement ─────────────────────────────────────────────────────────────────

@app.post("/projects/{project_id}/engagement")
def log_engagement(
    project_id: int,
    request: Request,
    platform: str = Form(...),
    check_period: int = Form(...),
    views: int = Form(0),
    likes: int = Form(0),
    comments: int = Form(0),
    shares: int = Form(0),
    db: Session = Depends(get_db),
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404)
    score = calculate_score(p.type, check_period, views)
    log = EngagementLog(
        project_id=project_id,
        platform=platform,
        check_period=check_period,
        views=views,
        likes=likes,
        comments=comments,
        shares=shares,
        score=score,
    )
    db.add(log)
    db.flush()
    plat_label = {"instagram": "Instagram", "youtube": "YouTube"}.get(platform, platform)
    details = f"{plat_label} {check_period}d — {views:,} views, {likes:,} likes → {score.replace('_', ' ').title()}"
    audit(db, request, "engagement_logged", "engagement", log.id, p.title, details)
    db.commit()
    return RedirectResponse(f"/projects/{project_id}?msg=Engagement+logged", status_code=303)


@app.post("/engagement/{log_id}/delete")
def delete_engagement(log_id: int, request: Request, db: Session = Depends(get_db)):
    log = db.query(EngagementLog).filter(EngagementLog.id == log_id).first()
    project_id = log.project_id if log else 0
    if log:
        p = log.project
        title = p.title if p else f"project #{project_id}"
        audit(db, request, "deleted", "engagement", log_id, title,
              f"Removed {log.platform_label} {log.check_period}d engagement log")
        db.delete(log)
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ── Audit Log ──────────────────────────────────────────────────────────────────

@app.get("/audit")
def audit_log(
    request: Request,
    entity_type: Optional[str] = None,
    performed_by: Optional[str] = None,
    page: int = 1,
    db: Session = Depends(get_db),
):
    q = db.query(AuditLog).order_by(AuditLog.timestamp.desc())
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    if performed_by:
        q = q.filter(AuditLog.performed_by == performed_by)

    per_page = 50
    total = q.count()
    entries = q.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = max(1, (total + per_page - 1) // per_page)

    all_users = [r[0] for r in db.query(AuditLog.performed_by).distinct().all()]

    return templates.TemplateResponse("audit_log.html", {
        "request": request,
        "today": date.today(),
        "user": current_user(request),
        "entries": entries,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "filter_entity": entity_type,
        "filter_user": performed_by,
        "all_users": all_users,
    })
