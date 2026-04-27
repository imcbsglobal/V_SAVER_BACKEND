# serializers.py
from rest_framework import serializers
from django.contrib.auth import authenticate
from django.utils import timezone
from .models import User, Category, Product, Offer, OfferMaster, OfferMasterMedia, BranchMaster, AccMaster, Misel, AccInvMast
from .models import CommonNotification, PDFInvoice

# ---------------- USER SERIALIZERS ----------------

class UserRegistrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("username", "email", "password", "shop_name")
        extra_kwargs = {"password": {"write_only": True}}

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class UserPublicSerializer(serializers.ModelSerializer):
    """
    Safe user payload for frontend. Do NOT expose __all__ on login.
    """
    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "user_type",
            "status",
            "shop_name",
            "business_name",
            "phone_number",
            "location",
            "shop_logo",
            "amount",
            "no_days",
            "validity_start",
            "validity_end",
            "created_date",
            "date_joined",
            "client_id",
        )


class UserSimpleSerializer(serializers.ModelSerializer):
    """
    Simple user serializer for dropdowns/listings
    """
    class Meta:
        model = User
        fields = ('id', 'username', 'shop_name', 'email')


class UserSerializer(serializers.ModelSerializer):
    """
    For admin/internal use — excludes sensitive fields like password and is_superuser.
    ✅ FIX: Changed from fields = "__all__" to explicit safe fields only.
    Previously this was exposing the password hash and superuser flags to the frontend.
    """
    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "user_type",
            "status",
            "shop_name",
            "business_name",
            "phone_number",
            "location",
            "shop_logo",
            "amount",
            "no_days",
            "validity_start",
            "validity_end",
            "is_active",
            "is_staff",
            "created_date",
            "date_joined",
        )
        extra_kwargs = {
            "password": {"write_only": True},  # safety net — never expose password hash
        }


# ---------------- LOGIN SERIALIZER ----------------

class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)
    username = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField()
    client_id = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        email = (data.get("email") or "").strip()
        username = (data.get("username") or "").strip()
        password = data.get("password")
        client_id = (data.get("client_id") or "").strip()

        if not password:
            raise serializers.ValidationError({"error": "Password is required."})

        if not email and not username:
            raise serializers.ValidationError({"error": "Provide email or username and password."})

        # Email login path
        if email:
            qs = User.objects.filter(email__iexact=email)

            if not qs.exists():
                raise serializers.ValidationError({"error": "Invalid email or user not found."})

            if qs.count() > 1:
                raise serializers.ValidationError({"error": "Multiple accounts use this email. Contact admin."})

            user_obj = qs.first()
            user = authenticate(username=user_obj.username, password=password)
            if user is None:
                raise serializers.ValidationError({"error": "Incorrect password."})
        else:
            # Username login path
            user = authenticate(username=username, password=password)
            if user is None:
                raise serializers.ValidationError({"error": "Invalid username or password."})

        # client_id is handled in the view — not validated here
        data["user"] = user
        return data


# ---------------- CATEGORY SERIALIZER ----------------

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = "__all__"


# ---------------- PRODUCT SERIALIZERS ----------------

class ProductCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = (
            "product_name",
            "brand",
            "category",
            "original_price",
            "offer_price",
            "valid_until",
            "template_type",
            "image",
        )


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = "__all__"


# ---------------- OFFER SERIALIZERS ----------------

class OfferSerializer(serializers.ModelSerializer):
    class Meta:
        model = Offer
        fields = "__all__"


class OfferTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = "__all__"


class OfferCreateSerializer(serializers.Serializer):
    category_id = serializers.IntegerField(required=False, allow_null=True)
    template_type = serializers.CharField()
    product_ids = serializers.ListField(child=serializers.UUIDField())

    def validate(self, data):
        if not data.get("product_ids"):
            raise serializers.ValidationError({"error": "At least one product required"})
        return data

    def create(self, validated_data):
        user = self.context["request"].user

        category = None
        if validated_data.get("category_id"):
            category = Category.objects.filter(id=validated_data["category_id"]).first()

        offer = Offer.objects.create(
            user=user,
            category=category,
            template_type=validated_data["template_type"],
        )

        products = Product.objects.filter(id__in=validated_data["product_ids"], user=user)
        offer.products.set(products)
        offer.save()
        return offer


