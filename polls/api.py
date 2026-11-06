import json
import html
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import File, Form, Router, Schema
from ninja.files import UploadedFile
from ninja.pagination import paginate
from ninja_jwt.authentication import JWTAuth

from accounts.models import User
from accounts.permissions import require_poll_ownership, require_role
from notifications.email import send_transactional_email
from notifications.models import Notification
from payments.models import Payment
from payments.services import (
    confirm_inline_poll_payment,
    create_inline_poll_payment,
    verify_paystack_transaction,
)
from voting.models import Vote, VoteAuditLog
from voting.services import create_vote, is_rate_limited

from .models import Contestant, Poll
from .schemas import (
    ContestantCreateSchema,
    ContestantSchema,
    PollCreateSchema,
    PollSchema,
    PollUpdateSchema,
    InlineVoteCheckoutSchema,
    VoteSchema,
)

logger = logging.getLogger(__name__)
router = Router(tags=["Polls"])


class VoteTrendPoint(Schema):
    date: date
    votes: int


def _poll_queryset():
    return (
        Poll.objects.select_related("creator")
        .prefetch_related("contestants")
        .annotate(annotated_votes_count=Sum("contestants__votes"))
    )


@router.get("/", response=list[PollSchema])
@paginate
def list_polls(
    request,
    category: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    ordering: Optional[str] = None,
):
    queryset = _poll_queryset()
    if category:
        queryset = queryset.filter(category__iexact=category)
    if status:
        queryset = queryset.filter(status=status)
    else:
        queryset = queryset.exclude(status=Poll.Status.DRAFT)
    if search:
        queryset = queryset.filter(
            Q(title__icontains=search)
            | Q(description__icontains=search)
            | Q(organizer__icontains=search)
        )
    if ordering == "votes":
        return queryset.order_by("-annotated_votes_count", "-created_at")
    if ordering == "ending_soon":
        return queryset.order_by("ends_at", "-created_at")
    if ordering == "alphabetical":
        return queryset.order_by("title")
    return queryset.order_by("-created_at")


@router.get("/my-polls/", response=list[PollSchema], auth=JWTAuth())
@paginate
def my_polls(request):
    return _poll_queryset().filter(creator=request.user).order_by("-created_at")


@router.get("/my-polls/analytics/", response=list[VoteTrendPoint], auth=JWTAuth())
@require_role(User.Role.ORGANIZER, User.Role.ADMIN)
def my_poll_vote_trend(request):
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=6)
    start_at = timezone.make_aware(
        datetime.combine(start_date, time.min), timezone.get_current_timezone()
    )
    end_at = start_at + timedelta(days=7)
    daily_totals = {
        row["day"]: row["votes"]
        for row in Vote.objects.filter(
            poll__creator=request.user,
            created_at__gte=start_at,
            created_at__lt=end_at,
        )
        .annotate(day=TruncDate("created_at", tzinfo=timezone.get_current_timezone()))
        .values("day")
        .annotate(votes=Sum("vote_quantity"))
    }
    return [
        {
            "date": start_date + timedelta(days=offset),
            "votes": daily_totals.get(start_date + timedelta(days=offset), 0),
        }
        for offset in range(7)
    ]


@router.get("/{slug}/", response=PollSchema)
def get_poll(request, slug: str):
    return get_object_or_404(_poll_queryset(), slug=slug)


