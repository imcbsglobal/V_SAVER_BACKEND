import uuid
import qrcode
from io import BytesIO
from django.core.files import File
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings

# ---------- User ----------
class User(AbstractUser):
    USER_TYPE_CHOICES = (
        ('admin', 'Admin'),
        ('user', 'Business Owner'),
    )
    STATUS_CHOICES = [
        ('Active', 'Active'),
        ('Disable', 'Disable'),
    ]
    user_type = models.CharField(max_length=20, choices=USER_TYPE_CHOICES, default='user')
    phone_number = models.CharField(max_length=15, blank=True, null=True, unique=True)
    business_name = models.CharField(max_length=255, blank=True, null=True, default='')
    shop_name = models.CharField(max_length=255, blank=True, null=True, default='')
    location = models.CharField(max_length=255, blank=True, null=True, default='')
    shop_logo = models.ImageField(upload_to='shop_logos/', blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Active')
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    no_days = models.IntegerField(default=0)
    validity_start = models.DateField(blank=True, null=True)
    validity_end = models.DateField(blank=True, null=True)
    client_id = models.CharField(max_length=100, blank=True, null=True, default='')
    created_date = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.is_superuser:
            self.user_type = 'admin'
            self.is_staff = True
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.username} ({self.user_type})"


# ---------- Category ----------
class Category(models.Model):
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True, null=True, default='')
    image = models.ImageField(upload_to="categories/", null=True, blank=True)

    def __str__(self):
        return self.name


# ---------- Product ----------
class Product(models.Model):
    TEMPLATE_CHOICES = [
        ('template1', 'Template 1'),
        ('template2', 'Template 2'),
        ('template3', 'Template 3'),
        ('template4', 'Template 4'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey('offer_app.User', on_delete=models.CASCADE)
    product_name = models.CharField(max_length=255)
    category = models.CharField(max_length=255, blank=True, null=True, default='')
    brand = models.CharField(max_length=255, blank=True, null=True, default='')
    original_price = models.DecimalField(max_digits=10, decimal_places=2)
    offer_price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    image = models.ImageField(upload_to='product_images/', blank=True, null=True)
    qr_code = models.ImageField(upload_to='qr_codes/', blank=True, null=True)
    offer_link = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    valid_until = models.DateTimeField(blank=True, null=True)
    template_type = models.CharField(max_length=20, choices=TEMPLATE_CHOICES, default='template1')
    is_active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if self.original_price and self.offer_price:
            try:
                discount = ((self.original_price - self.offer_price) / self.original_price) * 100
                self.discount_percentage = round(discount, 2)
            except Exception:
                self.discount_percentage = 0

        if not self.offer_link:
            self.offer_link = f"{getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000')}/api/product-offer/{self.id}/"

        super().save(*args, **kwargs)

        if not self.qr_code:
            try:
                self.generate_qr_code()
            except Exception:
                pass

    def generate_qr_code(self):
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(self.offer_link)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        qr_img.save(buffer, format='PNG')
        buffer.seek(0)
        self.qr_code.save(f'qr_code_{self.id}.png', File(buffer), save=False)
        super().save(update_fields=['qr_code'])

    def __str__(self):
        return self.product_name


# ---------- Offer ----------
class Offer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE, null=True, blank=True)
    products = models.ManyToManyField(Product, related_name="offers")
    template_type = models.CharField(max_length=50, default='template1')
    title = models.CharField(max_length=255, blank=True, default='')
    offer_link = models.CharField(max_length=500, blank=True)
    qr_code = models.ImageField(upload_to='offer_qr/', blank=True, null=True)
    is_public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if not self.offer_link:
            site = getattr(settings, 'SITE_URL', 'http://127.0.0.1:3000')
            self.offer_link = f"{site}/offer/{self.id}"
            super().save(update_fields=['offer_link'])

        if not self.qr_code:
            self.generate_qr()

    def generate_qr(self):
        try:
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=8,
                border=4
            )
            qr.add_data(self.offer_link)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white")
            buffer = BytesIO()
            qr_img.save(buffer, format='PNG')
            buffer.seek(0)
            self.qr_code.save(f'offer_qr_{self.id}.png', File(buffer), save=False)
            super().save(update_fields=['qr_code'])
        except Exception as e:
            print("QR generation error:", e)

    def __str__(self):
        return f"Offer {self.id} - {self.title or self.template_type}"


