"""Administrator-only user, audit, and contest moderation APIs."""

import uuid
from datetime import datetime
from typing import Optional

from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.pagination import paginate
from ninja_jwt.authentication import JWTAuth

from accounts.models import OrganizerProfile, User
from accounts.permissions import require_role
from payments.models import Payment
from polls.models import Contest, Poll
from polls.services import suspend_contest
from voting.models import AuditLog, Vote

router = Router(tags=["Administration"])


class RoleUpdateSchema(Schema):
    role: str


class SuspendContestSchema(Schema):
    reason: str = ""


class AdminUserSchema(Schema):
    id: uuid.UUID
    name: str
    email: str
    role: str
    is_active: bool
    is_email_verified: bool
    created_at: datetime


class AuditLogSchema(Schema):
    id: uuid.UUID
    action_type: str
    resource_type: str
    resource_id: str
    user_id: Optional[uuid.UUID]
    timestamp: datetime
    ip_hash: str
    device_fingerprint: str
    metadata: dict


class AdminDashboardActivitySchema(Schema):
    id: uuid.UUID
    action_type: str
    resource_type: str
    timestamp: datetime


class AdminDashboardSchema(Schema):
    total_users: int
    active_users: int
    voter_accounts: int
    organizer_accounts: int
    administrator_accounts: int
    pending_organizer_requests: int
    total_polls: int
    active_polls: int
    total_contests: int
    live_contests: int
    suspended_contests: int
    total_votes: int
    confirmed_payments: int
    pending_payments: int
    recent_activity: list[AdminDashboardActivitySchema]


@router.get("/dashboard/", response=AdminDashboardSchema, auth=JWTAuth())
@require_role(User.Role.ADMIN)
def dashboard_summary(request):
    user_counts = User.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        voters=Count("id", filter=Q(role=User.Role.USER)),
        organizers=Count("id", filter=Q(role=User.Role.ORGANIZER)),
        administrators=Count("id", filter=Q(role=User.Role.ADMIN)),
    )
    poll_counts = Poll.objects.aggregate(
        total=Count("id"),
        active=Count(
            "id",
            filter=Q(
                status__in=(Poll.Status.ACTIVE, Poll.Status.ENDING_SOON, Poll.Status.NEW)
            ),
        ),
    )
    contest_counts = Contest.objects.aggregate(
        total=Count("id"),
        live=Count("id", filter=Q(state=Contest.State.LIVE)),
        suspended=Count("id", filter=Q(state=Contest.State.SUSPENDED)),
    )
    payment_counts = Payment.objects.aggregate(
        confirmed=Count("id", filter=Q(status=Payment.Status.CONFIRMED)),
        pending=Count("id", filter=Q(status=Payment.Status.PENDING)),
    )

    return {
        "total_users": user_counts["total"],
        "active_users": user_counts["active"],
        "voter_accounts": user_counts["voters"],
        "organizer_accounts": user_counts["organizers"],
        "administrator_accounts": user_counts["administrators"],
        "pending_organizer_requests": OrganizerProfile.objects.filter(
            is_approved=False
        ).count(),
        "total_polls": poll_counts["total"],
        "active_polls": poll_counts["active"],
        "total_contests": contest_counts["total"],
        "live_contests": contest_counts["live"],
        "suspended_contests": contest_counts["suspended"],
        "total_votes": Vote.objects.aggregate(total=Sum("vote_quantity"))["total"] or 0,
        "confirmed_payments": payment_counts["confirmed"],
        "pending_payments": payment_counts["pending"],
        "recent_activity": list(
            AuditLog.objects.order_by("-timestamp").values(
                "id", "action_type", "resource_type", "timestamp"
            )[:6]
        ),
    }


@router.get("/users/", response=list[AdminUserSchema], auth=JWTAuth())
@require_role(User.Role.ADMIN)
@paginate
def list_users(request, role: Optional[str] = None, search: Optional[str] = None):
    queryset = User.objects.all().order_by("-created_at")
    if role:
        queryset = queryset.filter(role=role)
    if search:
        queryset = queryset.filter(Q(email__icontains=search) | Q(name__icontains=search))
    return queryset


@router.patch("/users/{user_id}/role/", response={200: dict}, auth=JWTAuth())
@require_role(User.Role.ADMIN)
def update_user_role(request, user_id: str, payload: RoleUpdateSchema):
    if payload.role not in User.Role.values:
        raise ValidationError({"role": "Choose USER, ORGANIZER, or ADMIN."})
    user = get_object_or_404(User, pk=user_id)
    if user.pk == request.user.pk and payload.role != User.Role.ADMIN:
        active_admins = User.objects.filter(role=User.Role.ADMIN, is_active=True).count()
        if active_admins <= 1:
            raise ValidationError("The last active administrator cannot demote itself.")
    previous = user.role
    user.role = payload.role
    user.save(update_fields=["role", "updated_at"])
    AuditLog.objects.create(
        user=request.user,
        action_type=AuditLog.ActionType.ROLE_CHANGE,
        resource_type="User",
        resource_id=str(user.pk),
        metadata={"target_user_id": str(user.pk), "old_role": previous, "new_role": user.role},
    )
    return {"success": True, "user_id": str(user.pk), "old_role": previous, "role": user.role}


@router.get("/audit-logs/", response=list[AuditLogSchema], auth=JWTAuth())
@require_role(User.Role.ADMIN)
@paginate
def list_audit_logs(
    request,
    action_type: Optional[str] = None,
    resource_type: Optional[str] = None,
    user_id: Optional[str] = None,
):
    queryset = AuditLog.objects.select_related("user").order_by("-timestamp")
    if action_type:
        queryset = queryset.filter(action_type=action_type)
    if resource_type:
        queryset = queryset.filter(resource_type=resource_type)
    if user_id:
        queryset = queryset.filter(user_id=user_id)
    return queryset


@router.post("/contests/{slug}/suspend/", response={200: dict}, auth=JWTAuth())
@require_role(User.Role.ADMIN)
def suspend(request, slug: str, payload: SuspendContestSchema):
    contest = get_object_or_404(Contest, slug=slug)
    contest = suspend_contest(contest, request.user, payload.reason)
    return {"success": True, "contest_id": str(contest.pk), "state": contest.state}
