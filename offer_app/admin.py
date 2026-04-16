from django.contrib import admin
from .models import User, Category, Product, Offer, BranchMaster, OfferMaster, OfferMasterMedia, ExpoPushToken
from .models import CommonNotification

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('username', 'email', 'user_type', 'status', 'shop_name')


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'user', 'original_price', 'offer_price', 'created_at')
    list_filter = ('user', 'category', 'template_type')


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'template_type', 'created_at', 'is_public')
    filter_horizontal = ('products',)


# ✅ FIX: BranchMaster, OfferMaster, OfferMasterMedia were missing from admin
# Without these, you cannot manage branches or offer masters from the Django admin panel

@admin.register(BranchMaster)
class BranchMasterAdmin(admin.ModelAdmin):
    list_display = ('branch_name', 'branch_code', 'user', 'location', 'city', 'status', 'created_at')
    list_filter = ('status', 'city', 'state', 'country')
    search_fields = ('branch_name', 'branch_code', 'user__username', 'user__shop_name', 'location')


@admin.register(OfferMaster)
class OfferMasterAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'status', 'valid_from', 'valid_to', 'created_at')
    list_filter = ('status',)
    search_fields = ('title', 'user__username', 'user__shop_name')
    filter_horizontal = ('branches',)


@admin.register(OfferMasterMedia)
class OfferMasterMediaAdmin(admin.ModelAdmin):
    list_display = ('offer_master', 'media_type', 'order', 'caption', 'uploaded_at')
    list_filter = ('media_type',)
    search_fields = ('offer_master__title', 'caption')


@admin.register(ExpoPushToken)
class ExpoPushTokenAdmin(admin.ModelAdmin):
    list_display = ('user', 'device_type', 'token', 'created_at')
    search_fields = ('user__phone_number', 'token')




@admin.register(CommonNotification)
class CommonNotificationAdmin(admin.ModelAdmin):
    list_display = ('title', 'target', 'status', 'sent_count', 'scheduled_at', 'sent_at', 'created_by', 'created_at')
    list_filter = ('status', 'target')
    search_fields = ('title', 'body')
    readonly_fields = ('status', 'sent_at', 'sent_count', 'created_at', 'updated_at')