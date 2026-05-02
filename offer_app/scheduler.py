"""
offer_app/scheduler.py
─────────────────────────────────────────────────────────────────────────────
Background scheduler that fires CommonNotification records whose
scheduled_at time has arrived.

Uses APScheduler's BackgroundScheduler (runs in a daemon thread inside the
Django process — no separate worker or Redis needed).

Started once from AppConfig.ready() so it survives server restarts.

Notification routing:
  • CommonNotification WITHOUT image → Expo push (works for all devices)
  • CommonNotification WITH image    → FCM V1 (Android) + APNs direct (iOS)
  • OfferMaster push notifications   → Expo push (unchanged)
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
    and sends it:
      - No image → Expo push (reaches all devices)
      - With image → FCM V1 (reaches devices with fcm_token)
    """
    from .models import CommonNotification, ExpoPushToken
    from .fcm_notifications import send_fcm_notification_with_image
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

            # Resolve image URL
            image_url = None
            if notif.image:
                try:
                    image_url = notif.image.url
                except Exception:
                    pass
            elif notif.image_url:
                image_url = notif.image_url

            sent_count  = 0
            dead_tokens = []

            if image_url:
                # ── Has image → FCM V1 (Android) ────────────────────────────
                fcm_tokens = list(
                    token_qs.exclude(fcm_token__isnull=True)
                            .exclude(fcm_token='')
                            .values_list('fcm_token', flat=True)
                )
                if fcm_tokens:
                    fcm_sent, fcm_dead = send_fcm_notification_with_image(
                        fcm_tokens, notif.title, notif.body, image_url
                    )
                    sent_count += fcm_sent
                    if fcm_dead:
                        ExpoPushToken.objects.filter(fcm_token__in=fcm_dead).delete()

                # ── Has image → APNs direct (iOS) ────────────────────────────
                from .apns_notifications import send_apns_notification
                apns_tokens = list(
                    token_qs.exclude(apns_device_token__isnull=True)
                            .exclude(apns_device_token='')
                            .values_list('apns_device_token', flat=True)
                )
                if apns_tokens:
                    apns_sent, apns_dead = send_apns_notification(
                        apns_tokens, notif.title, notif.body, image_url
                    )
                    sent_count += apns_sent
                    if apns_dead:
                        ExpoPushToken.objects.filter(apns_device_token__in=apns_dead).update(apns_device_token='')

                logger.info(
                    "[Scheduler] Sent scheduled notification '%s' (id=%s) to %d device(s) via FCM+APNs.",
                    notif.title, notif.id, sent_count,
                )

            else:
                # ── No image → Expo push (reaches all devices) ──────────────
                expo_tokens = list(token_qs.values_list('token', flat=True))
                if expo_tokens:
                    _, dead_tokens = send_expo_push_notification(
                        expo_tokens, notif.title, notif.body, {}
                    )
                    if dead_tokens:
                        ExpoPushToken.objects.filter(token__in=dead_tokens).delete()
                    sent_count = len(expo_tokens) - len(dead_tokens)

                logger.info(
                    "[Scheduler] Sent scheduled notification '%s' (id=%s) to %d device(s) via Expo.",
                    notif.title, notif.id, sent_count,
                )

            notif.status     = 'sent'
            notif.sent_at    = now
            notif.sent_count = sent_count
            notif.save(update_fields=['status', 'sent_at', 'sent_count'])

        except Exception as exc:
            logger.exception(
                "[Scheduler] Failed to send notification id=%s: %s", notif.id, exc
            )


def _build_offer_notification(offer):
    """
    Build push notification title + body for an OfferMaster.

    - Regular offer → standard title/body
    - Hourly offer  → ⏰ flash label in title, time remaining in body
                      e.g. "⏰ Flash Offer: Summer Sale"
                           "Hurry! Valid for next 2h 30m only (ends at 05:00 PM)"
                      If description exists it is prepended:
                           "Big discounts!\n⏰ Ends in 2h 30m (at 05:00 PM)"
    """
    from datetime import datetime, date as dt_date
    from django.utils.timezone import localtime

    notif_title = f"🛍️ New Offer: {offer.title}"
    notif_body  = offer.description or "Check out the latest offer now!"

    if offer.offer_end_time:
        now_ist  = localtime()
        now_time = now_ist.time().replace(second=0, microsecond=0)

        end_dt = datetime.combine(dt_date.today(), offer.offer_end_time)
        now_dt = datetime.combine(dt_date.today(), now_time)
        diff_seconds = (end_dt - now_dt).total_seconds()

        if diff_seconds > 0:
            total_minutes = int(diff_seconds / 60)
            hours   = total_minutes // 60
            minutes = total_minutes % 60

            end_time_str = offer.offer_end_time.strftime("%I:%M %p")

            if hours > 0 and minutes > 0:
                timer_str = f"{hours}h {minutes}m"
            elif hours > 0:
                timer_str = f"{hours}h"
            else:
                timer_str = f"{minutes}m"

            notif_title = f"⏰ Flash Offer: {offer.title}"
            if offer.description:
                notif_body = f"{offer.description}\n⏰ Ends in {timer_str} (at {end_time_str})"
            else:
                notif_body = f"Hurry! Valid for next {timer_str} only (ends at {end_time_str})"

    return notif_title, notif_body


def _activate_scheduled_offers():
    """
    Called every 60 seconds by APScheduler.
    Finds every OfferMaster that:
      - status == 'scheduled'
      - valid_from <= today   (offer window has started)
      - valid_to   >= today   (offer window has not yet expired)
    Marks them 'active' and sends a push notification via Expo push
    (OfferMaster notifications are NOT routed through FCM — unchanged).
    """
    from django.utils.timezone import localdate, localtime
    from .models import OfferMaster, ExpoPushToken
    from .push_notifications import send_expo_push_notification

    today    = localdate()
    now_time = localtime().time().replace(second=0, microsecond=0)

    due_offers = OfferMaster.objects.filter(
        status='scheduled',
        valid_from__lte=today,
        valid_to__gte=today,
    )

    if not due_offers.exists():
        return

    for offer in due_offers:
        try:
            # Skip hourly offers that are outside their time window
            if offer.offer_start_time and offer.offer_end_time:
                if not (offer.offer_start_time <= now_time <= offer.offer_end_time):
                    continue

            # Activate the offer
            offer.status = 'active'
            offer.save(update_fields=['status'])

            logger.info(
                "[Scheduler] OfferMaster '%s' (id=%s) is now active — sending push notification.",
                offer.title, offer.id,
            )

            # Send push notification via Expo with timer info for hourly offers
            tokens = list(ExpoPushToken.objects.values_list('token', flat=True))
            if tokens:
                notif_title, notif_body = _build_offer_notification(offer)
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
                    "[Scheduler] Expo push sent to %d device(s) for offer '%s'.",
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
        name='Fire due common notifications (FCM/Expo)',
        replace_existing=True,
    )
    scheduler.add_job(
        _activate_scheduled_offers,
        trigger=IntervalTrigger(seconds=60),
        id='activate_scheduled_offers',
        name='Activate scheduled OfferMasters and send Expo push notifications',
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