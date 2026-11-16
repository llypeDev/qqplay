from django.urls import path

from apps.live import views as live_views

from . import api, hooks, lti_views, media_api, views, wordpress
from .sessions import embed_api

app_name = "integrations"

urlpatterns = [
    # the frame on a site: its script, its sign-in, and the live's state and
    # chat with the session in a header (see sessions.py)
    path("embed/sdk.js", views.sdk, name="sdk"),
    path("embed/api/session/", views.session, name="session"),
    # signing in without a token: a code by e-mail, or the platform's word
    path("embed/api/login/start/", views.login_start, name="login_start"),
    path("embed/api/login/verify/", views.login_verify, name="login_verify"),
    path("embed/api/login/declared/", views.login_declared, name="login_declared"),
    path("embed/api/live/<str:uid>/state/", embed_api(live_views.state), name="live_state"),
    path("embed/api/live/<str:uid>/chat/", embed_api(live_views.chat_post), name="live_chat"),
    path(
        "embed/api/live/<str:uid>/chat/<int:message_id>/react/",
        embed_api(live_views.chat_react),
        name="live_react",
    ),
    path(
        "embed/api/live/<str:uid>/chat/<int:message_id>/hide/",
        embed_api(live_views.chat_hide),
        name="live_hide",
    ),
    path("embed/api/live/<str:uid>/chat/<int:message_id>/pin/", embed_api(live_views.chat_pin), name="live_pin"),
    path(
        "embed/api/live/<str:uid>/chat/<int:message_id>/answer/",
        embed_api(live_views.chat_answer),
        name="live_answer",
    ),
    path("embed/api/live/<str:uid>/chat/<int:message_id>/mute/", embed_api(live_views.chat_mute), name="live_mute"),
    path("embed/api/live/<str:uid>/chat/unmute/<int:mute_id>/", embed_api(live_views.chat_unmute), name="live_unmute"),
    path("embed/api/live/<str:uid>/chat/settings/", embed_api(live_views.chat_settings), name="live_settings"),
    # videos of the library and their comments, in a site's frame
    path("embed/api/media/<str:uid>/", embed_api(media_api.info), name="media_info"),
    path("embed/api/media/<str:uid>/comments/", embed_api(media_api.comments), name="media_comments"),
    path(
        "embed/api/media/<str:uid>/comments/<uuid:comment_id>/hide/",
        embed_api(media_api.hide),
        name="media_comment_hide",
    ),
    # the school's server (api.py)
    path("api/integrations/v1/", api.about, name="api_about"),
    path("api/integrations/v1/lives/", api.lives, name="api_lives"),
    path("api/integrations/v1/lives/<str:uid>/", api.live_detail, name="api_live"),
    path("api/integrations/v1/lives/<str:uid>/end/", api.live_end, name="api_live_end"),
    path("api/integrations/v1/lives/<str:uid>/attendance/", api.live_attendance, name="api_live_attendance"),
    path("api/integrations/v1/videos/", api.videos, name="api_videos"),
    path("api/integrations/v1/enrolments/", api.enrolment_list, name="api_enrolments"),
    path("api/integrations/v1/enrolments/<str:email>/", api.enrolment_detail, name="api_enrolment"),
    # purchases posted by the sales platforms (hooks.py)
    path("api/integrations/hooks/<str:key>/<str:secret>/", hooks.receive, name="purchase_hook"),
    # launched from inside an LMS (LTI 1.3: lti.py)
    path("plugins/wordpress/qqplay.zip", wordpress.download, name="wordpress_plugin"),

    path("lti/login/", lti_views.login, name="lti_login"),
    path("lti/launch/", lti_views.launch, name="lti_launch"),
    path("lti/jwks/", lti_views.jwks, name="lti_jwks"),
    path("lti/deeplink/", lti_views.deeplink, name="lti_deeplink"),
    path("lti/register/<str:token>/", lti_views.register, name="lti_register"),
    # the class by itself, for platforms that take a link and nothing else
    path("sala/<str:key>/<str:uid>/", views.room, {"kind": "live"}, name="room"),
    path("sala/<str:key>/v/<str:uid>/", views.room, {"kind": "video"}, name="room_video"),
    # Ids are whatever the site uses, slashes included.
    path("api/integrations/v1/students/<path:external_id>/block/", api.student, {"action": "block"}, name="api_student_block"),
    path(
        "api/integrations/v1/students/<path:external_id>/unblock/", api.student, {"action": "unblock"},
        name="api_student_unblock",
    ),
    path("api/integrations/v1/students/<path:external_id>/", api.student, name="api_student"),
    # the studio
    path("studio/integrations/", views.studio_list, name="list"),
    path("studio/integrations/<uuid:pk>/", views.studio_detail, name="detail"),
    path("studio/integrations/<uuid:pk>/rotate/", views.rotate, name="rotate"),
    path("studio/integrations/<uuid:pk>/delete/", views.delete, name="delete"),
    path("studio/integrations/<uuid:pk>/preview/", views.preview, name="preview"),
    path("studio/integrations/<uuid:pk>/look/", views.look, name="look"),
    path("studio/integrations/<uuid:pk>/webhook/test/", views.webhook_test, name="webhook_test"),
    path(
        "studio/integrations/<uuid:pk>/webhook/<int:delivery_id>/resend/",
        views.webhook_resend,
        name="webhook_resend",
    ),
    path("studio/integrations/<uuid:pk>/check/", views.check_token, name="check_token"),
    path("studio/integrations/<uuid:pk>/enrolments/", views.enrolments_view, name="enrolments"),
    path("studio/integrations/<uuid:pk>/lms/", views.lms_view, name="lms"),
    path("studio/integrations/<uuid:pk>/people/<int:person_id>/", views.person, name="person"),
]