@router.post("/", response={201: PollSchema}, auth=JWTAuth())
@require_role(User.Role.ORGANIZER, User.Role.ADMIN)
def create_poll(
    request,
    data: Form[PollCreateSchema],
    image: Optional[UploadedFile] = File(None),
    contestants: str = Form(""),
):
    poll_data = data.model_dump(exclude_unset=True)
    poll_data["organizer"] = request.user.name
    if poll_data.get("is_paid") and poll_data.get("price_per_vote", Decimal(0)) <= 0:
        return 400, {"detail": "Paid polls must have a price greater than zero."}

    try:
        contestants_data = json.loads(contestants or "[]")
    except json.JSONDecodeError as exc:
        return 400, {"detail": "Contestants must be valid JSON."}
    if not isinstance(contestants_data, list):
        return 400, {"detail": "Contestants must be a list."}
    if not contestants_data or any(not str(item.get("name", "")).strip() for item in contestants_data):
        return 400, {"detail": "Add at least one contestant and provide each contestant's name."}

    with transaction.atomic():
        poll = Poll.objects.create(creator=request.user, image=image, **poll_data)
        for index, item in enumerate(contestants_data):
            contestant_image = request.FILES.get(f"contestant_{index}_image")
            contestant_data = {
                "name": str(item["name"]).strip(),
                "author": str(item.get("author", "")).strip(),
                "description": str(item.get("description", "")).strip(),
            }
            if contestant_image:
                contestant_data["image"] = contestant_image
            Contestant.objects.create(poll=poll, **contestant_data)

    return 201, _poll_queryset().get(pk=poll.pk)


@router.patch("/{slug}/", response=PollSchema, auth=JWTAuth())
@require_poll_ownership
def update_poll(
    request,
    slug: str,
    data: Form[PollUpdateSchema],
    image: Optional[UploadedFile] = File(None),
):
    poll = request.poll

    for attr, value in data.model_dump(exclude_unset=True).items():
        setattr(poll, attr, value)
    if image:
        poll.image = image
    poll.save()
    return _poll_queryset().get(pk=poll.pk)


@router.delete("/{slug}/", auth=JWTAuth())
@require_poll_ownership
def delete_poll(request, slug: str):
    poll = request.poll
    poll.delete()
    return {"success": True}


@router.get("/{slug}/results/", response=list[ContestantSchema])
def get_results(request, slug: str):
    poll = get_object_or_404(Poll, slug=slug)
    return poll.contestants.order_by("-votes", "name")


@router.post("/{slug}/checkout/", response={200: dict, 400: dict, 503: dict})
def initialize_inline_vote_checkout(request, slug: str, payload: InlineVoteCheckoutSchema):
    """Create a payment record and return the server-priced InlineJS details."""
    poll = get_object_or_404(Poll, slug=slug)
    if not poll.is_paid:
        return 400, {"success": False, "message": "This poll does not require payment."}
    if poll.price_per_vote <= 0:
        return 400, {"success": False, "message": "This paid poll has no valid vote price."}
    if poll.price_per_vote * payload.quantity > Decimal("999999.99"):
        return 400, {"success": False, "message": "The checkout amount exceeds the supported payment limit."}
    if poll.status not in (Poll.Status.ACTIVE, Poll.Status.ENDING_SOON, Poll.Status.NEW):
        return 400, {"success": False, "message": "This poll is not currently accepting votes."}
    if poll.ends_at and poll.ends_at <= timezone.now():
        return 400, {"success": False, "message": "This poll has ended."}

    contestant = get_object_or_404(Contestant, id=payload.contestant_id, poll=poll)
    email = payload.email.strip() or (
        request.user.email if getattr(request.user, "is_authenticated", False) else ""
    )
    try:
        validate_email(email)
    except ValidationError:
        return 400, {"success": False, "message": "Enter a valid email address."}

    try:
        return create_inline_poll_payment(
            poll=poll,
            contestant=contestant,
            quantity=payload.quantity,
            email=email,
            user=request.user if getattr(request.user, "is_authenticated", False) else None,
        )
    except ImproperlyConfigured:
        logger.exception("Paystack keys are missing for inline poll checkout")
        return 503, {"success": False, "message": "Online payment is not configured."}
    except Exception:
        logger.exception("Could not create inline poll payment")
        return 503, {"success": False, "message": "Could not initialize checkout. Please try again."}


@router.post("/{slug}/contestants/", response={201: ContestantSchema}, auth=JWTAuth())
@require_poll_ownership
def add_contestant(
    request,
    slug: str,
    data: Form[ContestantCreateSchema],
    image: Optional[UploadedFile] = File(None),
):
    poll = request.poll
    contestant = Contestant.objects.create(poll=poll, image=image, **data.model_dump())
    return 201, contestant


