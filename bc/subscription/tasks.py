from django.conf import settings
from django.db import transaction
from django_rq.queues import get_queue
from rq import Retry

from bc.channel.models import Channel, Post
from bc.channel.selectors import get_enabled_channels
from bc.core.utils.images import add_sponsored_text_to_thumbnails
from bc.core.utils.microservices import get_thumbnails_from_range
from bc.core.utils.status.selectors import get_template_for_channel
from bc.core.utils.status.templates import DO_NOT_PAY, DO_NOT_POST
from bc.sponsorship.selectors import get_active_sponsorship
from bc.sponsorship.services import log_purchase
from bc.subscription.utils.courtlistener import (
    download_pdf_from_cl,
    lookup_document_by_doc_id,
    purchase_pdf_by_doc_id,
)

from .models import FilingWebhookEvent, Subscription

queue = get_queue("default")


@transaction.atomic
def process_filing_webhook_event(fwe_pk: int) -> FilingWebhookEvent:
    """Process an event from a CL webhook.

    This function links a webhook event to one of the records in the
    subscription table or ignores it if the bot is not following the
    case.

    :param fwe_pk: The PK of the FilingWebhookEvent record.
    :return: The FilingWebhookEvent object that was updated.
    """
    filing_webhook_event = FilingWebhookEvent.objects.get(pk=fwe_pk)

    if not filing_webhook_event.docket_id:
        return filing_webhook_event

    try:
        with transaction.atomic():
            subscription = Subscription.objects.get(
                cl_docket_id=filing_webhook_event.docket_id
            )
    except Subscription.DoesNotExist:
        # We don't know why we got this webhook event. Ignore it.
        filing_webhook_event.status = FilingWebhookEvent.FAILED
        filing_webhook_event.save()
        return filing_webhook_event

    filing_webhook_event.subscription = subscription
    filing_webhook_event.status = FilingWebhookEvent.SUCCESSFUL
    filing_webhook_event.save()

    return filing_webhook_event


@transaction.atomic
def check_webhook_before_posting(fwe_pk: int):
    """Checks the webhook event before start posting

    This function checks the description of the event to avoid
    creating post for junk docket entries, also checks if the document
    associated with the webhook is available in the RECAP archive to
    retrieve it and use it to create a post in the enabled channels.

    :param fwe_pk: The PK of the FilingWebhookEvent record.
    :return: the FilingWebhookEvent object used to .
    """
    filing_webhook_event = FilingWebhookEvent.objects.get(pk=fwe_pk)

    if filing_webhook_event.status != FilingWebhookEvent.SUCCESSFUL:
        return filing_webhook_event

    # check if the webhook event is linked to a subscription record
    if not filing_webhook_event.subscription:
        raise AssertionError(
            "The webhook event doesn't have a relationship with a subscription record"
        )

    # check the description to filter junk docket entries
    if DO_NOT_POST.search(filing_webhook_event.description):
        filing_webhook_event.status = FilingWebhookEvent.IGNORED
        filing_webhook_event.save(update_fields=["status"])
        return filing_webhook_event

    # check if the document is available or there's a sponsorship to purchase it.
    document = None
    cl_document = lookup_document_by_doc_id(filing_webhook_event.doc_id)
    if cl_document["filepath_local"]:
        document = download_pdf_from_cl(cl_document["filepath_local"])
    else:
        sponsorship = get_active_sponsorship()
        if (
            sponsorship
            and filing_webhook_event.pacer_doc_id
            and not DO_NOT_PAY.search(filing_webhook_event.description)
        ):
            purchase_pdf_by_doc_id(filing_webhook_event.doc_id)
            filing_webhook_event.status = (
                FilingWebhookEvent.WAITING_FOR_DOCUMENT
            )
            filing_webhook_event.save(update_fields=["status"])
            return filing_webhook_event

    # Got the document or no sponsorship. Tweet and toot.
    for channel in get_enabled_channels():
        queue.enqueue(
            make_post_for_webhook_event,
            channel.pk,
            filing_webhook_event.subscription.pk,
            filing_webhook_event.pk,
            document,
            retry=Retry(
                max=settings.RQ_MAX_NUMBER_OF_RETRIES,
                interval=settings.RQ_RETRY_INTERVAL,
            ),
        )

    return filing_webhook_event


