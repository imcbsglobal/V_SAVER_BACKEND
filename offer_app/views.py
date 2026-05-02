# views.py
from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework_simplejwt.tokens import RefreshToken
from django.db.models import Q
from django.utils import timezone
from .fcm_notifications import send_fcm_notification_with_image
import secrets
import random
import string
import requests as http_requests
from django.core.cache import cache
from .models import CommonNotification
from .serializers import CommonNotificationSerializer
from .models import User, Category, Product, Offer, OfferMaster, OfferMasterMedia, BranchMaster
from .models import AccMaster, Misel, AccInvMast
from .serializers import (
    UserSerializer,
    UserPublicSerializer,
    CategorySerializer,
    ProductSerializer,
    ProductCreateSerializer,
    OfferCreateSerializer,
    OfferPublicSerializer,
    LoginSerializer,
    UserRegistrationSerializer,
    OfferSerializer,
    OfferTemplateSerializer,
    OfferMasterSerializer,
    OfferMasterCreateUpdateSerializer,
    OfferMasterMediaSerializer,
    BranchMasterSerializer,
    BranchMasterCreateUpdateSerializer,
    UserSimpleSerializer,
    BranchWithOffersSerializer,
    AccMasterSerializer,
    MiselSerializer,
    AccInvMastSerializer,
)

# ------------------ AUTO-EXPIRE OFFERS ------------------

def auto_expire_offers():
    now_ist  = timezone.localtime()
    today    = now_ist.date()
    now_time = now_ist.time().replace(second=0, microsecond=0)

    OfferMaster.objects.filter(valid_to__lt=today).exclude(status='inactive').update(status='inactive')
    OfferMaster.objects.filter(valid_from__gt=today).exclude(status='inactive').update(status='scheduled')

    in_range = OfferMaster.objects.filter(
        valid_from__lte=today,
        valid_to__gte=today,
    ).exclude(status='inactive')

    for offer in in_range:
        if offer.offer_start_time and offer.offer_end_time:
            if now_time < offer.offer_start_time:
                new_status = 'scheduled'
            elif now_time > offer.offer_end_time:
                new_status = 'scheduled'
            else:
                new_status = 'active'
        else:
            new_status = 'active'

        if offer.status != new_status:
            offer.status = new_status
            offer.save(update_fields=['status'])


# ------------------ PERMISSIONS ------------------