@router.post("/{slug}/vote/", response={200: dict, 400: dict, 402: dict, 429: dict})
def vote(request, slug: str, payload: VoteSchema):
    poll = get_object_or_404(Poll, slug=slug)
    contestant = get_object_or_404(Contestant, id=payload.contestant_id, poll=poll)
    quantity = payload.quantity
    email = payload.email or (request.user.email if request.user.is_authenticated else "")
    payment_ref = (payload.payment_ref or "").strip()
    ip = request.META.get("REMOTE_ADDR")
    user_agent = request.META.get("HTTP_USER_AGENT", "")

    VoteAuditLog.objects.create(poll=poll, ip_address=ip, user_agent=user_agent, action="vote_attempt")
    if not poll.is_paid and is_rate_limited(ip, poll):
        VoteAuditLog.objects.create(poll=poll, ip_address=ip, user_agent=user_agent, action="vote_blocked")
        return 429, {"success": False, "message": "Too many votes. Please slow down."}

    if poll.is_paid:
        if not payment_ref:
            return 400, {"success": False, "message": "Payment reference is required for paid polls."}
        payment = Payment.objects.filter(provider_reference=payment_ref).first()
        metadata = (payment.metadata or {}) if payment else {}
        if (
            not payment
            or metadata.get("flow") != "inline_poll_vote"
            or metadata.get("poll_id") != str(poll.pk)
            or metadata.get("contestant_id") != str(contestant.pk)
            or metadata.get("quantity") != quantity
        ):
            return 400, {"success": False, "message": "Payment reference does not match this vote."}
        if email and email.lower() != payment.email.lower():
            return 400, {"success": False, "message": "Payment email does not match the voter email."}
        email = payment.email

        if payment.status == Payment.Status.CONFIRMED and payment.processed:
            return {"success": True, "message": "Vote already confirmed.", "transaction_ref": payment_ref}

        paystack_data = verify_paystack_transaction(payment_ref)
        if not paystack_data or paystack_data.get("reference") != payment_ref:
            return 402, {"success": False, "message": "Payment verification failed."}
        outcome = confirm_inline_poll_payment(paystack_data, request=request)
        if outcome not in ("confirmed", "duplicate"):
            return 402, {"success": False, "message": "Payment could not be confirmed for this vote."}
        return {
            "success": True,
            "message": "Vote cast successfully" if outcome == "confirmed" else "Vote already confirmed.",
            "transaction_ref": payment_ref,
        }

    if poll.status not in (Poll.Status.ACTIVE, Poll.Status.ENDING_SOON, Poll.Status.NEW):
        return 400, {"success": False, "message": "This poll is not currently accepting votes."}
    if poll.ends_at and poll.ends_at <= timezone.now():
        return 400, {"success": False, "message": "This poll has ended."}

    total_amount = Decimal("0")

    with transaction.atomic():
        Contestant.objects.filter(id=contestant.id).update(votes=F("votes") + quantity)
        create_vote(
            poll=poll,
            contestant=contestant,
            voter=request.user if request.user.is_authenticated else None,
            voter_email=email,
            quantity=quantity,
            payment_ref="",
            ip=ip,
            user_agent=user_agent,
            is_paid=False,
            amount=total_amount,
        )

        notification = Notification.objects.create(
            receiver=poll.creator,
            title=f"New vote on {poll.title}"[:30],
            description=f"{quantity} vote(s) cast for {contestant.name}.",
            email=poll.creator.email,
        )

    try:
        send_transactional_email(
            to=poll.creator.email,
            subject=f"New vote on {poll.title}",
            text=f"{quantity} vote(s) were cast for {contestant.name} in {poll.title}.",
            html=f"<p>{quantity} vote(s) were cast for <strong>{html.escape(contestant.name)}</strong> in {html.escape(poll.title)}.</p>",
        )
    except Exception:
        # Email delivery must not roll back an already committed vote.
        logger.exception("Could not email vote notification %s", notification.id)

    VoteAuditLog.objects.create(poll=poll, ip_address=ip, user_agent=user_agent, action="vote_success")
    return {"success": True, "message": "Vote cast successfully"}
