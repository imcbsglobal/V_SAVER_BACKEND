from django.apps import AppConfig


class OfferAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'offer_app'

    def ready(self):
        import os
        if os.environ.get('RUN_MAIN') == 'true' or not os.environ.get('RUN_MAIN'):
            from . import scheduler
            scheduler.start()