class OfferPublicSerializer(serializers.ModelSerializer):
    products = ProductSerializer(many=True)
    category = CategorySerializer()
    qr_url = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = (
            "id",
            "title",
            "template_type",
            "category",
            "products",
            "offer_link",
            "qr_url",
            "created_at",
            "is_public",
        )

    def get_qr_url(self, obj):
        return obj.qr_code.url if obj.qr_code else None


# ---------------- BRANCH MASTER SERIALIZERS ----------------

class BranchMasterSerializer(serializers.ModelSerializer):
    """
    Serializer for reading/listing BranchMaster
    Includes user/shop owner information
    """
    branch_image_url = serializers.SerializerMethodField()
    qr_code_url = serializers.SerializerMethodField()
    branch_offers_url = serializers.SerializerMethodField()
    user_info = serializers.SerializerMethodField()

    class Meta:
        model = BranchMaster
        fields = [
            'id',
            'user',
            'user_info',
            'branch_name',
            'branch_code',
            'location',
            'address',
            'city',
            'state',
            'pincode',
            'country',
            'contact_number',
            'email',
            'manager_name',
            'manager_phone',
            'status',
            'branch_image',
            'branch_image_url',
            'qr_code_url',
            'branch_offers_url',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_branch_image_url(self, obj):
        if obj.branch_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.branch_image.url)
            return obj.branch_image.url
        return None

    def get_qr_code_url(self, obj):
        if obj.qr_code:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.qr_code.url)
            return obj.qr_code.url
        return None

    def get_branch_offers_url(self, obj):
        return obj.get_public_url()

    def get_user_info(self, obj):
        return {
            'id': obj.user.id,
            'username': obj.user.username,
            'shop_name': obj.user.shop_name or obj.user.username,
            'email': obj.user.email
        }


class BranchMasterCreateUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating/updating BranchMaster
    Admin specifies which user/shop the branch belongs to
    """
    branch_image = serializers.ImageField(required=False, allow_null=True)

    class Meta:
        model = BranchMaster
        fields = [
            'user',
            'branch_name',
            'branch_code',
            'location',
            'address',
            'city',
            'state',
            'pincode',
            'country',
            'contact_number',
            'email',
            'manager_name',
            'manager_phone',
            'status',
            'branch_image'
        ]

    def validate_branch_code(self, value):
        instance = self.instance
        if instance:
            if BranchMaster.objects.filter(branch_code=value).exclude(id=instance.id).exists():
                raise serializers.ValidationError('A branch with this code already exists.')
        else:
            if BranchMaster.objects.filter(branch_code=value).exists():
                raise serializers.ValidationError('A branch with this code already exists.')
        return value

    def validate_branch_image(self, value):
        if value:
            max_size = 5 * 1024 * 1024  # 5MB
            if value.size > max_size:
                raise serializers.ValidationError('Image file is too large. Maximum size is 5MB.')
            file_extension = value.name.split('.')[-1].lower()
            allowed_extensions = ['jpg', 'jpeg', 'png', 'webp']
            if file_extension not in allowed_extensions:
                raise serializers.ValidationError(
                    f'File type .{file_extension} is not allowed. Allowed types: {", ".join(allowed_extensions)}'
                )
        return value


# ---------------- OFFER MASTER MEDIA SERIALIZER ----------------

class OfferMasterMediaSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = OfferMasterMedia
        fields = [
            'id',
            'file',
            'file_url',
            'media_type',
            'order',
            'caption',
            'uploaded_at'
        ]
        read_only_fields = ['id', 'uploaded_at', 'media_type']

    def get_file_url(self, obj):
        if obj.file:
            url = obj.file.url
            if url.startswith('http://') or url.startswith('https://'):
                return url
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(url)
            return url

# ---------------- OFFER MASTER SERIALIZERS ----------------

class OfferMasterSerializer(serializers.ModelSerializer):
    """
    Serializer for reading/listing OfferMaster with all media files and branches.
    computed_status reflects real-time status based on date + hourly window (IST).
    """
    media_files     = OfferMasterMediaSerializer(many=True, read_only=True)
    media_count     = serializers.SerializerMethodField()
    branches        = BranchMasterSerializer(many=True, read_only=True)
    branch_count    = serializers.SerializerMethodField()
    computed_status = serializers.SerializerMethodField()

    class Meta:
        model = OfferMaster
        fields = [
            'id',
            'title',
            'description',
            'valid_from',
            'valid_to',
            'offer_start_time',
            'offer_end_time',
            'status',
            'computed_status',
            'media_files',
            'media_count',
            'branches',
            'branch_count',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_media_count(self, obj):
        return obj.media_files.count()

    def get_branch_count(self, obj):
        return obj.branches.count()

    def get_computed_status(self, obj):
        """
        Real-time status in IST:
          inactive  -> admin manually disabled OR date fully passed
          scheduled -> date not started yet, OR hourly window hasnt started today
          active    -> within valid date AND within hourly window (if set)
          expired   -> hourly window ended today (same-day offer whose window passed)
        """
        if obj.status == 'inactive':
            return 'inactive'

        now_ist  = timezone.localtime()
        today    = now_ist.date()
        now_time = now_ist.time().replace(second=0, microsecond=0)

        if obj.valid_from > today:
            return 'scheduled'
        if obj.valid_to < today:
            return 'inactive'

        if obj.offer_start_time and obj.offer_end_time:
            if now_time < obj.offer_start_time:
                return 'scheduled'
            elif now_time > obj.offer_end_time:
                return 'expired'
            else:
                return 'active'

        return 'active' 


class OfferMasterCreateUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating/updating OfferMaster with multiple file uploads and branch assignment
    """
    files = serializers.ListField(
        child=serializers.FileField(),
        write_only=True,
        required=False,
        help_text="List of image/PDF files to upload"
    )
    captions = serializers.ListField(
        child=serializers.CharField(allow_blank=True),
        write_only=True,
        required=False,
        help_text="Optional captions for each file (must match files count)"
    )
    branch_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        help_text="List of branch IDs to assign this offer to"
    )

    class Meta:
        model = OfferMaster
        fields = [
            'title',
            'description',
            'valid_from',
            'valid_to',
            'offer_start_time',
            'offer_end_time',
            'status',
            'files',
            'captions',
            'branch_ids'
        ]

    def validate(self, data):
        if data.get('valid_from') and data.get('valid_to'):
            if data['valid_to'] < data['valid_from']:
                raise serializers.ValidationError({
                    'valid_to': 'End date must be on or after start date.'
                })

        # Hourly time validation
        start_time = data.get('offer_start_time')
        end_time   = data.get('offer_end_time')
        if start_time and end_time:
            if end_time <= start_time:
                raise serializers.ValidationError({
                    'offer_end_time': 'Offer end time must be after start time.'
                })
        elif start_time and not end_time:
            raise serializers.ValidationError({
                'offer_end_time': 'Please provide an end time when start time is set.'
            })
        elif end_time and not start_time:
            raise serializers.ValidationError({
                'offer_start_time': 'Please provide a start time when end time is set.'
            })

        files = data.get('files', [])
        if files:
            for file in files:
                file_extension = file.name.split('.')[-1].lower()
                allowed_extensions = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'pdf']
                if file_extension not in allowed_extensions:
                    raise serializers.ValidationError({
                        'files': f'File type .{file_extension} is not allowed. Allowed types: {", ".join(allowed_extensions)}'
                    })
                max_size = 10 * 1024 * 1024  # 10MB
                if file.size > max_size:
                    raise serializers.ValidationError({
                        'files': f'File {file.name} is too large. Maximum size is 10MB.'
                    })

        branch_ids = data.get('branch_ids', [])
        if branch_ids:
            existing_branches = BranchMaster.objects.filter(id__in=branch_ids)
            if existing_branches.count() != len(branch_ids):
                raise serializers.ValidationError({
                    'branch_ids': 'Some branch IDs are invalid.'
                })

        return data

    def create(self, validated_data):
        files = validated_data.pop('files', [])
        captions = validated_data.pop('captions', [])
        branch_ids = validated_data.pop('branch_ids', [])

        offer_master = OfferMaster.objects.create(**validated_data)

        if branch_ids:
            branches = BranchMaster.objects.filter(id__in=branch_ids)
            offer_master.branches.set(branches)

        for index, file in enumerate(files):
            caption = captions[index] if index < len(captions) else ''
            OfferMasterMedia.objects.create(
                offer_master=offer_master,
                file=file,
                order=index,
                caption=caption
            )

        return offer_master

    def update(self, instance, validated_data):
        files = validated_data.pop('files', None)
        captions = validated_data.pop('captions', [])
        branch_ids = validated_data.pop('branch_ids', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if branch_ids is not None:
            branches = BranchMaster.objects.filter(id__in=branch_ids)
            instance.branches.set(branches)

        if files:
            from django.db.models import Max
            current_max_order = instance.media_files.aggregate(Max('order'))['order__max']
            if current_max_order is None:
                current_max_order = -1
            for index, file in enumerate(files):
                caption = captions[index] if index < len(captions) else ''
                OfferMasterMedia.objects.create(
                    offer_master=instance,
                    file=file,
                    order=current_max_order + index + 1,
                    caption=caption
                )

        return instance


# ---------------- BRANCH WITH OFFERS SERIALIZER ----------------

class BranchWithOffersSerializer(serializers.ModelSerializer):
    """
    Serializer for Branch with its assigned offers.
    Used for public discovery (QR scan landing page) — shows branch info + active, non-expired offers only.
    """
    active_offers = serializers.SerializerMethodField()
    offers_count = serializers.SerializerMethodField()
    branch_image_url = serializers.SerializerMethodField()
    shop_name = serializers.SerializerMethodField()
    user_id = serializers.SerializerMethodField()

    class Meta:
        model = BranchMaster
        fields = [
            'id',
            'user_id',
            'shop_name',
            'branch_name',
            'branch_code',
            'location',
            'address',
            'city',
            'state',
            'contact_number',
            'email',
            'status',
            'branch_image',
            'branch_image_url',
            'active_offers',
            'offers_count'
        ]

    def get_active_offers(self, obj):
        """
        Return offers that are:
          - Date-valid: valid_from <= today (IST) <= valid_to
          - Not manually disabled: status != 'inactive'
          - Within hourly window if set (compared in IST via Django TIME_ZONE setting)
        """
        now_ist  = timezone.localtime()          # IST because TIME_ZONE = 'Asia/Kolkata'
        today    = now_ist.date()
        now_time = now_ist.time().replace(second=0, microsecond=0)

        active_offers = obj.offers.filter(
            valid_from__lte=today,
            valid_to__gte=today,
        ).exclude(status='inactive').prefetch_related('media_files')

        result = []
        for offer in active_offers:
            if offer.offer_start_time and offer.offer_end_time:
                if not (offer.offer_start_time <= now_time <= offer.offer_end_time):
                    continue
            result.append(offer)

        return OfferMasterSerializer(result, many=True, context=self.context).data

    def get_offers_count(self, obj):
        """Return count of currently visible offers (date + IST hourly window)."""
        now_ist  = timezone.localtime()
        today    = now_ist.date()
        now_time = now_ist.time().replace(second=0, microsecond=0)

        offers = obj.offers.filter(
            valid_from__lte=today,
            valid_to__gte=today,
        ).exclude(status='inactive')

        count = 0
        for offer in offers:
            if offer.offer_start_time and offer.offer_end_time:
                if not (offer.offer_start_time <= now_time <= offer.offer_end_time):
                    continue
            count += 1
        return count

    def get_branch_image_url(self, obj):
        if obj.branch_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.branch_image.url)
            return obj.branch_image.url
        return None

    def get_shop_name(self, obj):
        return obj.user.shop_name or obj.user.business_name or obj.user.username

    def get_user_id(self, obj):
        return obj.user.id

