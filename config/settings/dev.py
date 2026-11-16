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

# An LMS on this machine (a test double, a Moodle in Docker) may register and
# launch over plain http from 127.0.0.1. Development only: this setting is
# not read by the production settings, where an LMS's addresses are held to
# https and public hosts like any other a site gives.
LTI_ALLOW_LOCAL_PLATFORMS = env_bool("LTI_ALLOW_LOCAL_PLATFORMS", False)  # noqa: F405
