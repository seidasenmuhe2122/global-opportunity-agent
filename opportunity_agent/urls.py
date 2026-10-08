from django.urls import path
from . import views
from .forms import EmailAuthenticationForm
from django.contrib.auth import views as auth_views

urlpatterns = [
    path('', views.home, name='home'),
    path('private-access/', views.private_access, name='private_access'),
    path('private-access/<str:token>/', views.private_access, name='private_access_token'),
    path('website-visibility/', views.website_visibility_toggle, name='website_visibility_toggle'),
    path('registration-mode/', views.registration_mode_toggle, name='registration_mode_toggle'),
    path('robots.txt', views.robots_txt, name='robots_txt'),
    path('sitemap.xml', views.sitemap, name='sitemap'),
    path('dashboard/', views.user_dashboard, name='user_dashboard'),
    path('assistant/', views.ai_chat, name='ai_chat'),
    path('assistant/new/', views.ai_chat_new, name='ai_chat_new'),
    path(
        'assistant/conversations/<int:conversation_id>/messages/',
        views.ai_chat_send,
        name='ai_chat_send',
    ),
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('admin-dashboard/analytics/', views.analytics_dashboard, name='analytics_dashboard'),
    path('opportunities/', views.opportunity_list, name='opportunity_list'),
    path('opportunities/<int:pk>/', views.opportunity_detail, name='opportunity_detail'),
    path('opportunities/<int:pk>/apply/', views.apply_opportunity, name='apply_opportunity'),
    path('opportunities/<int:pk>/apply/', views.apply_opportunity, name='create_application'),
    path('profile/', views.profile_edit, name='profile_edit'),
    path('profiles/<int:user_id>/cv/', views.profile_cv_download, name='profile_cv_download'),
    path('credentials/', views.credentials, name='credentials'),
    path('accounts/signup/', views.signup, name='signup'),
    path('signup/', views.signup, name='signup_legacy'),
    path('accounts/login/', auth_views.LoginView.as_view(
        template_name='registration/login.html',
        authentication_form=EmailAuthenticationForm,
    ), name='login'),
    path('login/', auth_views.LoginView.as_view(
        template_name='registration/login.html',
        authentication_form=EmailAuthenticationForm,
    ), name='login_legacy'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout_legacy'),
]