# ================================================================
# ---------------- SYNC DATA SERIALIZERS ----------------
# AccMaster, Misel, AccInvMast — read-only, admin use only.
# client_id is stored as data but NOT used as a filter anywhere.
# ================================================================

class AccMasterSerializer(serializers.ModelSerializer):
    """Customers / Debtors synced from the accounting system."""
    class Meta:
        model  = AccMaster
        fields = [
            'id',
            'code',
            'name',
            'place',
            'phone2',
            'exregnodate',
            'super_code',
            'client_id',
            'synced_at',
        ]
        read_only_fields = fields


class MiselSerializer(serializers.ModelSerializer):
    """Shop / firm records synced from the Misel system."""
    class Meta:
        model  = Misel
        fields = [
            'id',
            'firm_name',
            'address1',
            'client_id',
            'synced_at',
        ]
        read_only_fields = fields


class AccInvMastSerializer(serializers.ModelSerializer):
    """Invoice records synced from the accounting system."""
    class Meta:
        model  = AccInvMast
        fields = [
            'id',
            'slno',
            'invdate',
            'customerid',
            'nettotal',
            'client_id',
            'synced_at',
        ]
        read_only_fields = fields
        
class CommonNotificationSerializer(serializers.ModelSerializer):
    """
    Handles both image-file upload (multipart/form-data) and plain image_url (JSON).
    The frontend reads `resolved_image_url` — it returns whichever source is set.
    """
    created_by_name = serializers.SerializerMethodField()
    # Write-only upload field
    image = serializers.ImageField(write_only=True, required=False, allow_null=True)
    # Read-only resolved URL (file or URL string, whichever is set)
    resolved_image_url = serializers.SerializerMethodField()

    class Meta:
        model = CommonNotification
        fields = [
            'id', 'title', 'body',
            'image',               # write-only file upload
            'image_url',           # write-only URL string
            'resolved_image_url',  # read-only: file URL or image_url
            'target',
            'status', 'scheduled_at', 'sent_at', 'sent_count',
            'created_by', 'created_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'status', 'sent_at', 'sent_count', 'created_by',
            'created_at', 'updated_at', 'resolved_image_url',
        ]

    def get_created_by_name(self, obj):
        return obj.created_by.username if obj.created_by else None

    def get_resolved_image_url(self, obj):
        """
        Return absolute URL of the notification image.
        Tries the uploaded file first (obj.image), falls back to obj.image_url.
        Wrapped in try/except so it doesn't crash if the migration hasn't been run yet.
        """
        request = self.context.get('request')
        try:
            if obj.image and obj.image.name:
                return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        except Exception:
            pass
        return obj.image_url or None

    def validate(self, data):
        image_file = data.get('image')
        if image_file:
            if image_file.size > 5 * 1024 * 1024:
                raise serializers.ValidationError({'image': 'Image must be smaller than 5 MB.'})
            allowed = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
            if image_file.content_type not in allowed:
                raise serializers.ValidationError(
                    {'image': 'Unsupported type. Use JPG, PNG, GIF, or WebP.'}
                )
        return data

    def create(self, validated_data):
        image_file = validated_data.pop('image', None)
        instance = super().create(validated_data)
        if image_file:
            instance.image = image_file
            instance.image_url = None  # file takes priority over URL
            instance.save(update_fields=['image', 'image_url'])
        return instance

    def update(self, instance, validated_data):
        image_file = validated_data.pop('image', None)
        instance = super().update(instance, validated_data)
        if image_file:
            instance.image = image_file
            instance.image_url = None
            instance.save(update_fields=['image', 'image_url'])
        return instance


