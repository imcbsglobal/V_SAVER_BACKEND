"""
offer_app/scheduler.py
─────────────────────────────────────────────────────────────────────────────
Background scheduler that fires CommonNotification records whose
scheduled_at time has arrived.

Uses APScheduler's BackgroundScheduler (runs in a daemon thread inside the
Django process — no separate worker or Redis needed).

Started once from AppConfig.ready() so it survives server restarts.
"""

import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


def _fire_due_notifications():
    """
    Called every 60 seconds by APScheduler.
    Finds every CommonNotification that:
      - status == 'scheduled'
      - scheduled_at <= now
    and sends it using the same logic as send_common_notification().
    """
    # Import here (inside the job) to avoid AppRegistryNotReady at startup
    from .models import CommonNotification, ExpoPushToken
    from .push_notifications import send_expo_push_notification

    now = timezone.now()

    due = CommonNotification.objects.filter(
        status='scheduled',
        scheduled_at__lte=now,
    )

    if not due.exists():
        return

    for notif in due:
        try:
            token_qs = ExpoPushToken.objects.select_related('user')
            if notif.target == 'active':
                token_qs = token_qs.filter(user__status='Active')

            tokens = list(token_qs.values_list('token', flat=True))
            dead_tokens = []
            sent_count = 0

            if tokens:
                extra_data = {}
                if notif.image_url:
                    extra_data['imageUrl'] = notif.image_url

                _, dead_tokens = send_expo_push_notification(
                    tokens, notif.title, notif.body, extra_data
                )

                if dead_tokens:
                    ExpoPushToken.objects.filter(token__in=dead_tokens).delete()

                sent_count = len(tokens) - len(dead_tokens)

            notif.status = 'sent'
            notif.sent_at = now
            notif.sent_count = sent_count
            notif.save(update_fields=['status', 'sent_at', 'sent_count'])

            logger.info(
                "[Scheduler] Sent scheduled notification '%s' (id=%s) to %d device(s).",
                notif.title, notif.id, sent_count,
            )

        except Exception as exc:
            logger.exception(
                "[Scheduler] Failed to send notification id=%s: %s", notif.id, exc
            )


def _activate_scheduled_offers():
    """
    Called every 60 seconds by APScheduler.
    Finds every OfferMaster that:
      - status == 'scheduled'
      - valid_from <= today   (offer window has started)
      - valid_to   >= today   (offer window has not yet expired)
    Marks them 'active' and sends a push notification to all registered
    devices — exactly the notification that was skipped at creation time
    because the offer was not yet live.
    """
    from django.utils.timezone import localdate
    from .models import OfferMaster, ExpoPushToken
    from .push_notifications import send_expo_push_notification

    today = localdate()

    due_offers = OfferMaster.objects.filter(
        status='scheduled',
        valid_from__lte=today,
        valid_to__gte=today,
    )

    if not due_offers.exists():
        return

    for offer in due_offers:
        try:
            # Activate the offer
            offer.status = 'active'
            offer.save(update_fields=['status'])

            logger.info(
                "[Scheduler] OfferMaster '%s' (id=%s) is now active — sending push notification.",
                offer.title, offer.id,
            )

            # Send push notification now that the offer is live
            tokens = list(ExpoPushToken.objects.values_list('token', flat=True))
            if tokens:
                notif_title = f"🛍️ New Offer: {offer.title}"
                notif_body  = offer.description or "Check out the latest offer now!"
                _, dead_tokens = send_expo_push_notification(
                    tokens,
                    notif_title,
                    notif_body,
                    {
                        'type':            'new_offer',
                        'offer_master_id': str(offer.id),
                    }
                )
                if dead_tokens:
                    ExpoPushToken.objects.filter(token__in=dead_tokens).delete()

                logger.info(
                    "[Scheduler] Push sent to %d device(s) for offer '%s'.",
                    len(tokens) - len(dead_tokens), offer.title,
                )

        except Exception as exc:
            logger.exception(
                "[Scheduler] Failed to activate OfferMaster id=%s: %s", offer.id, exc
            )


def _cleanup_old_notifications():
    """
    Called once every 24 hours by APScheduler.
    Permanently deletes CommonNotification records that:
      - status == 'sent'
      - sent_at is older than 24 hours
    Keeps the DB clean without any manual intervention.
    """
    from datetime import timedelta
    from .models import CommonNotification

    cutoff  = timezone.now() - timedelta(hours=24)
    deleted, _ = CommonNotification.objects.filter(
        status='sent',
        sent_at__lt=cutoff,
    ).delete()

    if deleted:
        logger.info("[Scheduler] Cleaned up %d sent notification(s) older than 24 hours.", deleted)


def start():
    """
    Start the APScheduler BackgroundScheduler.
    Safe to call multiple times — won't start a second scheduler.
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning(
            "[Scheduler] apscheduler is not installed. "
            "Run: pip install apscheduler\n"
            "Scheduled notifications will NOT fire automatically."
        )
        return

    scheduler = BackgroundScheduler(timezone=str(timezone.get_current_timezone()))
    scheduler.add_job(
        _fire_due_notifications,
        trigger=IntervalTrigger(seconds=60),
        id='fire_due_notifications',
        name='Fire due common notifications',
        replace_existing=True,
    )
    scheduler.add_job(
        _activate_scheduled_offers,
        trigger=IntervalTrigger(seconds=60),
        id='activate_scheduled_offers',
        name='Activate scheduled OfferMasters and send push notifications',
        replace_existing=True,
    )
    scheduler.add_job(
        _cleanup_old_notifications,
        trigger=IntervalTrigger(hours=24),
        id='cleanup_old_notifications',
        name='Delete sent notifications older than 24 hours',
        replace_existing=True,
    )
    scheduler.start()
    logger.info("[Scheduler] APScheduler started — firing due notifications every 60s, cleanup every 24h.")