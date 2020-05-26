"""
Django settings for connectf project.

Generated by 'django-admin startproject' using Django 1.11.2.

For more information on this file, see
https://docs.djangoproject.com/en/1.11/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/1.11/ref/settings/
"""

import os
import tempfile

import yaml

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/1.11/howto/deployment/checklist/

with open(os.path.join(BASE_DIR, 'connectf/config.yaml')) as f:
    CONFIG = yaml.safe_load(f)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = CONFIG['SECRET_KEY']

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = CONFIG.get('DEBUG', True)

ALLOWED_HOSTS = []

# Application definition

INSTALLED_APPS = [
    'targetdb',
    'querytgdb',
    'feedback',
    'overview',
    'sungear_app',
    'django.contrib.contenttypes',
    'django.contrib.staticfiles',
    'corsheaders'
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'connectf.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request'
            ],
        },
    },
]

WSGI_APPLICATION = 'connectf.wsgi.application'

# Database
# https://docs.djangoproject.com/en/1.11/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': CONFIG['DATABASE']['NAME'],
        'USER': CONFIG['DATABASE']['USER'],
        'PASSWORD': CONFIG['DATABASE']['PASSWORD'],
        'HOST': CONFIG['DATABASE'].get('HOST', 'localhost'),
        'PORT': CONFIG['DATABASE'].get('PORT', '3306'),
        'OPTIONS': {
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'"
        },
        'TEST': {
            'NAME': 'test_targetdb'
        }
    }
}

# Password validation
# https://docs.djangoproject.com/en/1.11/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/1.11/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/1.11/howto/static-files/

STATIC_URL = '/static/'

MEDIA_URL = '/media/'

MEDIA_ROOT = 'media/'

# CORS_ORIGIN_ALLOW_ALL = True
CORS_ORIGIN_WHITELIST = [
    'http://127.0.0.1:3000',
    'http://localhost:8080',
    'http://localhost:8081'
]

CORS_ALLOW_CREDENTIALS = True

EMAIL_HOST = 'coruzzilab-macpro.bio.nyu.edu'

EMAIL_POST = 25

ALERT_EMAILS = [
    "clj327@nyu.edu"
]

CACHES = {
    'mem': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'TIMEOUT': 3600
    },
    'default': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': tempfile.gettempdir(),
        'TIMEOUT': 3600
    },
}

# Configure motif annotation file and cluster definitions here
MOTIF_ANNOTATION = CONFIG.get('MOTIF_ANNOTATION',
                              os.path.join(BASE_DIR, 'data', 'motifs.csv.gz'))
MOTIF_TF_ANNOTATION = CONFIG.get('MOTIF_TF_ANNOTATION',
                                 os.path.join(BASE_DIR, 'data', 'motifs_indv.csv.gz'))
MOTIF_CLUSTER_INFO = CONFIG.get('MOTIF_CLUSTER_INFO', os.path.join(BASE_DIR, 'data', 'cluster_info.csv.gz'))
GENE_LISTS = CONFIG.get('GENE_LISTS', os.path.join(BASE_DIR, 'commongenelists'))
TARGET_NETWORKS = CONFIG.get('TARGET_NETWORKS', os.path.join(BASE_DIR, 'target_networks'))

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose'
        },
    },
    'loggers': {
        # 'django': {
        #     'handlers': ['console'],
        #     'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO')
        # },
        'querytgdb': {
            'handlers': ['console'],
            'level': 'WARNING'
        },
        'django.db.backends': {
            'level': 'WARNING',
            'handlers': ['console'],
        },
        'sungear': {
            'handlers': ['console'],
            'level': 'INFO'
        }
    },
}

# google recaptcha secret
RECAPTCHA_SECRET = CONFIG.get('RECAPTCHA_SECRET')

# AWS credentials
AWS_ACCESS_KEY_ID = CONFIG.get('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = CONFIG.get('AWS_SECRET_ACCESS_KEY')
AWS_TOPIC_ARN = CONFIG.get('AWS_TOPIC_ARN')
AWS_REGION_NAME = CONFIG.get('AWS_REGION_NAME')

NAMED_QUERIES = {
    # User predefined queries.
    'all_expression': 'all_tfs[EXPERIMENT_TYPE=Expression]',
    'all_dap': 'all_tfs[EDGE_TYPE=ampDap or EDGE_TYPE=DAP]',
    'in_planta_bound': "all_tfs[EDGE_TYPE='in planta:Bound']"
}