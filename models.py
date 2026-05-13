from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime, date
from database import Base

DEADLINE_DAYS = {"reel": 3, "short": 3, "video": 7}

SCORE_LABELS = {
    "viral": "Viral",
    "strong": "Strong",
    "average": "Average",
    "below_average": "Below Avg",
    "flop": "Flop",
}


class WorkDay(Base):
    __tablename__ = "workdays"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, unique=True, nullable=False)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    projects = relationship("Project", back_populates="work_day")


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    type = Column(String(20), nullable=False)
    filmed_date = Column(Date)
    deadline = Column(Date)
    status = Column(String(20), default="filmed")
    instagram_url = Column(String(500))
    youtube_url = Column(String(500))
    notes = Column(Text)
    delay_reason = Column(Text)
    work_day_id = Column(Integer, ForeignKey("workdays.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)

    work_day = relationship("WorkDay", back_populates="projects")
    edit_sessions = relationship(
        "EditSession", back_populates="project",
        cascade="all, delete-orphan", order_by="EditSession.date"
    )
    engagement_logs = relationship(
        "EngagementLog", back_populates="project",
        cascade="all, delete-orphan", order_by="EngagementLog.logged_at"
    )

    @property
    def is_overdue(self):
        if self.status == "complete":
            return False
        return bool(self.deadline and date.today() > self.deadline)

    @property
    def days_until_deadline(self):
        if not self.deadline:
            return None
        return (self.deadline - date.today()).days

    @property
    def total_edit_minutes(self):
        return sum(s.duration_minutes for s in self.edit_sessions)

    @property
    def total_edit_hours(self):
        return round(self.total_edit_minutes / 60, 1)

    @property
    def days_to_complete(self):
        if self.completed_at and self.filmed_date:
            return (self.completed_at.date() - self.filmed_date).days
        return None

    @property
    def met_deadline(self):
        if not self.completed_at or not self.deadline:
            return None
        return self.completed_at.date() <= self.deadline

    @property
    def needs_instagram(self):
        return self.type == "reel"

    @property
    def display_status(self):
        if self.is_overdue:
            return "overdue"
        return self.status

    @property
    def type_label(self):
        return {"reel": "Reel", "short": "Short", "video": "Video"}.get(self.type, self.type)


class EditSession(Base):
    __tablename__ = "edit_sessions"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    date = Column(Date, nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    project = relationship("Project", back_populates="edit_sessions")

    @property
    def duration_display(self):
        h = self.duration_minutes // 60
        m = self.duration_minutes % 60
        if h and m:
            return f"{h}h {m}m"
        if h:
            return f"{h}h"
        return f"{m}m"


class EngagementLog(Base):
    __tablename__ = "engagement_logs"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    platform = Column(String(20), nullable=False)
    check_period = Column(Integer, nullable=False)
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    score = Column(String(20))
    logged_at = Column(DateTime, default=datetime.utcnow)
    project = relationship("Project", back_populates="engagement_logs")

    @property
    def score_label(self):
        return SCORE_LABELS.get(self.score, self.score or "—")

    @property
    def platform_label(self):
        return {"instagram": "Instagram", "youtube": "YouTube"}.get(self.platform, self.platform)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    performed_by = Column(String(100), nullable=False, default="Unknown")
    action = Column(String(50), nullable=False)      # created, updated, deleted, completed, etc.
    entity_type = Column(String(50), nullable=False) # project, workday, edit_session, engagement
    entity_id = Column(Integer)
    entity_title = Column(String(200))
    details = Column(Text)
    ip_address = Column(String(45))