class IsAdminUser(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.user_type == "admin"


class IsAdminOrReadOnly(permissions.BasePermission):
    """
    Authenticated users can read (GET, HEAD, OPTIONS).
    Only admin users can write (POST, PUT, PATCH, DELETE).
    """
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:  # GET, HEAD, OPTIONS
            return True
        return request.user.user_type == "admin"


def _block_if_disabled(user):
    if getattr(user, "status", "Active") == "Disable":
        return True
    return False


# ===================== AUTH =====================

@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def admin_login(request):
    client_id = (request.data.get("client_id") or "").strip()
    if not client_id:
        return Response({"error": "Client ID is required."}, status=400)

    client_exists = AccMaster.objects.filter(client_id=client_id).exists()
    if not client_exists:
        return Response({"error": "Invalid Client ID. Please check and try again."}, status=400)

    serializer = LoginSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    user = serializer.validated_data["user"]

    if _block_if_disabled(user):
        return Response({"error": "Account is disabled"}, status=403)

    if user.user_type != "admin":
        return Response({"error": "Admin access only"}, status=403)

    User.objects.filter(pk=user.pk).update(client_id=client_id)
    user.client_id = client_id

    refresh = RefreshToken.for_user(user)
    return Response({
        "access":  str(refresh.access_token),
        "refresh": str(refresh),
        "user":    UserPublicSerializer(user).data
    })


# ─── SMS OTP (IMCBS) ──────────────────────────────────────────────────────────
SMS_API_BASE_URL    = "https://sms.imcbs.com/api/sms/v1.0/send-sms"
SMS_ACCESS_TOKEN    = "0AV9EI6AQ0EFCHQ"
SMS_EXPIRE          = "2092314885"
SMS_AUTH_SIGNATURE  = "bfdcd3efc2874d3529cd0f4d49380e0d"
SMS_ROUTE           = "transactional"
SMS_HEADER          = "VCARMT"
SMS_ENTITY_ID       = "1701177666647932819"
SMS_TEMPLATE_ID     = "1707177668810866932"
# The API uses {#var#} as the OTP placeholder in the template
SMS_MESSAGE_TEMPLATE = "{#var#} is the OTP for VCARE MART Mobile App.Valid for next 5 min. Do not share to anyone.VCARE MART"


def _send_sms_otp(phone_number: str, otp: str) -> tuple:
    """Send OTP via IMCBS SMS gateway. Returns (success: bool, error_msg: str)."""
    # Build message with OTP substituted for {#var#}
    message = SMS_MESSAGE_TEMPLATE.replace("{#var#}", otp)

    params = {
        "accessToken":          SMS_ACCESS_TOKEN,
        "expire":               SMS_EXPIRE,
        "authSignature":        SMS_AUTH_SIGNATURE,
        "route":                SMS_ROUTE,
        "smsHeader":            SMS_HEADER,
        "messageContent":       message,
        "recipients":           f"91{phone_number}",
        "contentType":          "text",
        "entityId":             SMS_ENTITY_ID,
        "templateId":           SMS_TEMPLATE_ID,
        "removeDuplicateNumbers": "1",
        "flashSMS":             "0",
    }

    try:
        res = http_requests.get(SMS_API_BASE_URL, params=params, timeout=10)
        print(f"[SMS OTP] status={res.status_code} | phone=91{phone_number} | response={res.text}")
        if res.status_code == 200:
            return True, ""
        try:
            err_data = res.json()
            err_msg  = err_data.get("message") or err_data.get("error") or res.text
        except Exception:
            err_msg = res.text or f"HTTP {res.status_code}"
        return False, err_msg
    except Exception as e:
        print(f"[SMS OTP] Exception: {e}")
        return False, str(e)


def _find_debtor_by_phone(phone_number):
    record = AccMaster.objects.filter(phone2__endswith=phone_number).first()
    if record:
        return {
            "code":        record.code,
            "name":        record.name,
            "place":       record.place or "",
            "phone2":      record.phone2 or "",
            "exregnodate": record.exregnodate or "0",
            "client_id":   record.client_id,
        }
    return None


def _find_branch_by_client_id(client_id):
    """Look up branch info from the Misel table using client_id."""
    if not client_id:
        return None
    record = Misel.objects.filter(client_id=client_id).first()
    if record:
        return {
            "branch_name":    record.firm_name or "",
            "branch_address": record.address1 or "",
        }
    return None


def _find_branch_master_by_phone(phone_number):
    """
    Look up BranchMaster by contact_number matching the user's phone.
    Used as a fallback when _find_branch_by_client_id returns nothing.
    """
    if not phone_number:
        return None
    branch = BranchMaster.objects.filter(
        contact_number__endswith=phone_number,
        status='active'
    ).first()
    if branch:
        return {
            "branch_name":    branch.branch_name or "",
            "branch_address": branch.address or "",
        }
    return None


# ══════════════════════════ LOGIN FLOW ═══════════════════════════

@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def user_request_otp(request):
    """
    LOGIN — send OTP.
    Allowed if phone is in AccMaster OR already in User table (self-signed-up).
    Blocked with a sign-up prompt if neither.
    """
    phone_number = request.data.get("phone_number", "").strip().replace(" ", "")

    if not phone_number or not phone_number.lstrip("+").isdigit() or len(phone_number.lstrip("+")) < 10:
        return Response({"error": "Please provide a valid 10-digit mobile number."}, status=400)

    phone_number = phone_number[-10:]

    local_user = User.objects.filter(phone_number=phone_number).first()
    debtor     = _find_debtor_by_phone(phone_number)

    # Allow login if user is in User table (self-signed-up) OR in AccMaster.
    # If neither, redirect to signup.
    if not local_user and not debtor:
        return Response(
            {"error": "This number is not registered. Please sign up first.", "redirect": "signup"},
            status=404
        )

    if local_user:
        if _block_if_disabled(local_user):
            return Response({"error": "Your account is disabled. Please contact admin."}, status=403)
        name = (local_user.business_name or local_user.username or "user").split()[0]
    else:
        name = (debtor.get("name") or "user").split()[0]

    otp = "".join(random.choices(string.digits, k=6))
    cache.set(f"otp_{phone_number}", otp, timeout=300)
    print(f"[OTP LOGIN] Generated OTP {otp} for {phone_number}")

    sent, err_msg = _send_sms_otp(phone_number, otp)

    if not sent:
        print(f"[OTP] SMS send failed for {phone_number}: {err_msg}")
        return Response({
            "message":      f"OTP generated for number ending in {phone_number[-4:]}. Check terminal.",
            "phone_number": phone_number,
            "dev_otp":      otp,
        })

    return Response({
        "message":      f"OTP sent via SMS to number ending in {phone_number[-4:]}",
        "phone_number": phone_number,
    })


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def user_verify_otp(request):
    """
    LOGIN — verify OTP and return JWT.
    Works for both AccMaster customers (auto-creates User if needed)
    and previously self-signed-up users.
    """
    phone_number = request.data.get("phone_number", "").strip().replace(" ", "")
    otp_input    = request.data.get("otp", "").strip()

    if not phone_number or not otp_input:
        return Response({"error": "Phone number and OTP are required."}, status=400)

    phone_number = phone_number[-10:]
    cache_key    = f"otp_{phone_number}"
    cached_otp   = cache.get(cache_key)

    if not cached_otp:
        return Response({"error": "OTP expired or not requested. Please request a new OTP."}, status=400)

    if otp_input != cached_otp:
        return Response({"error": "Invalid OTP. Please try again."}, status=400)

    cache.delete(cache_key)

    debtor_code = ""
    debtor_name = ""
    place       = ""

    local_user = User.objects.filter(phone_number=phone_number).first()

    if local_user:
        # Already in User table (AccMaster customer or self-signed-up)
        user = local_user
        if _block_if_disabled(user):
            return Response({"error": "Your account is disabled. Please contact admin."}, status=403)
        debtor = _find_debtor_by_phone(phone_number)
        if debtor:
            debtor_code = (debtor.get("code") or "").strip()
            debtor_name = (debtor.get("name") or local_user.business_name or "").strip()
            place       = (debtor.get("place") or "").strip()
        else:
            debtor_name = local_user.business_name or ""
    else:
        # Not in User table yet — must be an AccMaster customer logging in for the first time
        debtor = _find_debtor_by_phone(phone_number)

        if not debtor:
            return Response({"error": "Mobile number not registered."}, status=404)

        debtor_code = (debtor.get("code") or "").strip()
        debtor_name = (debtor.get("name") or "").strip()
        place       = (debtor.get("place") or "").strip()

        user, _ = User.objects.get_or_create(
            phone_number=phone_number,
            defaults={
                "username":      f"debtor_{debtor_code}_{phone_number}",
                "user_type":     "user",
                "status":        "Active",
                "business_name": debtor_name,
                "location":      place,
            }
        )

    client_id = (getattr(user, "client_id", "") or "").strip()
    if not client_id:
        debtor_fresh = _find_debtor_by_phone(phone_number)
        client_id    = (debtor_fresh.get("client_id") or "") if debtor_fresh else ""

    branch_info    = _find_branch_by_client_id(client_id) or _find_branch_master_by_phone(phone_number)
    branch_name    = branch_info.get("branch_name", "")    if branch_info else ""
    branch_address = branch_info.get("branch_address", "") if branch_info else ""

    refresh = RefreshToken.for_user(user)
    return Response({
        "access":  str(refresh.access_token),
        "refresh": str(refresh),
        "user": {
            **UserPublicSerializer(user).data,
            "debtor_code":    debtor_code,
            "debtor_name":    debtor_name,
            "place":          place,
            "client_id":      client_id,
            "branch_name":    branch_name,
            "branch_address": branch_address,
        }
    })


# ══════════════════════════ SIGN-UP FLOW ══════════════════════════

@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def user_request_otp_signup(request):
    """
    SIGN-UP — send OTP.
    Blocked if phone is already in User table  → direct them to Login.
    Blocked if phone is in AccMaster           → direct them to Login.
    Allowed only for brand-new numbers not found in either table.
    Also accepts 'name' to cache for the verify step.
    """
    phone_number = request.data.get("phone_number", "").strip().replace(" ", "")
    name         = request.data.get("name", "").strip()

    if not phone_number or not phone_number.lstrip("+").isdigit() or len(phone_number.lstrip("+")) < 10:
        return Response({"error": "Please provide a valid 10-digit mobile number."}, status=400)

    phone_number = phone_number[-10:]

    # ── Block if already self-signed-up (exists in User table) ──────────────
    if User.objects.filter(phone_number=phone_number).exists():
        return Response(
            {
                "error":    "This number is already registered. Please login instead.",
                "redirect": "login",   # frontend uses this to auto-switch to Login tab
            },
            status=400
        )

    # ── Block if an AccMaster (shop) customer — they must use Login ──────────
    debtor = _find_debtor_by_phone(phone_number)
    if debtor:
        return Response(
            {
                "error":    "This number belongs to a registered customer. Please use Login instead.",
                "redirect": "login",
            },
            status=400
        )

    otp = "".join(random.choices(string.digits, k=6))
    cache.set(f"otp_signup_{phone_number}", otp, timeout=300)
    # Cache name so verify step can use it even if not re-sent
    cache.set(f"otp_signup_name_{phone_number}", name, timeout=300)
    print(f"[OTP SIGNUP] Generated OTP {otp} for {phone_number} | name={name or '(none)'}")

    sent, err_msg = _send_sms_otp(phone_number, otp)

    if not sent:
        print(f"[OTP SIGNUP] SMS send failed for {phone_number}: {err_msg}")
        return Response({
            "message":      f"OTP generated for number ending in {phone_number[-4:]}. Check terminal.",
            "phone_number": phone_number,
            "dev_otp":      otp,
        })

    return Response({
        "message":      f"OTP sent via SMS to number ending in {phone_number[-4:]}",
        "phone_number": phone_number,
    })


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def user_verify_otp_signup(request):
    """
    SIGN-UP — verify OTP and create a new User.
    Phone is required, name is required, email is optional.
    Saves to User table only (not AccMaster — that is a read-only external sync table).
    name is saved to business_name field to stay consistent with AccMaster customer records.
    """
    phone_number = request.data.get("phone_number", "").strip().replace(" ", "")
    otp_input    = request.data.get("otp", "").strip()
    name         = request.data.get("name", "").strip()
    email        = request.data.get("email", "").strip()

    if not phone_number or not otp_input:
        return Response({"error": "Phone number and OTP are required."}, status=400)

    phone_number = phone_number[-10:]
    cache_key    = f"otp_signup_{phone_number}"
    cached_otp   = cache.get(cache_key)

    if not cached_otp:
        return Response({"error": "OTP expired or not requested. Please request a new OTP."}, status=400)

    if otp_input != cached_otp:
        return Response({"error": "Invalid OTP. Please try again."}, status=400)

    cache.delete(cache_key)

    # Fallback: use cached name if frontend didn't re-send it at the verify step
    if not name:
        name = cache.get(f"otp_signup_name_{phone_number}", "")
    cache.delete(f"otp_signup_name_{phone_number}")

    # Safety net: if somehow an existing user reaches verify (e.g. OTP was
    # already in cache from before we added the request-step block), log them
    # in cleanly instead of creating a duplicate.
    existing_user = User.objects.filter(phone_number=phone_number).first()
    if existing_user:
        if name:
            existing_user.business_name = name
        if email:
            existing_user.email = email
        existing_user.save(update_fields=["business_name", "email"])
        refresh = RefreshToken.for_user(existing_user)
        return Response({
            "access":  str(refresh.access_token),
            "refresh": str(refresh),
            "user":    UserPublicSerializer(existing_user).data,
        })

    # Build a unique username
    username_base = name.lower().replace(" ", "_") if name else f"user_{phone_number}"

    username      = username_base
    counter       = 1
    while User.objects.filter(username=username).exists():
        username = f"{username_base}_{counter}"
        counter += 1

    create_kwargs = {
        "username":      username,
        "user_type":     "user",
        "status":        "Active",
        "phone_number":  phone_number,
        "business_name": name,
    }
    if email:
        create_kwargs["email"] = email

    user = User.objects.create(**create_kwargs)

    refresh = RefreshToken.for_user(user)
    return Response({
        "access":  str(refresh.access_token),
        "refresh": str(refresh),
        "user":    {**UserPublicSerializer(user).data, "is_new_user": True},
    }, status=201)


# ══════════════════════════ LEGACY / UNUSED ═══════════════════════

@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def user_login(request):
    """Kept for backward compatibility. Prefer user_request_otp."""
    phone_number = request.data.get("phone_number", "").strip().replace(" ", "")

    if not phone_number or not phone_number.lstrip("+").isdigit() or len(phone_number.lstrip("+")) < 10:
        return Response({"error": "Please provide a valid 10-digit mobile number."}, status=400)

    phone_number = phone_number[-10:]

    local_user = User.objects.filter(phone_number=phone_number).first()
    if not local_user:
        debtor = _find_debtor_by_phone(phone_number)
        if not debtor:
            return Response(
                {"error": "Mobile number not registered. Please contact your admin."},
                status=404
            )
        name = (debtor.get("name") or "user").split()[0]
    else:
        if _block_if_disabled(local_user):
            return Response({"error": "Your account is disabled. Please contact admin."}, status=403)
        name = (local_user.business_name or local_user.username or "user").split()[0]

    otp = "".join(random.choices(string.digits, k=6))
    cache.set(f"otp_{phone_number}", otp, timeout=300)
    print(f"[OTP] Generated OTP {otp} for {phone_number}")

    sent, err_msg = _send_sms_otp(phone_number, otp)

    if not sent:
        print(f"[OTP] SMS send failed for {phone_number}: {err_msg}")
        return Response({
            "message":      f"OTP generated for number ending in {phone_number[-4:]}. Check terminal.",
            "phone_number": phone_number,
            "requires_otp": True,
            "dev_otp":      otp,
        })

    return Response({
        "message":      f"OTP sent via SMS to number ending in {phone_number[-4:]}",
        "phone_number": phone_number,
        "requires_otp": True,
    })


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def register_user(request):
    serializer = UserRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    user = serializer.save(user_type="user")
    refresh = RefreshToken.for_user(user)
    return Response({
        "access":  str(refresh.access_token),
        "refresh": str(refresh),
        "user":    UserPublicSerializer(user).data
    }, status=201)


# ===================== CATEGORY =====================

class CategoryListCreateView(generics.ListCreateAPIView):
    serializer_class   = CategorySerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]

    def get_queryset(self):
        return Category.objects.all().order_by("-id")

    def perform_create(self, serializer):
        serializer.save()


class CategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = CategorySerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset           = Category.objects.all()
    parser_classes     = [MultiPartParser, FormParser]

    def destroy(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.delete()
            return Response({"message": "Category deleted successfully"}, status=status.HTTP_200_OK)
        except Category.DoesNotExist:
            return Response({"error": "Category not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["PATCH", "PUT"])
@permission_classes([permissions.IsAuthenticated])
def update_category_image(request, category_id):
    try:
        category = Category.objects.get(id=category_id)
        if "image" in request.FILES:
            category.image = request.FILES["image"]
            category.save()
            return Response(CategorySerializer(category).data)
        return Response({"error": "No image provided"}, status=400)
    except Category.DoesNotExist:
        return Response({"error": "Category not found"}, status=404)


# ===================== PRODUCTS =====================

class ProductListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]

    def get_serializer_class(self):
        return ProductCreateSerializer if self.request.method == "POST" else ProductSerializer

    def get_queryset(self):
        return Product.objects.filter(user=self.request.user).order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class ProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = ProductSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]

    def get_queryset(self):
        return Product.objects.filter(user=self.request.user)

    def update(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            data     = request.data.copy()
            data.pop('category', None)
            data.pop('valid_until', None)
            serializer = ProductCreateSerializer(instance, data=data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(ProductSerializer(instance).data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except Product.DoesNotExist:
            return Response({"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            import traceback; traceback.print_exc()
            return Response({"error": f"Failed to update product: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def destroy(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            try:
                if hasattr(instance, 'offers'):
                    instance.offers.clear()
            except Exception as clear_error:
                print(f"Warning: Could not clear offers relationship: {str(clear_error)}")
            instance.delete()
            return Response({"message": "Product deleted successfully"}, status=status.HTTP_200_OK)
        except Product.DoesNotExist:
            return Response({"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            import traceback; traceback.print_exc()
            return Response({"error": f"Failed to delete product: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def products_by_category(request, category_name):
    products = Product.objects.filter(
        user=request.user, category=category_name, is_active=True
    ).order_by("-created_at")
    return Response(ProductSerializer(products, many=True).data)


# ===================== LEGACY OFFER (per product) =====================

@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def get_offer(request, product_id):
    try:
        product    = Product.objects.get(id=product_id, is_active=True)
        serializer = OfferTemplateSerializer(product)
        return Response(serializer.data)
    except Product.DoesNotExist:
        return Response({"error": "Offer not found or has expired."}, status=status.HTTP_404_NOT_FOUND)


# ===================== NEW OFFER SYSTEM =====================

class OfferCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = OfferCreateSerializer(data=request.data, context={"request": request})
        if serializer.is_valid():
            offer = serializer.save()
            out   = OfferPublicSerializer(offer, context={"request": request})
            return Response(out.data, status=201)
        return Response(serializer.errors, status=400)


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def public_offer_detail(request, offer_id):
    try:
        offer      = Offer.objects.get(id=offer_id, is_public=True)
        serializer = OfferPublicSerializer(offer)
        return Response(serializer.data)
    except Offer.DoesNotExist:
        return Response({"error": "Offer not found"}, status=404)


# ===================== OFFER MASTER =====================

from .models import ExpoPushToken
from .push_notifications import send_expo_push_notification


def _build_offer_notification(offer):
    """
    Build push notification title + body for an OfferMaster.

    - Regular offer  → standard title/body
    - Hourly offer   → title gets ⏰ flash label, body shows time remaining
                       e.g. "⏰ Flash Offer: Summer Sale"
                            "Hurry! Valid for next 2h 30m only (ends at 05:00 PM)"
                       If a description exists it is prepended:
                            "Big discounts on all items!\n⏰ Ends in 2h 30m (at 05:00 PM)"
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


class OfferMasterListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]

    def get_queryset(self):
        auto_expire_offers()
        # Order by -updated_at so that reactivated (inactive → active) offers
        # surface to the top immediately, just like newly created offers do.
        return OfferMaster.objects.all().prefetch_related('branches', 'media_files').order_by('-updated_at')

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return OfferMasterCreateUpdateSerializer
        return OfferMasterSerializer

    def create(self, request, *args, **kwargs):
        if request.user.user_type != 'admin':
            return Response({"error": "Only administrators can create offers"}, status=status.HTTP_403_FORBIDDEN)
        try:
            files      = request.FILES.getlist('files')
            branch_ids = request.data.getlist('branch_ids')
            data = {
                'title':       request.data.get('title'),
                'description': request.data.get('description', ''),
                'valid_from':  request.data.get('valid_from'),
                'valid_to':    request.data.get('valid_to'),
                'status':      request.data.get('status', 'active'),
            }
            offer_start_time = request.data.get('offer_start_time', '')
            offer_end_time   = request.data.get('offer_end_time', '')
            data['offer_start_time'] = offer_start_time if offer_start_time else None
            data['offer_end_time']   = offer_end_time   if offer_end_time   else None
            if files:      data['files']      = files
            if branch_ids: data['branch_ids'] = branch_ids
            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            offer_master        = serializer.save(user=request.user)
            response_serializer = OfferMasterSerializer(offer_master, context={'request': request})

            # ── Send push notification only if the offer is ACTIVE right now ──
            # If status is 'scheduled', the scheduler will send the notification
            # automatically when valid_from arrives (see scheduler._activate_scheduled_offers).
            if offer_master.status == 'active':
                try:
                    tokens = list(ExpoPushToken.objects.values_list('token', flat=True))
                    if tokens:
                        notif_title, notif_body = _build_offer_notification(offer_master)
                        _, dead_tokens = send_expo_push_notification(
                            tokens,
                            notif_title,
                            notif_body,
                            {
                                'type':            'new_offer',
                                'offer_master_id': str(offer_master.id),
                            }
                        )
                        # Clean up dead tokens
                        if dead_tokens:
                            ExpoPushToken.objects.filter(token__in=dead_tokens).delete()
                        print(f"[OfferMaster] Push sent to {len(tokens)} device(s) for offer '{offer_master.title}'")
                except Exception as notif_err:
                    # Non-fatal — offer creation still succeeds even if push fails
                    print(f"[OfferMaster] Push notification failed (non-fatal): {notif_err}")
            else:
                print(f"[OfferMaster] Offer '{offer_master.title}' is scheduled — push will fire when it goes active.")

            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            import traceback; traceback.print_exc()
            return Response({"error": f"Failed to create offer: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context


class OfferMasterDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]

    def get_queryset(self):
        return OfferMaster.objects.all().prefetch_related('branches', 'media_files')

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return OfferMasterCreateUpdateSerializer
        return OfferMasterSerializer

    def update(self, request, *args, **kwargs):
        if request.user.user_type != 'admin':
            return Response({"error": "Only administrators can update offers"}, status=status.HTTP_403_FORBIDDEN)
        try:
            instance        = self.get_object()
            previous_status = instance.status          # ← capture BEFORE save
            files      = request.FILES.getlist('files')
            branch_ids = request.data.getlist('branch_ids')
            data = {
                'title':       request.data.get('title',       instance.title),
                'description': request.data.get('description', instance.description),
                'valid_from':  request.data.get('valid_from',  instance.valid_from),
                'valid_to':    request.data.get('valid_to',    instance.valid_to),
                'status':      request.data.get('status',      instance.status),
            }
            if 'offer_start_time' in request.data:
                val = request.data.get('offer_start_time', '')
                data['offer_start_time'] = val if val else None
            if 'offer_end_time' in request.data:
                val = request.data.get('offer_end_time', '')
                data['offer_end_time'] = val if val else None
            if files:                  data['files']      = files
            if branch_ids is not None: data['branch_ids'] = branch_ids
            serializer = self.get_serializer(instance, data=data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            instance.refresh_from_db()
            response_serializer = OfferMasterSerializer(instance, context={'request': request})

            # ── Send push notification when an offer is reactivated ──────────
            # Fires only when:  previous status was NOT 'active'
            #               AND new status IS 'active'
            # This covers the inactive → active case (manual reactivation via edit).
            if previous_status != 'active' and instance.status == 'active':
                try:
                    tokens = list(ExpoPushToken.objects.values_list('token', flat=True))
                    if tokens:
                        notif_title, notif_body = _build_offer_notification(instance)
                        _, dead_tokens = send_expo_push_notification(
                            tokens,
                            notif_title,
                            notif_body,
                            {
                                'type':            'new_offer',
                                'offer_master_id': str(instance.id),
                            }
                        )
                        if dead_tokens:
                            ExpoPushToken.objects.filter(token__in=dead_tokens).delete()
                        print(f"[OfferMaster] Reactivation push sent to {len(tokens)} device(s) for '{instance.title}'")
                except Exception as notif_err:
                    # Non-fatal — offer update still succeeds even if push fails
                    print(f"[OfferMaster] Reactivation push notification failed (non-fatal): {notif_err}")

            return Response(response_serializer.data)
        except OfferMaster.DoesNotExist:
            return Response({"error": "Offer not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            import traceback; traceback.print_exc()
            return Response({"error": f"Failed to update offer: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

    def destroy(self, request, *args, **kwargs):
        if request.user.user_type != 'admin':
            return Response({"error": "Only administrators can delete offers"}, status=status.HTTP_403_FORBIDDEN)
        try:
            instance = self.get_object()
            instance.delete()
            return Response({"message": "Offer deleted successfully"}, status=status.HTTP_200_OK)
        except OfferMaster.DoesNotExist:
            return Response({"error": "Offer not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            import traceback; traceback.print_exc()
            return Response({"error": f"Failed to delete offer: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ===================== OFFER MASTER MEDIA =====================

@api_view(['DELETE'])
@permission_classes([permissions.IsAuthenticated])
def delete_offer_master_media(request, pk, media_id):
    if request.user.user_type != 'admin':
        return Response({"error": "Only administrators can delete media files"}, status=status.HTTP_403_FORBIDDEN)
    try:
        media = OfferMasterMedia.objects.get(id=media_id, offer_master_id=pk)
        media.delete()
        return Response({"message": "Media file deleted successfully"}, status=status.HTTP_200_OK)
    except OfferMasterMedia.DoesNotExist:
        return Response({"error": "Media file not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response({"error": f"Failed to delete media file: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def offer_master_stats(request):
    user = request.user
    if user.user_type == 'admin':
        total     = OfferMaster.objects.filter(user=user).count()
        active    = OfferMaster.objects.filter(user=user, status='active').count()
        inactive  = OfferMaster.objects.filter(user=user, status='inactive').count()
        scheduled = OfferMaster.objects.filter(user=user, status='scheduled').count()
    else:
        total     = OfferMaster.objects.all().count()
        active    = OfferMaster.objects.filter(status='active').count()
        inactive  = OfferMaster.objects.filter(status='inactive').count()
        scheduled = OfferMaster.objects.filter(status='scheduled').count()
    return Response({'total': total, 'active': active, 'inactive': inactive, 'scheduled': scheduled})


# ===================== BRANCH-SPECIFIC VIEWS =====================

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_user_branches(request):
    try:
        branches   = BranchMaster.objects.filter(user=request.user, status='active').order_by('branch_name')
        serializer = BranchMasterSerializer(branches, many=True, context={'request': request})
        return Response({'success': True, 'count': branches.count(), 'branches': serializer.data})
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response({'error': f'Failed to fetch branches: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_branch_offers(request, branch_id):
    auto_expire_offers()
    try:
        branch = BranchMaster.objects.prefetch_related('offers', 'offers__media_files').get(id=branch_id, user=request.user)
    except BranchMaster.DoesNotExist:
        return Response({'success': False, 'error': 'Branch not found or you do not have access'}, status=status.HTTP_404_NOT_FOUND)
    try:
        offers            = branch.offers.filter(status='active').order_by('-updated_at')
        branch_serializer = BranchMasterSerializer(branch, context={'request': request})
        offers_serializer = OfferMasterSerializer(offers, many=True, context={'request': request})
        return Response({'success': True, 'branch': branch_serializer.data, 'offers_count': offers.count(), 'offers': offers_serializer.data})
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response({'error': f'Failed to fetch offers: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_all_branches_dropdown(request):
    user = request.user
    try:
        if user.user_type == 'admin' or user.is_superuser:
            branches = BranchMaster.objects.filter(status='active').select_related('user').order_by('user__shop_name', 'branch_name')
        else:
            branches = BranchMaster.objects.filter(user=user, status='active').order_by('branch_name')
        branch_list = [{
            'id':          str(branch.id),
            'label':       f"{branch.branch_name} ({branch.branch_code}) - {branch.user.shop_name or branch.user.username}",
            'branch_name': branch.branch_name,
            'branch_code': branch.branch_code,
            'shop_name':   branch.user.shop_name or branch.user.username,
            'user_id':     branch.user.id,
            'location':    branch.location
        } for branch in branches]
        return Response({'success': True, 'count': len(branch_list), 'branches': branch_list})
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response({'error': f'Failed to fetch branches: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ===================== PUBLIC OFFER DISCOVERY =====================

@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def discover_offers(request):
    try:
        auto_expire_offers()
        location  = request.query_params.get('location', None)
        city      = request.query_params.get('city', None)
        branch_id = request.query_params.get('branch_id', None)
        today     = timezone.localdate()
        offers    = OfferMaster.objects.filter(
            valid_from__lte=today, valid_to__gte=today,
        ).exclude(status='inactive').prefetch_related('branches', 'branches__user', 'media_files')
        if branch_id:
            offers = offers.filter(branches__id=branch_id)
        elif location:
            offers = offers.filter(branches__location__icontains=location)
        elif city:
            offers = offers.filter(branches__city__icontains=city)
        # Sort by -updated_at so reactivated offers surface to the top immediately.
        # (updated_at is bumped by auto_now=True on every save, including reactivation)
        offers     = offers.distinct().order_by('-updated_at')
        serializer = OfferMasterSerializer(offers, many=True, context={'request': request})
        return Response({'success': True, 'count': offers.count(), 'offers': serializer.data})
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response({'error': f'Failed to discover offers: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def get_all_active_branches_public(request):
    try:
        auto_expire_offers()
        location = request.query_params.get('location', None)
        city     = request.query_params.get('city', None)
        branches = BranchMaster.objects.filter(status='active').select_related('user').prefetch_related('offers', 'offers__media_files')
        if location:
            branches = branches.filter(location__icontains=location)
        if city:
            branches = branches.filter(city__icontains=city)
        branches   = branches.order_by('user__shop_name', 'branch_name')
        serializer = BranchWithOffersSerializer(branches, many=True, context={'request': request})
        return Response({'success': True, 'count': branches.count(), 'branches': serializer.data})
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response({'error': f'Failed to fetch branches: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ===================== TEMPLATES =====================

class TemplateListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        templates = [
            {"id": 1, "name": "Template 1", "type": "template1"},
            {"id": 2, "name": "Template 2", "type": "template2"},
            {"id": 3, "name": "Template 3", "type": "template3"},
            {"id": 4, "name": "Template 4", "type": "template4"},
        ]
        return Response(templates)


# ===================== DASHBOARD STATS =====================

@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def user_dashboard_stats(request):
    user = request.user

    client_id = (getattr(user, 'client_id', '') or '').strip()
    phone     = (getattr(user, 'phone_number', '') or '').strip().lstrip('+')
    if len(phone) > 10:
        phone = phone[-10:]

    branch_info = _find_branch_by_client_id(client_id)
    if not branch_info and phone:
        branch_info = _find_branch_master_by_phone(phone)
    branch_name    = branch_info.get('branch_name', '')    if branch_info else ''
    branch_address = branch_info.get('branch_address', '') if branch_info else ''

    return Response({
        "total_categories":     Category.objects.count(),
        "total_products":       Product.objects.filter(user=user).count(),
        "active_offers":        Product.objects.filter(user=user, is_active=True).count(),
        "total_offer_masters":  OfferMaster.objects.filter(user=user).count(),
        "active_offer_masters": OfferMaster.objects.filter(user=user, status='active').count(),
        "client_id":      client_id,
        "branch_name":    branch_name,
        "branch_address": branch_address,
    })


# ===================== PROFILE =====================

@api_view(["GET", "PUT"])
@permission_classes([permissions.IsAuthenticated])
def user_profile(request):
    user = request.user
    if request.method == "GET":
        return Response(UserPublicSerializer(user).data)
    serializer = UserSerializer(user, data=request.data, partial=True)
    if serializer.is_valid():
        serializer.save()
        return Response(UserPublicSerializer(user).data)
    return Response(serializer.errors, status=400)


# ===================== ADMIN USER MANAGEMENT =====================

class AdminListView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]

    def get(self, request):
        try:
            search_term = request.GET.get("search", "")

            # Show ALL users regardless of AccMaster linkage
            queryset = User.objects.filter(user_type="user")

            if search_term:
                queryset = queryset.filter(
                    Q(username__icontains=search_term) |
                    Q(email__icontains=search_term) |
                    Q(shop_name__icontains=search_term) |
                    Q(location__icontains=search_term) |
                    Q(business_name__icontains=search_term) |
                    Q(phone_number__icontains=search_term)
                )

            queryset = queryset.order_by("-date_joined")
            return Response(UserPublicSerializer(queryset, many=True).data)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

    def post(self, request):
        try:
            data = request.data.copy()
            data["user_type"]     = "user"
            data["business_name"] = data.get("customer_name", "")
            serializer = UserSerializer(data=data)
            if serializer.is_valid():
                user = serializer.save()
                user.set_password(data.get("password"))
                user.save()
                return Response(UserPublicSerializer(user).data, status=201)
            return Response(serializer.errors, status=400)
        except Exception as e:
            return Response({"error": str(e)}, status=500)


class AdminDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]
    serializer_class   = UserSerializer

    def get_queryset(self):
        return User.objects.filter(user_type="user")

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.delete()
        return Response({"message": "User deleted successfully"}, status=status.HTTP_200_OK)


# ===================== ADMIN STATS =====================

class AdminStatsView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]

    def get(self, request):
        # Show stats for ALL users
        base_qs = User.objects.filter(user_type="user")
        return Response({
            "total_admins":    base_qs.count(),
            "active_admins":   base_qs.filter(status="Active").count(),
            "disabled_admins": base_qs.filter(status="Disable").count(),
        })


# ===================== BRANCH MASTER =====================

class BranchMasterListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            if request.user.is_superuser or request.user.user_type == 'admin':
                branches = BranchMaster.objects.all().select_related('user').order_by('user__shop_name', 'branch_name')
            else:
                branches = BranchMaster.objects.filter(user=request.user)
            serializer = BranchMasterSerializer(branches, many=True, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': f'Failed to fetch branches: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        try:
            serializer = BranchMasterCreateUpdateSerializer(data=request.data, context={'request': request})
            if serializer.is_valid():
                if not (request.user.is_superuser or request.user.user_type == 'admin'):
                    serializer.validated_data['user'] = request.user
                branch = serializer.save()
                branch.refresh_from_db()
                response_serializer = BranchMasterSerializer(branch, context={'request': request})
                return Response(response_serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': f'Failed to create branch: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BranchMasterDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self, pk, user):
        try:
            if user.is_superuser or user.user_type == 'admin':
                return BranchMaster.objects.get(pk=pk)
            else:
                return BranchMaster.objects.get(pk=pk, user=user)
        except BranchMaster.DoesNotExist:
            return None

    def get(self, request, pk):
        branch = self.get_object(pk, request.user)
        if not branch:
            return Response({'error': 'Branch not found or you do not have permission to view it'}, status=status.HTTP_404_NOT_FOUND)
        serializer = BranchMasterSerializer(branch, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request, pk):
        branch = self.get_object(pk, request.user)
        if not branch:
            return Response({'error': 'Branch not found or you do not have permission to update it'}, status=status.HTTP_404_NOT_FOUND)
        try:
            serializer = BranchMasterCreateUpdateSerializer(branch, data=request.data, partial=True, context={'request': request})
            if serializer.is_valid():
                updated_branch = serializer.save()
                updated_branch.refresh_from_db()
                response_serializer = BranchMasterSerializer(updated_branch, context={'request': request})
                return Response(response_serializer.data, status=status.HTTP_200_OK)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': f'Failed to update branch: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        branch = self.get_object(pk, request.user)
        if not branch:
            return Response({'error': 'Branch not found or you do not have permission to delete it'}, status=status.HTTP_404_NOT_FOUND)
        try:
            branch.delete()
            return Response({'message': 'Branch deleted successfully'}, status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({'error': f'Failed to delete branch: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def branch_master_stats(request):
    try:
        if request.user.is_superuser or request.user.user_type == 'admin':
            branches = BranchMaster.objects.all()
        else:
            branches = BranchMaster.objects.filter(user=request.user)
        return Response({
            'total_branches':    branches.count(),
            'active_branches':   branches.filter(status='active').count(),
            'inactive_branches': branches.filter(status='inactive').count(),
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({'error': f'Failed to fetch branch statistics: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_all_users_for_dropdown(request):
    try:
        if not (request.user.is_superuser or request.user.user_type == 'admin'):
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        users      = User.objects.filter(user_type='user').order_by('username')
        serializer = UserSimpleSerializer(users, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({'error': f'Failed to fetch users: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ===================== MISEL SHOP SYNC =====================

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def sync_misel_shops(request):
    if not (request.user.is_superuser or request.user.user_type == 'admin'):
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

    misel_records = Misel.objects.all()
    created      = []
    skipped      = []
    no_client_id = []

    for shop in misel_records:
        firm_name = (shop.firm_name or '').strip()
        address   = (shop.address1  or '').strip()
        client_id = (shop.client_id or '').strip()

        if not firm_name:
            continue

        if client_id:
            base_username = f"misel_{client_id}"
        else:
            base_username = f"misel_{shop.id}"
            no_client_id.append(firm_name)

        if User.objects.filter(username=base_username).exists():
            skipped.append(base_username)
            continue

        User.objects.create_user(
            username=base_username,
            email=f"{base_username}@misel.sync",
            password=secrets.token_urlsafe(16),
            user_type='user',
            shop_name=firm_name,
            business_name=client_id,
            location=address,
            status='Active',
        )
        created.append(base_username)

    return Response({
        'success':       True,
        'created':       created,
        'created_count': len(created),
        'skipped':       skipped,
        'skipped_count': len(skipped),
        'no_client_id':  no_client_id,
        'message': (
            f'{len(created)} shop(s) synced, {len(skipped)} already existed'
            + (f', {len(no_client_id)} missing client_id (used id fallback).' if no_client_id else '.')
        )
    }, status=status.HTTP_200_OK)


# ===================== PUBLIC BRANCH OFFERS (QR SCAN LANDING) =====================

@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def public_branch_offers(request, branch_id):
    auto_expire_offers()
    try:
        branch = BranchMaster.objects.prefetch_related('offers', 'offers__media_files', 'user').get(id=branch_id)
    except BranchMaster.DoesNotExist:
        return Response({'error': 'Branch not found.'}, status=status.HTTP_404_NOT_FOUND)
    serializer = BranchWithOffersSerializer(branch, context={'request': request})
    return Response(serializer.data)


# ===================== USER INVOICES =====================

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def user_invoices(request):
    debtor_code = request.query_params.get('debtor_code', '').strip()

    if not debtor_code:
        username = getattr(request.user, 'username', '') or ''
        if username.startswith('debtor_'):
            inner       = username[len('debtor_'):]
            parts       = inner.rsplit('_', 1)
            debtor_code = parts[0] if len(parts) == 2 else inner

    if not debtor_code:
        return Response(
            {'error': 'Could not determine customer code for this account.'},
            status=400
        )

    limit = min(int(request.query_params.get('limit', 20)), 50)

    invoices_qs = AccInvMast.objects.filter(
        customerid=debtor_code
    ).order_by('-slno').values('slno', 'invdate', 'nettotal')[:limit]

    collected = [
        {
            'slno':     inv['slno'],
            'invdate':  str(inv['invdate']) if inv['invdate'] else None,
            'nettotal': str(inv['nettotal']) if inv['nettotal'] else "0",
        }
        for inv in invoices_qs
    ]

    return Response({
        'success':     True,
        'debtor_code': debtor_code,
        'total_found': len(collected),
        'invoices':    collected,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def user_invoice_bill(request, slno):
    """
    GET /api/invoices/my/<slno>/
    Returns a single invoice bill for the logged-in user.
    Users can only see their own invoices — matched via debtor code or phone number.
    Admins can see any invoice under their client_id.
    """
    user = request.user

    # ── Admin path ───────────────────────────────────────────────────────────
    if user.is_superuser or user.user_type == 'admin':
        admin_client_id = getattr(user, 'client_id', '') or ''
        try:
            invoice = AccInvMast.objects.get(slno=slno, client_id=admin_client_id)
        except AccInvMast.DoesNotExist:
            return Response({'error': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)
        customer = AccMaster.objects.filter(code=invoice.customerid, client_id=admin_client_id).first()
        return Response({
            'success':        True,
            'slno':           invoice.slno,
            'invdate':        str(invoice.invdate) if invoice.invdate else None,
            'nettotal':       str(invoice.nettotal) if invoice.nettotal else '0',
            'customerid':     invoice.customerid,
            'customer_name':  customer.name  if customer else '',
            'customer_place': customer.place if customer else '',
        })

    # ── User path: derive debtor code from username ──────────────────────────
    debtor_code = ''
    username = getattr(user, 'username', '') or ''
    if username.startswith('debtor_'):
        inner       = username[len('debtor_'):]
        parts       = inner.rsplit('_', 1)
        debtor_code = parts[0] if len(parts) == 2 else inner

    # Fallback: match via phone number in AccMaster
    if not debtor_code:
        phone = (getattr(user, 'phone_number', '') or '').strip().lstrip('+')
        if len(phone) > 10:
            phone = phone[-10:]
        if phone:
            acc = AccMaster.objects.filter(phone2__endswith=phone).first()
            if acc:
                debtor_code = acc.code

    if not debtor_code:
        return Response(
            {'error': 'Could not determine your customer account. Please contact admin.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        invoice = AccInvMast.objects.get(slno=slno, customerid=debtor_code)
    except AccInvMast.DoesNotExist:
        return Response(
            {'error': 'Invoice not found or does not belong to your account.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Pull customer name/place from AccMaster for the bill header
    customer_name  = ''
    customer_place = ''
    acc = AccMaster.objects.filter(code=debtor_code, client_id=invoice.client_id).first()
    if acc:
        customer_name  = acc.name  or ''
        customer_place = acc.place or ''

    return Response({
        'success':        True,
        'slno':           invoice.slno,
        'invdate':        str(invoice.invdate) if invoice.invdate else None,
        'nettotal':       str(invoice.nettotal) if invoice.nettotal else '0',
        'customerid':     invoice.customerid,
        'customer_name':  customer_name,
        'customer_place': customer_place,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def invoice_history(request):
    """
    GET /api/invoices/history/

    Returns paginated invoice history for a customer, showing invdate,
    customerid, and nettotal.

    Query params:
      - customer_id : (admin only) filter by a specific customerid
      - from_date   : filter invoices on or after this date (YYYY-MM-DD)
      - to_date     : filter invoices on or before this date (YYYY-MM-DD)
      - page        : page number (default 1)
      - page_size   : results per page (default 20, max 100)

    Regular users see only their own invoices (derived from their username
    or phone number). Admins see all invoices under their client_id and can
    optionally pass customer_id to narrow results.
    """
    user = request.user

    # ── Resolve queryset based on user role ──────────────────────────────────
    if user.is_superuser or user.user_type == 'admin':
        admin_client_id = (getattr(user, 'client_id', '') or '').strip()
        if not admin_client_id:
            return Response({'error': 'Admin account has no client_id.'}, status=400)

        qs = AccInvMast.objects.filter(client_id=admin_client_id)

        customer_id = request.query_params.get('customer_id', '').strip()
        if customer_id:
            qs = qs.filter(customerid=customer_id)

    else:
        # Derive debtor code from username (e.g. "debtor_CODE_phone")
        debtor_code = ''
        username = (getattr(user, 'username', '') or '')
        if username.startswith('debtor_'):
            inner = username[len('debtor_'):]
            parts = inner.rsplit('_', 1)
            debtor_code = parts[0] if len(parts) == 2 else inner

        # Fallback: match via phone number in AccMaster
        if not debtor_code:
            phone = (getattr(user, 'phone_number', '') or '').strip().lstrip('+')
            if len(phone) > 10:
                phone = phone[-10:]
            if phone:
                acc = AccMaster.objects.filter(phone2__endswith=phone).first()
                if acc:
                    debtor_code = acc.code

        if not debtor_code:
            return Response(
                {'error': 'Could not determine customer code for this account.'},
                status=400,
            )

        qs = AccInvMast.objects.filter(customerid=debtor_code)

    # ── Date filters ─────────────────────────────────────────────────────────
    from_date = request.query_params.get('from_date', '').strip()
    to_date   = request.query_params.get('to_date', '').strip()

    if from_date:
        try:
            qs = qs.filter(invdate__gte=from_date)
        except Exception:
            return Response({'error': 'Invalid from_date. Use YYYY-MM-DD.'}, status=400)

    if to_date:
        try:
            qs = qs.filter(invdate__lte=to_date)
        except Exception:
            return Response({'error': 'Invalid to_date. Use YYYY-MM-DD.'}, status=400)

    qs = qs.order_by('-invdate', '-slno')

    # ── Pagination ────────────────────────────────────────────────────────────
    try:
        page      = max(1, int(request.query_params.get('page', 1)))
        page_size = min(int(request.query_params.get('page_size', 20)), 100)
    except ValueError:
        return Response({'error': 'page and page_size must be integers.'}, status=400)

    total  = qs.count()
    start  = (page - 1) * page_size
    sliced = qs.values('slno', 'invdate', 'customerid', 'nettotal')[start:start + page_size]

    results = [
        {
            'slno':       inv['slno'],
            'invdate':    str(inv['invdate']) if inv['invdate'] else None,
            'customerid': inv['customerid'],
            'nettotal':   str(inv['nettotal']) if inv['nettotal'] else '0.000',
        }
        for inv in sliced
    ]

    return Response({
        'success':     True,
        'total':       total,
        'page':        page,
        'page_size':   page_size,
        'total_pages': (total + page_size - 1) // page_size if total else 0,
        'results':     results,
    })


# ================================================================
# ===================== SYNC DATA VIEWS ==========================
# ================================================================

def _require_admin(user):
    return not (user.is_superuser or user.user_type == 'admin')


# -------------------- AccMaster (Customers) ---------------------

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def acc_master_list(request):
    user = request.user

    if user.is_superuser or user.user_type == 'admin':
        admin_client_id = getattr(user, 'client_id', '') or ''
        qs = AccMaster.objects.filter(client_id=admin_client_id).order_by('code')

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)   |
                Q(code__icontains=search)   |
                Q(phone2__icontains=search) |
                Q(place__icontains=search)
            )

        total  = qs.count()
        limit  = min(int(request.query_params.get('limit',  50)), 200)
        offset = int(request.query_params.get('offset', 0))
        qs     = qs[offset: offset + limit]

        return Response({
            'total':   total,
            'limit':   limit,
            'offset':  offset,
            'results': AccMasterSerializer(qs, many=True).data,
        })

    phone = (getattr(user, 'phone_number', '') or '').strip().lstrip('+')
    if len(phone) > 10:
        phone = phone[-10:]

    if not phone:
        return Response(
            {'error': 'No phone number linked to your account. Please contact admin.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    record = AccMaster.objects.filter(phone2__endswith=phone).first()
    if not record:
        return Response(
            {'error': 'No customer account found for your phone number. Please contact admin.'},
            status=status.HTTP_404_NOT_FOUND
        )

    return Response({
        'total':   1,
        'results': AccMasterSerializer([record], many=True).data,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def acc_master_detail(request, pk):
    user = request.user

    if user.is_superuser or user.user_type == 'admin':
        admin_client_id = getattr(user, 'client_id', '') or ''
        try:
            obj = AccMaster.objects.get(pk=pk, client_id=admin_client_id)
        except AccMaster.DoesNotExist:
            return Response({'error': 'Customer not found.'}, status=status.HTTP_404_NOT_FOUND)

        invoices = AccInvMast.objects.filter(
            customerid=obj.code, client_id=admin_client_id
        ).order_by('-slno').values('slno', 'invdate', 'nettotal')[:50]

        invoice_data = [
            {
                'slno':     inv['slno'],
                'invdate':  str(inv['invdate']) if inv['invdate'] else None,
                'nettotal': str(inv['nettotal']) if inv['nettotal'] else '0',
            }
            for inv in invoices
        ]

        data = AccMasterSerializer(obj).data
        data['invoices']      = invoice_data
        data['invoice_count'] = len(invoice_data)
        return Response(data)

    phone = (getattr(user, 'phone_number', '') or '').strip().lstrip('+')
    if len(phone) > 10:
        phone = phone[-10:]

    if not phone:
        return Response({'error': 'No phone number linked to your account.'}, status=status.HTTP_400_BAD_REQUEST)

    record = AccMaster.objects.filter(phone2__endswith=phone).first()
    if not record:
        return Response({'error': 'No customer account found for your phone number.'}, status=status.HTTP_404_NOT_FOUND)

    if record.pk != pk:
        return Response({'error': 'You do not have permission to view this record.'}, status=status.HTTP_403_FORBIDDEN)

    return Response(AccMasterSerializer(record).data)


# -------------------- Misel (Shops) ---------------------

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def misel_list(request):
    if _require_admin(request.user):
        return Response({'error': 'Admin access only.'}, status=status.HTTP_403_FORBIDDEN)

    qs = Misel.objects.all().order_by('firm_name')

    search = request.query_params.get('search', '').strip()
    if search:
        qs = qs.filter(
            Q(firm_name__icontains=search) |
            Q(address1__icontains=search)
        )

    total  = qs.count()
    limit  = min(int(request.query_params.get('limit',  50)), 200)
    offset = int(request.query_params.get('offset', 0))
    qs     = qs[offset: offset + limit]

    return Response({
        'total':   total,
        'limit':   limit,
        'offset':  offset,
        'results': MiselSerializer(qs, many=True).data,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def misel_detail(request, pk):
    if _require_admin(request.user):
        return Response({'error': 'Admin access only.'}, status=status.HTTP_403_FORBIDDEN)

    try:
        obj = Misel.objects.get(pk=pk)
    except Misel.DoesNotExist:
        return Response({'error': 'Shop not found.'}, status=status.HTTP_404_NOT_FOUND)

    return Response(MiselSerializer(obj).data)


# -------------------- AccInvMast (Invoices) ---------------------

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def acc_inv_mast_list(request):
    user = request.user

    if user.is_superuser or user.user_type == 'admin':
        admin_client_id = getattr(user, 'client_id', '') or ''
        qs = AccInvMast.objects.filter(client_id=admin_client_id).order_by('-invdate', '-slno')

        if request.query_params.get('customerid'):
            qs = qs.filter(customerid=request.query_params['customerid'].strip())

        if request.query_params.get('date_from'):
            qs = qs.filter(invdate__gte=request.query_params['date_from'])

        if request.query_params.get('date_to'):
            qs = qs.filter(invdate__lte=request.query_params['date_to'])

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(customerid__icontains=search) |
                Q(slno__icontains=search)
            )

        total  = qs.count()
        limit  = min(int(request.query_params.get('limit',  50)), 200)
        offset = int(request.query_params.get('offset', 0))
        qs     = qs[offset: offset + limit]

        return Response({
            'total':   total,
            'limit':   limit,
            'offset':  offset,
            'results': AccInvMastSerializer(qs, many=True).data,
        })

    phone = (getattr(user, 'phone_number', '') or '').strip().lstrip('+')
    if len(phone) > 10:
        phone = phone[-10:]

    if not phone:
        return Response(
            {'error': 'No phone number linked to your account. Please contact admin.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    record = AccMaster.objects.filter(phone2__endswith=phone).first()
    if not record:
        return Response(
            {'error': 'No customer account found for your phone number. Please contact admin.'},
            status=status.HTTP_404_NOT_FOUND
        )

    qs = AccInvMast.objects.filter(
        customerid=record.code,
        client_id=record.client_id,
    ).order_by('-invdate', '-slno')

    if request.query_params.get('date_from'):
        qs = qs.filter(invdate__gte=request.query_params['date_from'])

    if request.query_params.get('date_to'):
        qs = qs.filter(invdate__lte=request.query_params['date_to'])

    total  = qs.count()
    limit  = min(int(request.query_params.get('limit', 20)), 50)
    offset = int(request.query_params.get('offset', 0))
    qs     = qs[offset: offset + limit]

    return Response({
        'total':         total,
        'limit':         limit,
        'offset':        offset,
        'customerid':    record.code,
        'customer_name': record.name,
        'results':       AccInvMastSerializer(qs, many=True).data,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def acc_inv_mast_detail(request, pk):
    user = request.user

    if user.is_superuser or user.user_type == 'admin':
        admin_client_id = getattr(user, 'client_id', '') or ''
        try:
            obj = AccInvMast.objects.get(pk=pk, client_id=admin_client_id)
        except AccInvMast.DoesNotExist:
            return Response({'error': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)

        customer = AccMaster.objects.filter(code=obj.customerid, client_id=admin_client_id).first()
        data = AccInvMastSerializer(obj).data
        data['customer_name']  = customer.name  if customer else None
        data['customer_place'] = customer.place if customer else None
        return Response(data)

    phone = (getattr(user, 'phone_number', '') or '').strip().lstrip('+')
    if len(phone) > 10:
        phone = phone[-10:]

    if not phone:
        return Response({'error': 'No phone number linked to your account.'}, status=status.HTTP_400_BAD_REQUEST)

    record = AccMaster.objects.filter(phone2__endswith=phone).first()
    if not record:
        return Response({'error': 'No customer account found for your phone number.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        obj = AccInvMast.objects.get(pk=pk, customerid=record.code, client_id=record.client_id)
    except AccInvMast.DoesNotExist:
        return Response(
            {'error': 'Invoice not found or does not belong to your account.'},
            status=status.HTTP_404_NOT_FOUND
        )

    data = AccInvMastSerializer(obj).data
    data['customer_name']  = record.name
    data['customer_place'] = record.place
    return Response(data)


# -------------------- Summary Stats (Admin only) ---------------------

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def sync_data_stats(request):
    if _require_admin(request.user):
        return Response({'error': 'Admin access only.'}, status=status.HTTP_403_FORBIDDEN)

    admin_client_id = getattr(request.user, 'client_id', '') or ''
    return Response({
        'acc_master_total': AccMaster.objects.filter(client_id=admin_client_id).count(),
        'invoices_total':   AccInvMast.objects.filter(client_id=admin_client_id).count(),
        'misel_total':      Misel.objects.all().count(),
    })


# ===================== MY POINTS (User-facing) =====================

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def my_points(request):
    user  = request.user
    phone = (getattr(user, 'phone_number', '') or '').strip().lstrip('+')
    if len(phone) > 10:
        phone = phone[-10:]

    record = AccMaster.objects.filter(phone2__endswith=phone).first() if phone else None
    raw    = (record.exregnodate or '0') if record else '0'

    return Response({
        'points': raw.strip() if raw else '0',
    })


# -------------------- BranchMaster (Invoice-style List & Detail) --------------------

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def branch_list(request):
    user = request.user

    if user.is_superuser or user.user_type == 'admin':
        qs = BranchMaster.objects.all().select_related('user').order_by('-created_at')

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(branch_name__icontains=search) |
                Q(branch_code__icontains=search) |
                Q(location__icontains=search)
            )

        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'].strip())

        if request.query_params.get('city'):
            qs = qs.filter(city__icontains=request.query_params['city'].strip())

        total  = qs.count()
        limit  = min(int(request.query_params.get('limit', 20)), 200)
        offset = int(request.query_params.get('offset', 0))
        qs     = qs[offset: offset + limit]

        return Response({
            'total':   total,
            'limit':   limit,
            'offset':  offset,
            'results': BranchMasterSerializer(qs, many=True, context={'request': request}).data,
        })

    qs = BranchMaster.objects.filter(status='active').order_by('-created_at')

    search = request.query_params.get('search', '').strip()
    if search:
        qs = qs.filter(
            Q(branch_name__icontains=search) |
            Q(branch_code__icontains=search) |
            Q(location__icontains=search)
        )

    if request.query_params.get('city'):
        qs = qs.filter(city__icontains=request.query_params['city'].strip())

    total  = qs.count()
    limit  = min(int(request.query_params.get('limit', 20)), 50)
    offset = int(request.query_params.get('offset', 0))
    qs     = qs[offset: offset + limit]

    results = [
        {
            'branch_name': b.branch_name,
            'branch_code': b.branch_code,
            'location':    b.location,
            'address':     b.address,
        }
        for b in qs
    ]

    return Response({
        'total':   total,
        'limit':   limit,
        'offset':  offset,
        'results': results,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def branch_detail(request, pk):
    user = request.user

    if user.is_superuser or user.user_type == 'admin':
        try:
            branch = BranchMaster.objects.select_related('user').get(pk=pk)
        except BranchMaster.DoesNotExist:
            return Response({'error': 'Branch not found.'}, status=status.HTTP_404_NOT_FOUND)

        data = BranchMasterSerializer(branch, context={'request': request}).data
        data['owner_name']  = branch.user.shop_name or branch.user.username
        data['owner_email'] = branch.user.email
        return Response(data)

    try:
        branch = BranchMaster.objects.get(pk=pk, user=user)
    except BranchMaster.DoesNotExist:
        return Response(
            {'error': 'Branch not found or does not belong to your account.'},
            status=status.HTTP_404_NOT_FOUND
        )

    return Response(BranchMasterSerializer(branch, context={'request': request}).data)


# ===================== PUSH NOTIFICATIONS =====================

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def list_fcm_tokens(request):
    """Admin-only: list all registered FCM tokens with user info."""
    if request.user.user_type != 'admin' and not request.user.is_superuser:
        return Response({'error': 'Admin access only'}, status=403)

    tokens = ExpoPushToken.objects.select_related('user').exclude(
        fcm_token__isnull=True
    ).exclude(fcm_token='').order_by('-updated_at')

    data = [
        {
            'id':           t.id,
            'fcm_token':    t.fcm_token,
            'expo_token':   t.token,
            'device_type':  t.device_type or '',
            'user_id':      t.user.id,
            'username':     t.user.username,
            'business_name': t.user.business_name or '',
            'phone_number': t.user.phone_number or '',
            'updated_at':   t.updated_at,
        }
        for t in tokens
    ]
    return Response({'count': len(data), 'tokens': data})


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def register_push_token(request):
    token             = (request.data.get('token')             or '').strip()
    fcm_token         = (request.data.get('fcm_token')         or '').strip()
    apns_device_token = (request.data.get('apns_device_token') or '').strip()
    device_type       = (request.data.get('device_type')       or '').strip()

    if not token:
        return Response({'error': 'token is required'}, status=400)

    defaults = {'user': request.user, 'device_type': device_type}
    if fcm_token:
        defaults['fcm_token'] = fcm_token
    if apns_device_token:
        defaults['apns_device_token'] = apns_device_token

    obj, created = ExpoPushToken.objects.update_or_create(
        token=token,
        defaults=defaults,
    )
    return Response({'message': 'Token registered', 'created': created})


def _send_common_notification(notif, request=None):
    from .push_notifications import send_expo_push_notification
    from .fcm_notifications import send_fcm_notification_with_image

    # Resolve image URL
    image_url = None
    if notif.image:
        try:
            image_url = request.build_absolute_uri(notif.image.url) if request else notif.image.url
        except Exception:
            image_url = notif.image.url
    elif notif.image_url:
        image_url = notif.image_url

    token_qs = ExpoPushToken.objects.select_related('user')
    if notif.target == 'active':
        token_qs = token_qs.filter(user__status='Active')

    sent_count       = 0
    dead_token_count = 0

    if image_url:
        # ── Has image → FCM V1 (Android) ────────────────────────────────────
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
            dead_token_count += len(fcm_dead)
            if fcm_dead:
                ExpoPushToken.objects.filter(fcm_token__in=fcm_dead).delete()

        # ── Has image → APNs direct (iOS) ───────────────────────────────────
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
            dead_token_count += len(apns_dead)
            if apns_dead:
                ExpoPushToken.objects.filter(apns_device_token__in=apns_dead).update(apns_device_token='')
    else:
        # ── No image → Expo push (reaches all 10 devices) ───────────────────
        expo_tokens = list(token_qs.values_list('token', flat=True))
        if expo_tokens:
            _, dead_tokens = send_expo_push_notification(
                expo_tokens, notif.title, notif.body, {}
            )
            if dead_tokens:
                ExpoPushToken.objects.filter(token__in=dead_tokens).delete()
            sent_count = len(expo_tokens) - len(dead_tokens)
            dead_token_count = len(dead_tokens)

    return sent_count, dead_token_count


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def send_push_notification(request):
    """Admin calls this to send a push notification to all users."""
    if request.user.user_type != 'admin':
        return Response({'error': 'Admin access only'}, status=403)

    title     = request.data.get('title', '').strip()
    body      = request.data.get('body', '').strip()
    image_url = request.data.get('image_url', '').strip()
    data      = request.data.get('data', {})

    if not title or not body:
        return Response({'error': 'title and body are required'}, status=400)

    # Attach image URL to notification data if provided
    if image_url:
        data = dict(data)
        data['imageUrl'] = image_url

    tokens = list(ExpoPushToken.objects.values_list('token', flat=True))
    if not tokens:
        return Response({'message': 'No registered tokens found'}, status=200)

    responses, dead_tokens = send_expo_push_notification(tokens, title, body, data)

    # Auto-delete dead/unregistered tokens
    deleted_count = 0
    if dead_tokens:
        deleted_count, _ = ExpoPushToken.objects.filter(token__in=dead_tokens).delete()

    return Response({
        'sent_to':             len(tokens),
        'batches':             len(responses),
        'dead_tokens_removed': deleted_count,
        'expo_response':       responses,
    })


# ── List / Create ──────────────────────────────────────────────
class CommonNotificationListCreateView(generics.ListCreateAPIView):
    serializer_class   = CommonNotificationSerializer
    permission_classes = [IsAdminOrReadOnly]
    # Accept both JSON (image_url) and multipart (image file upload)
    parser_classes     = [MultiPartParser, FormParser]

    def get_queryset(self):
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(hours=24)
        # Always show scheduled/draft; only show sent notifications from the last 24 hours
        return CommonNotification.objects.exclude(
            status='sent',
            sent_at__lt=cutoff,
        ).order_by('-created_at')

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        scheduled_at = serializer.validated_data.get('scheduled_at')

        if scheduled_at:
            # Save as scheduled — the APScheduler job will fire it later
            notif = serializer.save(created_by=request.user, status='scheduled')
            return Response(
                self.get_serializer(notif).data,
                status=status.HTTP_201_CREATED,
            )

        # No scheduled_at → send immediately, never leave as draft
        now   = timezone.now()
        notif = serializer.save(created_by=request.user, status='draft')

        try:
            sent_count, _ = _send_common_notification(notif, request=request)
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("_send_common_notification failed: %s", e)
            sent_count = 0

        notif.status     = 'sent'
        notif.sent_at    = now
        notif.sent_count = sent_count
        notif.save(update_fields=['status', 'sent_at', 'sent_count'])

        return Response(
            self.get_serializer(notif).data,
            status=status.HTTP_201_CREATED,
        )


# ── Retrieve / Update / Delete ─────────────────────────────────
class CommonNotificationDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = CommonNotificationSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset           = CommonNotification.objects.all()
    lookup_field       = 'pk'


# ── Send Now ───────────────────────────────────────────────────
@api_view(['POST'])
@permission_classes([IsAdminUser])
def send_common_notification(request, pk):
    """
    Admin hits this to instantly send a common notification to all/active users.
    POST /api/notifications/common/<uuid>/send/
    """
    try:
        notif = CommonNotification.objects.get(pk=pk)
    except CommonNotification.DoesNotExist:
        return Response({'error': 'Notification not found.'}, status=404)

    if notif.status == 'sent':
        return Response({'error': 'This notification has already been sent.'}, status=400)

    sent_count, dead_count = _send_common_notification(notif, request=request)

    notif.status     = 'sent'
    notif.sent_at    = timezone.now()
    notif.sent_count = sent_count
    notif.save(update_fields=['status', 'sent_at', 'sent_count'])

    if sent_count == 0 and dead_count == 0:
        return Response({
            'message':             'Notification marked as sent. No FCM tokens are registered yet.',
            'dead_tokens_cleaned': 0,
        })

    return Response({
        'message':             f'Notification sent to {sent_count} device(s).',
        'dead_tokens_cleaned': dead_count,
    })

# ===================== PDF INVOICES =====================

from .models import PDFInvoice
from .serializers import PDFInvoiceSerializer
import boto3
from botocore.config import Config
from django.conf import settings
import uuid as uuid_lib
import os

def _get_r2_client():
    """Return a boto3 S3 client pointed at Cloudflare R2."""
    return boto3.client(
        's3',
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def upload_pdf_invoice(request):
    """
    POST /api/pdf-invoices/upload/
    Called by an external sync system (Postman, script, etc.) to upload a PDF
    invoice and assign it to a user identified by phone number.

    No authentication required — open endpoint.

    Form-data fields:
      - file         (required) : PDF file, max 20 MB
      - phone_number (required) : user's registered phone number
      - title        (optional) : human-readable label e.g. "January Invoice"
    """
    # ── Resolve user by phone number ───────────────────────────────────────────
    phone_number = (request.data.get('phone_number') or '').strip()
    if not phone_number:
        return Response(
            {'error': 'phone_number is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Normalise to last 10 digits to handle +91XXXXXXXXXX or 91XXXXXXXXXX
    phone_number = phone_number[-10:]

    # Step 1: Check User table first
    target_user = User.objects.filter(phone_number__endswith=phone_number).first()

    if not target_user:
        # Step 2: Not in User table - check AccMaster (customers who haven't logged in yet)
        debtor = _find_debtor_by_phone(phone_number)

        if not debtor:
            # Number doesn't exist anywhere in the system
            return Response(
                {'error': f'No user found with phone number {phone_number}.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Step 3: Auto-create User from AccMaster so PDF can be assigned now.
        # When the customer eventually logs in, they will see their PDFs waiting.
        debtor_code = (debtor.get('code') or '').strip()
        debtor_name = (debtor.get('name') or '').strip()
        place       = (debtor.get('place') or '').strip()

        target_user, _ = User.objects.get_or_create(
            phone_number=phone_number,
            defaults={
                'username':      f'debtor_{debtor_code}_{phone_number}',
                'user_type':     'user',
                'status':        'Active',
                'business_name': debtor_name,
                'location':      place,
            }
        )

    serializer = PDFInvoiceSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    pdf_file = serializer.validated_data['file']
    title    = serializer.validated_data.get('title') or ''

    # Build a unique R2 object key: pdf_invoices/<target_user_id>/<uuid>_<original_name>
    safe_name  = pdf_file.name.replace(' ', '_')
    object_key = f"pdf_invoices/{target_user.id}/{uuid_lib.uuid4().hex}_{safe_name}"

    try:
        r2 = _get_r2_client()
        r2.upload_fileobj(
            pdf_file,
            settings.AWS_STORAGE_BUCKET_NAME,
            object_key,
            ExtraArgs={'ContentType': 'application/pdf'},
        )
    except Exception as e:
        return Response(
            {'error': f'R2 upload failed: {str(e)}'},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    # Build the public URL using the R2 custom domain configured in settings
    public_url = f"{settings.MEDIA_URL.rstrip('/')}/{object_key}"

    invoice = PDFInvoice.objects.create(
        user              = target_user,
        title             = title or None,
        original_filename = pdf_file.name,
        file_url          = public_url,
        file_key          = object_key,
        file_size         = pdf_file.size,
    )

    # ── Send push notification to the user's registered devices ──────────────
    try:
        user_tokens = list(
            ExpoPushToken.objects.filter(user=target_user).values_list('token', flat=True)
        )
        if user_tokens:
            notif_title = "🧾 New Invoice Available"
            notif_body  = f"Your invoice '{invoice.title or invoice.original_filename}' has been uploaded."
            _, dead_tokens = send_expo_push_notification(
                user_tokens,
                notif_title,
                notif_body,
                {'type': 'pdf_invoice', 'invoice_id': str(invoice.id)},
            )
            if dead_tokens:
                ExpoPushToken.objects.filter(token__in=dead_tokens).delete()
    except Exception as notif_err:
        # Non-fatal — invoice upload still succeeds even if push fails
        import logging
        logging.getLogger(__name__).warning("[PDFInvoice] Push notification failed: %s", notif_err)
    # ─────────────────────────────────────────────────────────────────────────

    return Response(
        PDFInvoiceSerializer(invoice).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def list_pdf_invoices(request):
    """
    GET /api/pdf-invoices/
    List PDF invoices for the authenticated user with pagination.

    Query params:
      - page      (default 1)
      - page_size (default 10, max 100)

    Response shape:
      {
        "total":       <int>,
        "page":        <int>,
        "page_size":   <int>,
        "total_pages": <int>,
        "results":     [ ... ]
      }
    """
    try:
        page      = max(1, int(request.query_params.get('page', 1)))
        page_size = min(100, max(1, int(request.query_params.get('page_size', 10))))
    except (ValueError, TypeError):
        page      = 1
        page_size = 10

    qs    = PDFInvoice.objects.filter(user=request.user).order_by('-uploaded_at')
    total = qs.count()

    import math
    total_pages = math.ceil(total / page_size) if total else 1

    # Clamp page to valid range
    page  = min(page, total_pages)
    start = (page - 1) * page_size
    end   = start + page_size

    results = PDFInvoiceSerializer(qs[start:end], many=True).data

    return Response({
        'total':       total,
        'page':        page,
        'page_size':   page_size,
        'total_pages': total_pages,
        'results':     results,
    })