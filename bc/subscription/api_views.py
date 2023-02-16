from datetime import timedelta
from http import HTTPStatus

from django.conf import settings
from django.core.cache import cache
from django_rq.queues import get_queue
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response

from bc.subscription.exceptions import (
    IdempotencyKeyMissing,
    WebhookNotSupported,
)

from .api_permissions import AllowListPermission
from .models import FilingWebhookEvent
from .tasks import process_filing_webhook_event

queue = get_queue("default")


@api_view(["POST"])
@permission_classes([AllowListPermission])
def handle_cl_webhook(request: Request) -> Response:
    """
    Receives a docket alert webhook from CourtListener.
    """

    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        raise IdempotencyKeyMissing()

    data = request.data
    if data["webhook"]["event_type"] != 1:
        raise WebhookNotSupported()

    cache_idempotency_key = cache.get(idempotency_key)
    if cache_idempotency_key:
        return Response(status=HTTPStatus.OK)

    sorted_results = sorted(
        data["payload"]["results"], key=lambda d: d["recap_sequence_number"]
    )
    for result in sorted_results:
        cl_docket_id = result["docket"]
        long_description = result["description"]
        document_number = result.get("entry_number")
        for doc in result["recap_documents"]:
            filing = FilingWebhookEvent.objects.create(
                docket_id=cl_docket_id,
                pacer_doc_id=doc["pacer_doc_id"],
                document_number=document_number,
                attachment_number=doc.get("attachment_number"),
                short_description=doc["description"],
                long_description=long_description,
            )

            queue.enqueue_in(
                timedelta(seconds=settings.WEBHOOK_DELAY_TIME),
                process_filing_webhook_event,
                filing.pk,
            )

    # Save the idempotency key for two days after the webhook is handled
    cache.set(idempotency_key, True, 60 * 60 * 24 * 2)

    return Response(request.data, status=HTTPStatus.CREATED)