# ---------- BranchMaster ----------
class BranchMaster(models.Model):
    """
    Branch Master - Each branch belongs to a user/shop owner.
    Admin can see and manage ALL branches from ALL users.
    A QR code is auto-generated per branch pointing to the public branch offers page.
    Customers scan this QR → land on the branch offers page → see all active offers.
    Admin only needs to print the QR once; offers update automatically.
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='branches')
    branch_name = models.CharField(max_length=255)
    branch_code = models.CharField(max_length=50, unique=True)
    location = models.CharField(max_length=255)
    address = models.TextField(blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    pincode = models.CharField(max_length=20, blank=True, null=True)
    country = models.CharField(max_length=100, default='India')
    contact_number = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    manager_name = models.CharField(max_length=255, blank=True, null=True)
    manager_phone = models.CharField(max_length=20, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    branch_image = models.ImageField(upload_to='branch_images/', blank=True, null=True)
    qr_code = models.ImageField(upload_to='branch_qr/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Branch Master'
        verbose_name_plural = 'Branch Masters'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.qr_code:
            try:
                self.generate_qr()
            except Exception as e:
                print(f"Branch QR generation error: {e}")

    def get_public_url(self):
        site = getattr(settings, 'FRONTEND_URL', 'http://192.168.1.45:5173')
        return f"{site}/branch/{self.id}/offers"

    def generate_qr(self):
        url = self.get_public_url()
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        qr_img.save(buffer, format='PNG')
        buffer.seek(0)
        self.qr_code.save(f'branch_qr_{self.id}.png', File(buffer), save=False)
        BranchMaster.objects.filter(pk=self.pk).update(qr_code=self.qr_code.name)

    def __str__(self):
        return f"{self.branch_name} ({self.branch_code}) - {self.user.shop_name}"


# ---------- OfferMaster ----------
class OfferMaster(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('scheduled', 'Scheduled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='offer_masters')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    valid_from = models.DateField()
    valid_to = models.DateField()
    offer_start_time = models.TimeField(blank=True, null=True, help_text="Daily start time for hourly offers (e.g. 15:00)")
    offer_end_time   = models.TimeField(blank=True, null=True, help_text="Daily end time for hourly offers (e.g. 17:00)")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    branches = models.ManyToManyField(
        BranchMaster,
        related_name='offers',
        blank=True,
        help_text="Branches where this offer is available"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Offer Master'
        verbose_name_plural = 'Offer Masters'

    def __str__(self):
        return f"{self.title} - {self.user.username}"


# ---------- OfferMasterMedia ----------
class OfferMasterMedia(models.Model):
    """
    Stores multiple media files (images/PDFs) for each OfferMaster.
    """
    MEDIA_TYPE_CHOICES = [
        ('image', 'Image'),
        ('pdf', 'PDF'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    offer_master = models.ForeignKey(
        OfferMaster,
        on_delete=models.CASCADE,
        related_name='media_files'
    )
    file = models.FileField(upload_to='offer_master_media/')
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES)
    order = models.IntegerField(default=0)
    caption = models.CharField(max_length=255, blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'uploaded_at']
        verbose_name = 'Offer Master Media'
        verbose_name_plural = 'Offer Master Media Files'

    def save(self, *args, **kwargs):
        if not self.media_type and self.file:
            file_extension = self.file.name.split('.')[-1].lower()
            if file_extension == 'pdf':
                self.media_type = 'pdf'
            elif file_extension in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                self.media_type = 'image'
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.media_type.upper()} for {self.offer_master.title}"


# ---------- Sync Models (External DB Sync) ----------

class AccMaster(models.Model):
    code        = models.CharField(max_length=30)
    name        = models.CharField(max_length=250)
    place       = models.CharField(max_length=60,  null=True, blank=True)
    exregnodate = models.CharField(max_length=30,  null=True, blank=True)
    super_code  = models.CharField(max_length=5,   null=True, blank=True)
    phone2      = models.CharField(max_length=60,  null=True, blank=True)
    client_id   = models.CharField(max_length=50,  db_index=True)
    synced_at   = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'acc_master_sync'
        ordering        = ['code']
        unique_together = [('code', 'client_id')]

    def __str__(self):
        return f"{self.code} - {self.name} [{self.client_id}]"


class Misel(models.Model):
    firm_name = models.CharField(max_length=150, null=True, blank=True)
    address1  = models.CharField(max_length=50,  null=True, blank=True)
    client_id = models.CharField(max_length=50,  db_index=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'misel_sync'
        unique_together = [('firm_name', 'client_id')]

    def __str__(self):
        return f"{self.firm_name} [{self.client_id}]"


class AccInvMast(models.Model):
    slno       = models.BigIntegerField()
    invdate    = models.DateField(null=True, blank=True)
    customerid = models.CharField(max_length=30, null=True, blank=True)
    nettotal   = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    client_id  = models.CharField(max_length=50,  db_index=True)
    synced_at  = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'acc_invmast_sync'
        ordering        = ['-invdate', '-slno']
        unique_together = [('slno', 'client_id')]

    def __str__(self):
        return f"Invoice {self.slno} | {self.customerid} | {self.client_id}"


# ---------- Expo Push Token ----------
class ExpoPushToken(models.Model):
    user              = models.ForeignKey(User, on_delete=models.CASCADE, related_name='push_tokens')
    token             = models.CharField(max_length=200, unique=True)
    fcm_token         = models.CharField(max_length=512, blank=True, null=True,
                                         help_text='FCM token — used only for CommonNotification (Android)')
    apns_device_token = models.CharField(max_length=200, blank=True, null=True,
                                         help_text='Raw APNs device token — used for iOS image notifications')
    device_type       = models.CharField(max_length=20, blank=True, null=True)
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} - {self.device_type} - {self.token[:20]}"


# ---------- PDF Invoice ----------
class PDFInvoice(models.Model):
    """
    Stores PDF invoices uploaded by users.
    The actual file lives in Cloudflare R2; only the URL + metadata are saved here.
    """
    id                = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user              = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pdf_invoices')
    title             = models.CharField(max_length=255, blank=True, null=True,
                                         help_text="Optional label for this PDF")
    original_filename = models.CharField(max_length=255, blank=True, null=True)
    file_url          = models.URLField(max_length=1000,
                                        help_text="Public R2 URL of the uploaded PDF")
    file_key          = models.CharField(max_length=500,
                                         help_text="R2 object key (path inside bucket)")
    file_size         = models.PositiveBigIntegerField(null=True, blank=True,
                                                        help_text="File size in bytes")
    uploaded_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering        = ['-uploaded_at']
        verbose_name    = 'PDF Invoice'
        verbose_name_plural = 'PDF Invoices'

    def __str__(self):
        return f"{self.title or self.original_filename} - {self.user.username}"


# ---------- File Deletion Signals ----------
from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver

@receiver(post_delete)
def auto_delete_file_on_delete(sender, instance, **kwargs):
    """
    Deletes files from storage when corresponding model object is deleted.
    """
    if not hasattr(sender, '_meta') or sender._meta.app_label != 'offer_app':
        return
    for field in sender._meta.get_fields():
        if isinstance(field, models.FileField):
            file = getattr(instance, field.name, None)
            if file and file.name:
                try:
                    file.delete(save=False)
                except Exception:
                    pass

@receiver(pre_save)
def auto_delete_file_on_change(sender, instance, **kwargs):
    """
    Deletes old file from storage when corresponding file field is updated.
    """
    if not hasattr(sender, '_meta') or sender._meta.app_label != 'offer_app':
        return
    if not instance.pk:
        return

    try:
        old_instance = sender.objects.get(pk=instance.pk)
    except sender.DoesNotExist:
        return

    for field in sender._meta.get_fields():
        if isinstance(field, models.FileField):
            old_file = getattr(old_instance, field.name, None)
            new_file = getattr(instance, field.name, None)
            if old_file and old_file.name and old_file != new_file:
                try:
                    old_file.delete(save=False)
                except Exception:
                    pass


# ---------- Common Notification ----------
class CommonNotification(models.Model):
    """
    Admin-created reusable notifications like 'Happy Vishu', 'Good Morning' etc.
    Admin can schedule or instantly send these to all registered push token users.
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('scheduled', 'Scheduled'),
    ]
    TARGET_CHOICES = [
        ('all', 'All Users'),
        ('active', 'Active Users Only'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255, help_text="Notification title e.g. 'Happy Vishu 🎉'")
    body = models.TextField(help_text="Notification body message")
    image = models.ImageField(
        upload_to='notification_images/',
        blank=True, null=True,
        help_text="Upload an image file (JPG/PNG/WebP, max 5 MB)"
    )
    image_url    = models.URLField(blank=True, null=True,
                                   help_text="Optional image URL to show in notification (used when no file is uploaded)")
    target       = models.CharField(max_length=20, choices=TARGET_CHOICES, default='all')
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    scheduled_at = models.DateTimeField(blank=True, null=True,
                                        help_text="Leave blank to send immediately")
    sent_at      = models.DateTimeField(blank=True, null=True)
    sent_count   = models.IntegerField(default=0, help_text="Number of tokens notified")
    created_by   = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_notifications'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Common Notification'
        verbose_name_plural = 'Common Notifications'

    def __str__(self):
        return f"{self.title} [{self.status}]"