@transaction.atomic
def process_fetch_webhook_event(fwe_pk: int):
    """Process a RECAP fetch webhook event from CL.

    This functions retrieves the new document available in the
    RECAP archive, creates a new entry related to the purchase
    in the ledger and schedule the tasks to create new post in
    the enabled channels.

    :param fwe_pk: The PK of the FilingWebhookEvent record.
    :return: A FilingWebhookEvent object that was updated.
    """
    filing_webhook_event = FilingWebhookEvent.objects.get(pk=fwe_pk)

    # check if the webhook event is linked to a subscription record
    if not filing_webhook_event.subscription:
        raise AssertionError(
            "The webhook event doesn't have a relationship with a subscription record"
        )

    filing_webhook_event.status = FilingWebhookEvent.SUCCESSFUL
    filing_webhook_event.save(update_fields=["status"])

    cl_document = lookup_document_by_doc_id(filing_webhook_event.doc_id)
    document = download_pdf_from_cl(cl_document["filepath_local"])

    sponsorship = get_active_sponsorship()
    sponsor_message = None
    if sponsorship:
        sponsor_message = sponsorship.watermark_message
        log_purchase(
            sponsorship, filing_webhook_event, cl_document["page_count"]
        )

    for channel in get_enabled_channels():
        queue.enqueue(
            make_post_for_webhook_event,
            channel.pk,
            filing_webhook_event.subscription.pk,
            filing_webhook_event.pk,
            document,
            sponsor_message,
            retry=Retry(
                max=settings.RQ_MAX_NUMBER_OF_RETRIES,
                interval=settings.RQ_RETRY_INTERVAL,
            ),
        )

    return filing_webhook_event


@transaction.atomic
def make_post_for_webhook_event(
    channel_pk: int,
    subscription_pk: int,
    fwe_pk: int,
    document: bytes | None,
    sponsor_text: str | None = None,
) -> Post:
    """Post a new status in the given channel using the data of the given webhook
    event and subscription.

    Args:
        channel_pk (int): The pk of the channel where the post will be created.
        subscription_pk (int): The pk of the subscription related to the webhook event.
        fwe_pk (int): The PK of the FilingWebhookEvent record.
        document (bytes | None): document content(if available) as bytes.
        sponsor_text (str | None): sponsor message to include in the thumbnails.

    Returns:
        Post: A post object with the data of the new status that was created
    """

    channel = Channel.objects.get(pk=channel_pk)
    subscription = Subscription.objects.get(pk=subscription_pk)
    filing_webhook_event = FilingWebhookEvent.objects.get(pk=fwe_pk)

    template = get_template_for_channel(
        channel.service, filing_webhook_event.document_number
    )

    message, image = template.format(
        docket=subscription.name_with_summary,
        description=filing_webhook_event.description,
        doc_num=filing_webhook_event.document_number,
        pdf_link=filing_webhook_event.cl_pdf_or_pacer_url,
        docket_link=filing_webhook_event.cl_docket_url,
    )

    files = None
    if document:
        thumbnail_range = "[1,2,3]" if image else "[1,2,3,4]"
        files = get_thumbnails_from_range(document, thumbnail_range)

    if sponsor_text and files:
        files = add_sponsored_text_to_thumbnails(files, sponsor_text)

    api = channel.get_api_wrapper()
    api_post_id = api.add_status(message, image, files)

    return Post.objects.create(
        filing_webhook_event=filing_webhook_event,
        channel=channel,
        object_id=api_post_id,
        text=message,
    )
