from django.urls import path
from . import views
from .views import public_branch_offers

urlpatterns = [
    # ---------- AUTH ----------
    path('admin/login/', views.admin_login, name='admin-login'),

    # Customer LOGIN (AccMaster or previously signed-up users)
    path('user/request-otp/', views.user_request_otp, name='user-request-otp'),
    path('user/verify-otp/', views.user_verify_otp, name='user-verify-otp'),

    # Customer SIGN-UP (brand-new users not in AccMaster)
    path('user/request-otp-signup/', views.user_request_otp_signup, name='user-request-otp-signup'),
    path('user/verify-otp-signup/', views.user_verify_otp_signup, name='user-verify-otp-signup'),

    path('register/', views.register_user, name='register-user'),

    # ---------- CATEGORY ----------
    path('categories/', views.CategoryListCreateView.as_view(), name='category-list'),
    path('categories/<int:pk>/', views.CategoryDetailView.as_view(), name='category-detail'),
    path('categories/<int:category_id>/update-image/', views.update_category_image, name='category-update-image'),

    # ---------- PRODUCTS ----------
    path('products/', views.ProductListCreateView.as_view(), name='product-list'),
    path('products/category/<str:category_name>/', views.products_by_category, name='products-by-category'),
    path('products/<uuid:pk>/', views.ProductDetailView.as_view(), name='product-detail'),

    # ---------- TEMPLATES ----------
    path('templates/', views.TemplateListView.as_view(), name='templates-list'),

    # ---------- NEW OFFER SYSTEM ----------
    path('offers/create/', views.OfferCreateView.as_view(), name='offer-create'),
    path('offers/<uuid:offer_id>/', views.public_offer_detail, name='offer-detail'),

    # ---------- OFFER MASTER ----------
    path('offer-master/stats/', views.offer_master_stats, name='offer-master-stats'),
    path('offer-master/', views.OfferMasterListCreateView.as_view(), name='offer-master-list-create'),
    path('offer-master/<uuid:pk>/', views.OfferMasterDetailView.as_view(), name='offer-master-detail'),
    path('offer-master/<uuid:pk>/media/<uuid:media_id>/', views.delete_offer_master_media, name='offer-master-media-delete'),

    # ---------- BRANCH MASTER ----------
    path('branch-master/stats/', views.branch_master_stats, name='branch-master-stats'),
    path('branch-master/', views.BranchMasterListCreateView.as_view(), name='branch-master-list-create'),
    path('branch-master/<uuid:pk>/', views.BranchMasterDetailView.as_view(), name='branch-master-detail'),

    # ---------- USERS ----------
    path('users/dropdown/', views.get_all_users_for_dropdown, name='users-dropdown'),
    path('misel-sync/', views.sync_misel_shops, name='misel-sync'),

    # ---------- BRANCHES (functional endpoints) ----------
    path('branches/my-branches/', views.get_user_branches, name='user-branches'),
    path('branches/dropdown/', views.get_all_branches_dropdown, name='branches-dropdown'),
    path('branches/<uuid:branch_id>/offers/', views.get_branch_offers, name='branch-offers'),

    # ---------- PUBLIC ENDPOINTS (No authentication required) ----------
    path('public/offers/', views.discover_offers, name='public-discover-offers'),
    path('public/branches/', views.get_all_active_branches_public, name='public-branches-with-offers'),
    path('public/branch/<uuid:branch_id>/offers/', public_branch_offers, name='public-branch-offers'),

    # ---------- OLD OFFER (per product) ----------
    path('offer/<uuid:product_id>/', views.get_offer, name='legacy-offer'),

    # ---------- PROFILE ----------
    path('profile/', views.user_profile, name='user-profile'),

    # ---------- DASHBOARD STATS ----------
    path('dashboard/stats/', views.user_dashboard_stats, name='dashboard-stats'),

    # ---------- USER INVOICES ----------
    path('invoices/my/', views.user_invoices, name='user-invoices'),

    # ---------- ADMIN ----------
    path('admins/stats/', views.AdminStatsView.as_view(), name='admin-stats'),
    path('admins/', views.AdminListView.as_view(), name='admin-list'),
    path('admins/<int:pk>/', views.AdminDetailView.as_view(), name='admin-detail'),

    path('misel/', views.misel_list, name='misel-list'),
    path('misel/<int:pk>/', views.misel_detail, name='misel-detail'),

    # ---------- INVOICES (Admin) ----------
    path('invoices/', views.acc_inv_mast_list, name='invoice-list'),
    path('invoices/<int:pk>/', views.acc_inv_mast_detail, name='invoice-detail'),

    path('acc-master/', views.acc_master_list, name='acc-master-list'),
    path('acc-master/<int:pk>/', views.acc_master_detail, name='acc-master-detail'),

    # ---------- BRANCHES (Invoice-style) ----------
    path('branches/', views.branch_list, name='branch-list'),
    path('branches/<uuid:pk>/', views.branch_detail, name='branch-detail'),

    # ---------- PUSH NOTIFICATIONS ----------
    path('push/register-token/',    views.register_push_token,    name='register-push-token'),
    path('push/send-notification/', views.send_push_notification, name='send-push-notification'),
]