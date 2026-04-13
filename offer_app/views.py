# views.py
from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework_simplejwt.tokens import RefreshToken
from django.db.models import Q
from django.utils import timezone
import secrets
import random
import string
import requests as http_requests
from django.core.cache import cache

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
                new_status = 'inactive'
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


# ─── WhatsApp OTP (AiSensy) ───────────────────────────────────────────────────
AISENSY_API_KEY  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6IjY0ZDM2ZTZiNzNjM2NmMjIwNmE4MjA2OCIsIm5hbWUiOiJjaGF0aWNvIGFsZXJ0IiwiYXBwTmFtZSI6IkFpU2Vuc3kiLCJjbGllbnRJZCI6IjY0ZDM2ZTZhNzNjM2NmMjIwNmE4MjA2MyIsImFjdGl2ZVBsYW4iOiJCQVNJQ19NT05USExZIiwiaWF0IjoxNzYyMTUyODUyfQ.Rl0OfVFNGiUd8vdaNHX9R0vBJLTdFa3Y7X-smA92c8w"
AISENSY_URL      = "https://backend.api-wa.co/campaign/chatico/api/v2"
AISENSY_CAMPAIGN = "testingauthentication"
AISENSY_USERNAME = "chatico alert"


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


def _send_whatsapp_otp(phone_number: str, otp: str, name: str = "user") -> tuple:
    payload = {
        "apiKey":         AISENSY_API_KEY,
        "campaignName":   AISENSY_CAMPAIGN,
        "destination":    f"91{phone_number}",
        "userName":       AISENSY_USERNAME,
        "templateParams": [name],
        "source":         "otp-login",
        "media":          {},
        "buttons": [
            {
                "type":       "button",
                "sub_type":   "url",
                "index":      0,
                "parameters": [{"type": "text", "text": otp}]
            }
        ],
        "carouselCards":       [],
        "location":            {},
        "attributes":          {},
        "paramsFallbackValue": {"FirstName": name}
    }
    try:
        res = http_requests.post(AISENSY_URL, json=payload, timeout=10)
        print(f"[AiSensy] status={res.status_code} | phone=91{phone_number} | response={res.text}")
        if res.status_code == 200:
            return True, ""
        try:
            err_data = res.json()
            err_msg  = err_data.get("message") or err_data.get("error") or res.text
        except Exception:
            err_msg = res.text or f"HTTP {res.status_code}"
        return False, err_msg
    except Exception as e:
        print(f"[AiSensy] Exception: {e}")
        return False, str(e)


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

    sent, err_msg = _send_whatsapp_otp(phone_number, otp, name)

    if not sent:
        print(f"[OTP] AiSensy send failed for {phone_number}: {err_msg}")
        return Response({
            "message":      f"OTP generated for number ending in {phone_number[-4:]}. Check terminal.",
            "phone_number": phone_number,
            "dev_otp":      otp,
        })

    return Response({
        "message":      f"OTP sent to WhatsApp number ending in {phone_number[-4:]}",
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
    Also accepts 'name' to greet user by name on WhatsApp and cache for verify step.
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

    sent, err_msg = _send_whatsapp_otp(phone_number, otp, name or "there")

    if not sent:
        print(f"[OTP SIGNUP] AiSensy send failed for {phone_number}: {err_msg}")
        return Response({
            "message":      f"OTP generated for number ending in {phone_number[-4:]}. Check terminal.",
            "phone_number": phone_number,
            "dev_otp":      otp,
        })

    return Response({
        "message":      f"OTP sent to WhatsApp number ending in {phone_number[-4:]}",
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

    sent, err_msg = _send_whatsapp_otp(phone_number, otp, name)

    if not sent:
        print(f"[OTP] AiSensy send failed for {phone_number}: {err_msg}")
        return Response({
            "message":      f"OTP generated for number ending in {phone_number[-4:]}. Check terminal.",
            "phone_number": phone_number,
            "requires_otp": True,
            "dev_otp":      otp,
        })

    return Response({
        "message":      f"OTP sent to WhatsApp number ending in {phone_number[-4:]}",
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

class OfferMasterListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]

    def get_queryset(self):
        auto_expire_offers()
        return OfferMaster.objects.all().prefetch_related('branches', 'media_files').order_by('-created_at')

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
            instance   = self.get_object()
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
            response_serializer = OfferMasterSerializer(instance, context={'request': request})
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
        offers            = branch.offers.filter(status='active').order_by('-created_at')
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
        offers     = offers.distinct().order_by('-created_at')
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

from .models import ExpoPushToken
from .push_notifications import send_expo_push_notification

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def register_push_token(request):
    """Mobile app calls this to save/update the Expo push token."""
    token       = request.data.get('token', '').strip()
    device_type = request.data.get('device_type', '').strip()  # 'ios' or 'android'

    if not token:
        return Response({'error': 'token is required'}, status=400)

    obj, created = ExpoPushToken.objects.update_or_create(
        token=token,
        defaults={'user': request.user, 'device_type': device_type}
    )
    return Response({'message': 'Token registered', 'created': created})


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def send_push_notification(request):
    """Admin calls this to send a push notification to all users."""
    if request.user.user_type != 'admin':
        return Response({'error': 'Admin access only'}, status=403)

    title = request.data.get('title', '').strip()
    body  = request.data.get('body', '').strip()
    data  = request.data.get('data', {})  # optional deep-link payload

    if not title or not body:
        return Response({'error': 'title and body are required'}, status=400)

    tokens = list(ExpoPushToken.objects.values_list('token', flat=True))
    if not tokens:
        return Response({'message': 'No registered tokens found'}, status=200)

    result = send_expo_push_notification(tokens, title, body, data)
    return Response({'sent_to': len(tokens), 'expo_response': result})