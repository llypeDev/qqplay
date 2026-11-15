"""Development settings: everything permissive, nothing cached."""
from .base import *  # noqa: F401,F403

DEBUG = True

ALLOWED_HOSTS = ["*"]

# console email so signup and reset links appear in the terminal; a test that
# reads the messages (tools/e2e/integration.mjs) asks for files instead:
# EMAIL_BACKEND=django.core.mail.backends.filebased.EmailBackend EMAIL_FILE_PATH=<dir>
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")  # noqa: F405
EMAIL_FILE_PATH = env("EMAIL_FILE_PATH", str(BASE_DIR / ".mail"))  # noqa: F405

CELERY_TASK_ALWAYS_EAGER = True

INTERNAL_IPS = ["127.0.0.1", "localhost"]