# ---------------- PDF INVOICE SERIALIZER ----------------

class PDFInvoiceSerializer(serializers.ModelSerializer):
    """
    Serializer for PDFInvoice.
    - On POST (upload): accepts `file` (the actual PDF) + optional `title`.
      The view handles the R2 upload and fills file_url / file_key / file_size.
    - On GET (list): returns all stored metadata including the R2 public URL.
    - `file` is write-only — it is only used during upload, never returned.
    - `uploaded_by` is a read-only convenience field showing the uploader's username.
    """
    file = serializers.FileField(write_only=True, required=True)
    uploaded_by = serializers.SerializerMethodField()

    class Meta:
        model = PDFInvoice
        fields = [
            'id',
            'title',
            'file',             # write-only: PDF upload field
            'original_filename',
            'file_url',         # R2 public URL (read-only, set by view)
            'file_key',         # R2 object key (read-only, set by view)
            'file_size',        # bytes (read-only, set by view)
            'uploaded_by',      # read-only: uploader username
            'uploaded_at',
        ]
        read_only_fields = [
            'id',
            'original_filename',
            'file_url',
            'file_key',
            'file_size',
            'uploaded_at',
        ]

    def get_uploaded_by(self, obj):
        return obj.user.username if obj.user else None

    def validate_file(self, value):
        # Only allow PDF files
        ext = value.name.split('.')[-1].lower()
        if ext != 'pdf':
            raise serializers.ValidationError('Only PDF files are allowed.')
        # Max 20 MB
        max_size = 20 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError('PDF file is too large. Maximum size is 20 MB.')
        return value